"""HttpBackend + OAuth client-credentials (P3) — fully mocked transport.

No network: `urlopen` is injected. Proves request building, the bearer header,
JSON parsing, non-2xx pass-through (so 429/403 reach the enforcer as responses,
not exceptions), timeout -> TransportTimeout, read-only enforcement, and OAuth
token caching/refresh with a deterministic clock.
"""
from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from erp_harness.transport import ManualClock, TransportRequest, TransportTimeout
from erp_transport import HttpBackend, OAuthClientCredentials, OAuthError


class _Resp:
    def __init__(self, status, headers, body: bytes):
        self.status, self.headers, self._body = status, headers, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(payload, status=200, headers=None):
    h = {'Content-Type': 'application/json', **(headers or {})}
    return _Resp(status, h, json.dumps(payload).encode())


# --- HttpBackend ----------------------------------------------------------------

def test_get_builds_url_with_params_and_bearer_header():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['auth'] = req.headers.get('Authorization')
        captured['method'] = req.get_method()
        return _ok({'value': [{'number': 'K5-24SBC'}]})

    be = HttpBackend('https://bc.example/api/v2.0/',
                     token_provider=lambda: 'TKN', urlopen=fake_urlopen)
    resp = be.handle(TransportRequest('GET', 'items', {'$top': '2'}))
    assert resp.status == 200
    assert resp.json == {'value': [{'number': 'K5-24SBC'}]}
    assert captured['url'] == 'https://bc.example/api/v2.0/items?%24top=2'
    assert captured['auth'] == 'Bearer TKN'
    assert captured['method'] == 'GET'


def test_non_2xx_passes_through_as_response_not_exception():
    # 429 must reach the enforcer (for backoff), not raise.
    def fake_urlopen(req, timeout=None):
        from email.message import Message
        h = Message()
        h['Retry-After'] = '5'
        h['Content-Type'] = 'application/json'
        raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', h,
                                     io.BytesIO(b'{"error":"throttled"}'))

    be = HttpBackend('https://bc.example', urlopen=fake_urlopen)
    resp = be.handle(TransportRequest('GET', 'items'))
    assert resp.status == 429
    assert resp.headers.get('Retry-After') == '5'
    assert resp.json == {'error': 'throttled'}


def test_timeout_becomes_transport_timeout():
    def fake_urlopen(req, timeout=None):
        raise socket.timeout('timed out')

    be = HttpBackend('https://bc.example', urlopen=fake_urlopen)
    with pytest.raises(TransportTimeout):
        be.handle(TransportRequest('GET', 'items'))


def test_urlerror_wrapping_timeout_becomes_transport_timeout():
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(socket.timeout('slow'))

    be = HttpBackend('https://bc.example', urlopen=fake_urlopen)
    with pytest.raises(TransportTimeout):
        be.handle(TransportRequest('GET', 'items'))


def test_backend_refuses_write_methods():
    be = HttpBackend('https://bc.example', urlopen=lambda *a, **k: _ok({}))
    for m in ('POST', 'PATCH', 'PUT', 'DELETE'):
        with pytest.raises(PermissionError):
            be.handle(TransportRequest(m, 'items'))


# --- OAuth client credentials ---------------------------------------------------

def test_oauth_caches_token_until_near_expiry():
    calls = {'n': 0}

    def fake_urlopen(req, timeout=None):
        calls['n'] += 1
        return _Resp(200, {'Content-Type': 'application/json'},
                     json.dumps({'access_token': f'tok{calls["n"]}',
                                 'expires_in': 3600}).encode())

    clock = ManualClock()
    oauth = OAuthClientCredentials('https://login/token', 'id', 'secret',
                                   'scope', clock, urlopen=fake_urlopen,
                                   refresh_margin_s=60)
    assert oauth.token() == 'tok1'
    assert oauth.token() == 'tok1'        # cached, no second fetch
    assert calls['n'] == 1
    clock.advance(3600)                    # past expiry - margin
    assert oauth.token() == 'tok2'         # refreshed
    assert calls['n'] == 2


def test_oauth_missing_access_token_raises():
    def fake_urlopen(req, timeout=None):
        return _Resp(200, {'Content-Type': 'application/json'},
                     json.dumps({'error': 'invalid_client'}).encode())

    oauth = OAuthClientCredentials('https://login/token', 'id', 'bad', 'scope',
                                   ManualClock(), urlopen=fake_urlopen)
    with pytest.raises(OAuthError):
        oauth.token()
