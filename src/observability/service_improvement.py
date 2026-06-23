"""Service-improvement capture — anonymized, PII-scrubbed records of how the
service performs on real interactions, used to improve resolution quality and
the product over time.

Captured as a byproduct of normal use (shadow observation, confirmations,
corrections). Privacy posture: tenant/account identifiers are one-way hashed;
transcript text is scrubbed of phone numbers and account-number-shaped digits
before anything is retained. Off by default — records are kept in memory unless
a path is provided, then appended as JSONL.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_PHONE = re.compile(r'\b\d[\d\-\(\)\. ]{6,}\d\b')
_ACCT = re.compile(r'\b\d{3,8}\b')


def anon_key(value: str) -> str:
    """One-way, stable key for a tenant/account — no identity recoverable."""
    return hashlib.sha256(('svc-improve:' + str(value)).encode()).hexdigest()[:12]


def scrub(text: str) -> str:
    """Remove phone numbers and account-number-shaped digit runs from text."""
    return _ACCT.sub('[redacted]', _PHONE.sub('[redacted]', text or ''))


class ImprovementLog:
    def __init__(self, path: str | None = None, *, tenant: str = '',
                 now_iso=lambda: '', max_rows: int = 10_000) -> None:
        self._path = Path(path) if path else None
        self._tenant = anon_key(tenant) if tenant else ''
        self._now = now_iso
        self._max_rows = max_rows      # bound in-memory growth (always-on)
        self.rows: list[dict] = []

    def _write(self, row: dict) -> None:
        row = {'tenant': self._tenant, 'ts': self._now(), **row}
        self.rows.append(row)
        if len(self.rows) > self._max_rows:    # keep only the most recent
            del self.rows[:-self._max_rows]
        if self._path is not None:
            # Append-only on disk; rotate the file externally (logrotate) — the
            # on-disk stream is the durable record, the in-memory list is bounded.
            with self._path.open('a') as f:
                f.write(json.dumps(row) + '\n')

    def record_attempt(self, attempt) -> None:
        """One shadow-observation attempt (what the tool would have done)."""
        self._write({
            'kind': 'attempt',
            'utterance': scrub(attempt.utterance),
            'outcome': attempt.outcome, 'state': attempt.state,
            'sku': attempt.sku, 'confidence': attempt.confidence,
            'candidates': list(attempt.candidate_skus), 'source': attempt.source})

    def record_correction(self, *, category: str, kind: str,
                          phrase: str = '', sku: str = '',
                          behavior: str = '') -> None:
        """One SME correction from the HITL session (grammar/semantic alias or a
        graceful-degradation choice)."""
        self._write({'kind': 'correction', 'category': category,
                     'correction_kind': kind, 'phrase': scrub(phrase),
                     'sku': sku, 'behavior': behavior})

    def record_self_heal(self, heal) -> None:
        """One self-heal harvested from how a human rep resolved an inquiry the
        tool missed — used to improve the service. PII-scrubbed."""
        self._write({'kind': 'self_heal', 'source': heal.source,
                     'failed_utterance': scrub(heal.failed_utterance),
                     'healed_sku': heal.healed_sku, 'rep_turn': scrub(heal.rep_turn),
                     'confidence': heal.confidence, 'applied': heal.applied})

    def record_answer(self, *, correlation_id: str, account: str, sku: str,
                      answer_kind: str, basis: str = '') -> None:
        """One answer the service issued, with a correlation id so the eventual
        real-world outcome can be joined later to measure/improve quality."""
        self._write({'kind': 'answer', 'correlation_id': correlation_id,
                     'account': anon_key(account) if account else '',
                     'sku': sku, 'answer_kind': answer_kind, 'basis': basis})
