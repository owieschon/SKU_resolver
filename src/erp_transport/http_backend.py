"""HTTPS Backend for the ERP harness — Business Central OData over OAuth.

Implements `erp_harness.transport.Backend` so the SafetyEnforcer can drive a
real tenant the same way it drives the twin. The enforcer still owns the rate
budget, method allowlist, backoff, and journal — this class only moves bytes.

Testable without a network: `urlopen` is injectable, so request building, the
bearer header, JSON parsing, non-2xx pass-through (429/403 reach the enforcer
as TransportResponses, not exceptions), and timeout -> TransportTimeout are all
unit-tested against a fake transport. The credential-gated live smoke is the
only path that touches a real endpoint, and (per the harness spec) live-tenant
runs are gated behind the twin fault-injection check matrix.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlencode

from erp_harness.transport import (
    Backend, Clock, TransportRequest, TransportResponse, TransportTimeout,
)

_READ_ALLOWED = {'GET', 'HEAD'}


def certifi_urlopen(req, timeout=30):
    """urlopen with a certifi CA bundle (TLS verifies on interpreters without a
    system trust store). The default opener for the HTTP adapters."""
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:   # pragma: no cover
        ctx = None
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


class OAuthError(Exception):
    """Token acquisition failed (bad client creds / token endpoint)."""


class OAuthClientCredentials:
    """OAuth2 client-credentials token source (Entra/BC). Caches the token and
    refreshes it shortly before expiry, using the injected Clock so expiry is
    deterministic under test. `urlopen` is injectable for mocked tests."""

    def __init__(self, token_url: str, client_id: str, client_secret: str,
                 scope: str, clock: Clock, *, urlopen=None,
                 refresh_margin_s: float = 60.0) -> None:
        self._token_url = token_url
        self._form = {'grant_type': 'client_credentials',
                      'client_id': client_id, 'client_secret': client_secret,
                      'scope': scope}
        self._clock = clock
        self._urlopen = urlopen or certifi_urlopen
        self._margin = refresh_margin_s
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        if self._token and self._clock.now() < self._expires_at - self._margin:
            return self._token
        body = urlencode(self._form).encode()
        req = urllib.request.Request(
            self._token_url, data=body, method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with self._urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, ValueError, socket.timeout) as e:
            raise OAuthError(f'token request failed: {e}') from e
        self._token = data.get('access_token')
        if not self._token:
            raise OAuthError('token response had no access_token')
        self._expires_at = self._clock.now() + float(data.get('expires_in', 3600))
        return self._token

    def __call__(self) -> str:   # usable directly as a token_provider
        return self.token()


class HttpBackend:
    """erp_harness.transport.Backend over HTTPS (OData-style JSON)."""

    def __init__(self, base_url: str, *, token_provider=None, urlopen=None,
                 timeout_s: float = 30.0) -> None:
        self._base = base_url.rstrip('/')
        self._token_provider = token_provider
        self._urlopen = urlopen or certifi_urlopen
        self._timeout = timeout_s

    def _url(self, req: TransportRequest) -> str:
        url = f'{self._base}/{req.path.lstrip("/")}'
        if req.params:
            url = f'{url}?{urlencode(dict(req.params))}'
        return url

    def handle(self, req: TransportRequest) -> TransportResponse:
        if req.method not in _READ_ALLOWED:
            # Defense in depth: the enforcer blocks writes first; the wire
            # transport refuses them too rather than ever emitting one.
            raise PermissionError(f'HttpBackend is read-only: {req.method}')
        headers = {'Accept': 'application/json'}
        if self._token_provider is not None:
            headers['Authorization'] = f'Bearer {self._token_provider()}'
        request = urllib.request.Request(self._url(req), method=req.method,
                                         headers=headers)
        try:
            with self._urlopen(request, timeout=self._timeout) as resp:
                return _to_response(getattr(resp, 'status', 200),
                                    _headers(resp.headers), resp.read())
        except urllib.error.HTTPError as e:
            # 4xx/5xx are SIGNAL for the enforcer (429 backoff, 403 grant gap),
            # not transport failures — pass them through as responses.
            body = b''
            try:
                body = e.read()
            except Exception:
                pass
            return _to_response(e.code, _headers(e.headers), body)
        except (socket.timeout, TimeoutError) as e:
            raise TransportTimeout(f'{req.method} {req.path}: {e}') from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TransportTimeout(f'{req.method} {req.path}: {e}') from e
            raise


def _headers(h) -> dict:
    """Normalize urllib/email/httplib header objects (or a dict) to a plain
    dict — they expose .items() but aren't all dict()-able directly."""
    if h is None:
        return {}
    try:
        return dict(h.items())
    except AttributeError:
        return dict(h)


def _to_response(status: int, headers: dict, body: bytes) -> TransportResponse:
    text = body.decode('utf-8', 'replace') if body else ''
    ctype = ''
    for k, v in headers.items():
        if k.lower() == 'content-type':
            ctype = v
            break
    data = None
    if text and 'json' in ctype.lower():
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return TransportResponse(status=status, headers=headers, json=data, text=text)
