"""Error tracking (Sentry) — off by default, fail-open, PII-scrubbed.

Mirrors the tracing posture in telemetry.py:
  - OFF unless SENTRY_DSN is set AND sentry-sdk is installed (an optional extra).
  - FAIL-OPEN: any import/init error degrades to a no-op; never raises, so a
    misconfigured deploy still boots.
  - PII-SCRUBBED: `send_default_pii=False` plus a before-send hook that runs the
    same `scrub_pii` chokepoint over the event message — errors must not carry a
    customer's account number or contact details off-box.
"""
from __future__ import annotations

import os


def init_error_tracking(*, environment: str = 'production') -> bool:
    """Initialize Sentry iff SENTRY_DSN is set and the SDK is present.
    Returns True if error tracking was enabled, False (no-op) otherwise."""
    dsn = os.environ.get('SENTRY_DSN', '').strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        from observability.telemetry import scrub_pii

        def _before_send(event, _hint):
            try:
                msg = event.get('message')
                if isinstance(msg, str):
                    event['message'] = scrub_pii(msg)
                for exc in (event.get('exception', {}) or {}).get('values', []):
                    if isinstance(exc.get('value'), str):
                        exc['value'] = scrub_pii(exc['value'])
            except Exception:
                pass  # never let scrubbing break delivery
            return event

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get('SENTRY_ENVIRONMENT', environment),
            send_default_pii=False,
            traces_sample_rate=0.0,  # tracing is OTel's job (telemetry.py)
            integrations=[StarletteIntegration(), FastApiIntegration()],
            before_send=_before_send,
        )
        return True
    except Exception:
        return False  # fail-open: SDK missing or init failed -> no-op
