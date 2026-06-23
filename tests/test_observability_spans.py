"""Observability is wired, not just configured: the resolution and gateway
seams emit named spans with the right (redacted) attributes, the gateway's
swallowed faults surface as logs, and Sentry stays off without a DSN.

Spans are asserted with a recording fake tracer (no OpenTelemetry needed in CI);
that proves the instrumentation is present and correct deterministically.
"""
from __future__ import annotations

import logging

from gateway_fixtures import build_gateway

from gateway import Channel


class _RecSpan:
    def __init__(self, name):
        self.name = name
        self.attrs: dict = {}
        self.exceptions: list = []

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def record_exception(self, e):
        self.exceptions.append(e)

    def set_status(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecTracer:
    def __init__(self):
        self.spans: list[_RecSpan] = []

    def start_as_current_span(self, name, *a, **k):
        sp = _RecSpan(name)
        self.spans.append(sp)
        return sp


def _open(gw, sessions, sid='S'):
    return sessions.open(sid, f'chan-{sid}')


def test_gateway_turn_emits_span(tmp_path, monkeypatch):
    rec = _RecTracer()
    monkeypatch.setattr('gateway.orchestrator.tracer', rec)
    gw, sessions, _, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    gw.turn('S', tok, 'is K5-24SBC in stock?', channel=Channel.TYPED)

    turn_spans = [s for s in rec.spans if s.name == 'gateway.turn']
    assert turn_spans, [s.name for s in rec.spans]
    sp = turn_spans[0]
    assert sp.attrs.get('svc.task') == 'gateway.turn'
    assert sp.attrs.get('svc.outcome')          # the response kind is recorded
    assert sp.attrs.get('svc.channel') == 'typed'


def test_resolve_emits_span(tmp_path, monkeypatch):
    rec = _RecTracer()
    monkeypatch.setattr('resolution.service.tracer', rec)
    gw, sessions, _, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    gw.turn('S', tok, 'is K5-24SBC in stock?', channel=Channel.TYPED)

    resolve_spans = [s for s in rec.spans if s.name == 'resolve.turn']
    assert resolve_spans, [s.name for s in rec.spans]
    assert resolve_spans[0].attrs.get('svc.task') == 'resolve'


def test_internal_fault_is_logged_not_swallowed(tmp_path, monkeypatch):
    from observability import get_logger
    logger = get_logger('gateway')           # 'sku.gateway' tree has propagate=False,
    records = []                             # so capture with our own handler, not caplog
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)

    gw, sessions, _, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)

    # Force an internal fault inside dispatch.
    def _boom(*a, **k):
        raise RuntimeError('dependency down')
    monkeypatch.setattr(gw, '_dispatch', _boom)
    try:
        resp = gw.turn('S', tok, 'anything', channel=Channel.TYPED)
    finally:
        logger.removeHandler(handler)

    # Fault is surfaced (logged), not silently swallowed...
    assert any('gateway.internal_fault' in r.getMessage() for r in records)
    # ...and the caller still gets a coherent turn (not a 500/raise).
    assert resp is not None


def test_sentry_off_without_dsn(monkeypatch):
    from observability import init_error_tracking
    monkeypatch.delenv('SENTRY_DSN', raising=False)
    assert init_error_tracking() is False


def test_log_event_format():
    from observability import get_logger, log_event
    logger = get_logger('test')
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        logger.setLevel(logging.INFO)
        log_event(logger, 'info', 'thing.happened', a=1, b='x')
    finally:
        logger.removeHandler(handler)
    assert records and records[0].getMessage() == 'thing.happened a=1 b=x'
