"""Alert routing with append-only file audit + once-per-key dedup.

Adapted from a prior agent stack's alert routing. Webhook delivery is optional and
imported lazily (keeps this module network-free at import — purity tests
depend on it). Severities: info | warning | critical.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AlertRouter:
    """File-backed alert sink with dedup. Webhook is opt-in via webhook_url."""
    log_path: Path
    webhook_url: str | None = None
    _seen_keys: set[str] = field(default_factory=set)

    def route(self, *, severity: str, title: str, summary: str, now_iso: str,
              dedup_key: str | None = None, extra: dict[str, Any] | None = None
              ) -> bool:
        """Record an alert. If dedup_key is given and already seen this run,
        the alert is suppressed (returns False). Always writes to the file
        audit log on first sight; webhook is best-effort and never raises."""
        if dedup_key is not None:
            if dedup_key in self._seen_keys:
                return False
            self._seen_keys.add(dedup_key)
        row = {'ts': now_iso, 'severity': severity, 'title': title,
               'summary': summary, **(extra or {})}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open('a') as f:
            f.write(json.dumps(row) + '\n')
        if self.webhook_url and severity in ('warning', 'critical'):
            self._post_webhook(row)
        return True

    def _post_webhook(self, row: dict) -> None:
        try:                                  # lazy import: no net stack at import
            import urllib.request
            assert self.webhook_url is not None
            req = urllib.request.Request(
                self.webhook_url, data=json.dumps(row).encode(),
                headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass                              # best-effort; never break the path
