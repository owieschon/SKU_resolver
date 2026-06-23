"""Tracing with a no-op fallback and a redaction chokepoint.

Adapted from a prior agent stack's telemetry (verified in
the prior project), generalized to be domain-neutral:
no task-tier specifics, a registerable structured-attribute allowlist, and a
domain PII pattern for spoken/written account numbers (the gateway journal
risk).

Design guarantees:
  - OFF by default. `tracer` is a no-op until init_tracing() succeeds, so
    importing this module — or emitting a span without init — is always safe.
  - FAIL-OPEN. Any bootstrap or export error degrades to the no-op tracer and
    is swallowed; observability never breaks the main path.
  - NETWORK-FREE IMPORT. OpenTelemetry is imported lazily inside init_tracing()
    only, so this module (and anything that imports it) pulls no network stack
    at import time — the harness/gateway purity tests depend on this.
  - REDACT EVERYTHING. Every span attribute passes through redact(): a
    registered structured attr passes as-is; everything else is treated as
    content (fail-closed) — PII-scrubbed, capped, or dropped entirely when
    content is disabled.
"""
from __future__ import annotations

import os
import re

_ENV_ON = 'SKU_OBS_TRACING'
_ENV_CONTENT = 'SKU_OBS_TRACE_CONTENT'
_ENV_MAXCHARS = 'SKU_OBS_TRACE_MAX_CHARS'


def _content_enabled() -> bool:
    return os.environ.get(_ENV_CONTENT, '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _max_chars() -> int:
    try:
        return max(0, int(os.environ.get(_ENV_MAXCHARS, '800')))
    except Exception:
        return 800


# ── No-op tracer / span ──────────────────────────────────────────────────────

class _NoopSpan:
    def set_attribute(self, *a, **k): return None
    def set_status(self, *a, **k): return None
    def record_exception(self, *a, **k): return None
    def add_event(self, *a, **k): return None
    def end(self, *a, **k): return None
    def __enter__(self): return self
    def __exit__(self, *a): return False   # never suppress caller's exception


class _NoopTracer:
    def start_as_current_span(self, *a, **k): return _NoopSpan()
    def start_span(self, *a, **k): return _NoopSpan()


tracer = _NoopTracer()
_initialized = False


# ── Redaction ────────────────────────────────────────────────────────────────

# Structured attrs carry no free-text PII — scalars/short ids we set ourselves.
# Domain-neutral defaults; callers register their own via register_structured().
_STRUCTURED_ATTRS: set[str] = {
    'svc.name', 'svc.task', 'svc.phase', 'svc.outcome',
    'svc.source', 'svc.confidence', 'svc.channel', 'svc.refused',
    'llm.model_name', 'llm.provider', 'llm.cost.total', 'llm.latency_s',
    'llm.token_count.prompt', 'llm.token_count.completion',
    'session.id', 'svc.run_id', 'svc.tenant_id', 'svc.catalog_version',
}

# Content attrs: free text that may contain PII. Always capped + scrubbed;
# dropped entirely when content disabled. (Informational — anything NOT in the
# structured set is treated as content anyway, fail-closed.)
_CONTENT_ATTRS = frozenset({'input.value', 'output.value', 'transcript.text'})

_PII_PATTERNS = [
    (re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'), '[EMAIL]'),
    (re.compile(r'\b(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b'), '[PHONE]'),
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN]'),
    # Domain addition (gateway): spoken/typed customer account numbers, both
    # bare (9-12 digits) and the "account 12345678" phrasing.
    # An account number near the word "account/acct" — scrub even short runs
    # (3+ digits), tolerating filler ("account number is 1001", "acct #4837").
    # Over-scrubbing in a journal is safe; under-scrubbing leaks a credential.
    (re.compile(r'\b(account|acct)\b[^\d]{0,15}\d{3,12}\b', re.I),
     r'\1 [ACCOUNT]'),
    (re.compile(r'\b\d{9,12}\b'), '[ACCOUNT]'),
    (re.compile(r'sk-ant-[A-Za-z0-9\-_]{8,}'), '[APIKEY]'),
    (re.compile(r'\b[A-Za-z0-9\-_]{32,}\b'), '[TOKEN]'),
]


def register_structured(*attr_names: str) -> None:
    """Declare scalar attribute names that pass through redaction untouched.
    Use for ids/labels you set yourself. Anything unregistered is content."""
    _STRUCTURED_ATTRS.update(attr_names)


def scrub_pii(text: str) -> str:
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact(attr_name: str, value):
    """Single chokepoint. Structured -> as-is; content -> scrub+cap, or None
    when content disabled (caller skips the attr). Unknown names are content
    (fail-closed). Never raises."""
    try:
        if attr_name in _STRUCTURED_ATTRS:
            return value
        if not _content_enabled():
            return None
        s = value if isinstance(value, str) else str(value)
        s = scrub_pii(s)
        cap = _max_chars()
        if len(s) > cap:
            s = s[:cap] + f'...[+{len(s) - cap} chars]'
        return s
    except Exception:
        return '[REDACTION_ERROR]'


def set_attr(span, attr_name: str, value) -> None:
    try:
        red = redact(attr_name, value)
        if red is None:
            return
        span.set_attribute(attr_name, red)
    except Exception:
        pass


# ── Init (fail-open, OTel optional and lazy) ─────────────────────────────────

def init_tracing(service_name: str = 'sku-engine') -> bool:
    """Bootstrap OTel/Phoenix if SKU_OBS_TRACING is truthy AND the libs are
    present. Returns True if live tracing was enabled, False otherwise (no-op).
    Never raises."""
    global tracer, _initialized
    if _initialized:
        return not isinstance(tracer, _NoopTracer)
    _initialized = True
    if os.environ.get(_ENV_ON, '').strip().lower() not in ('1', 'true', 'yes', 'on'):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        endpoint = os.environ.get('SKU_OBS_OTLP_ENDPOINT',
                                  'http://localhost:4318/v1/traces')
        provider = TracerProvider(
            resource=Resource.create({'service.name': service_name}))
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer(service_name)
        return True
    except Exception:
        tracer = _NoopTracer()   # fail-open: any error -> no-op, swallowed
        return False


def reset_for_test() -> None:
    """Test hook: forget init state so a test can re-init."""
    global tracer, _initialized
    tracer = _NoopTracer()
    _initialized = False
