"""Twilio request-signature validation (pure stdlib — no twilio SDK).

Twilio signs every webhook with the account auth token so the receiver can
prove the request really came from Twilio and not a spoofer hitting the public
`/voice` URL. The algorithm (Twilio "Security" docs):

  1. Start with the full request URL (scheme://host/path?query).
  2. Append each POST param as key+value, sorted by key, concatenated.
  3. HMAC-SHA1 over that string, keyed by the account auth token.
  4. Base64-encode; compare (constant-time) to the X-Twilio-Signature header.

Posture (matches `using_dev_secret`): if no TWILIO_AUTH_TOKEN is configured we
are in local/ngrok dev — validation is skipped and `signature_enforced()`
reports False so the surface can advertise the gap clearly. When the token IS
set (production), an invalid/missing signature is rejected. This keeps "go to
production" = set an env var, not a code change.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Mapping


def signature_enforced() -> bool:
    """True when a TWILIO_AUTH_TOKEN is configured (production posture)."""
    return bool(os.environ.get('TWILIO_AUTH_TOKEN'))


def expected_signature(auth_token: str, url: str,
                       params: Mapping[str, str]) -> str:
    """Compute the Twilio X-Twilio-Signature value for (url, POST params)."""
    data = url + ''.join(f'{k}{params[k]}' for k in sorted(params))
    digest = hmac.new(auth_token.encode('utf-8'), data.encode('utf-8'),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode('ascii')


def validate(url: str, params: Mapping[str, str], signature: str,
             auth_token: str | None = None) -> bool:
    """Validate a Twilio webhook signature.

    Fail-open ONLY when no auth token is configured (local dev). When a token
    is present, a missing or mismatched signature fails closed.
    """
    token = auth_token if auth_token is not None else os.environ.get(
        'TWILIO_AUTH_TOKEN')
    if not token:
        return True  # dev/ngrok: no token => validation disabled (see module doc)
    if not signature:
        return False
    expected = expected_signature(token, url, params)
    return hmac.compare_digest(expected, signature)
