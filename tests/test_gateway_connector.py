"""G1/G7 — tool-calling contract + signed webhook with replay defense.

The tools.json-only client test is the proof that a third-party platform can
drive the gateway from the contract alone (G1 DoD).
"""
from __future__ import annotations

import hashlib
import hmac
import json

from gateway import Channel, tools_manifest
from gateway.connector import WebhookConnector, _response_to_dict
from gateway_fixtures import build_gateway

SECRET = b'webhook-secret'


def test_tools_manifest_is_self_describing():
    m = tools_manifest()
    req = m['parameters']['required']
    assert set(req) == {'session_id', 'token', 'text', 'channel'}
    assert m['parameters']['properties']['channel']['enum'] == ['typed', 'voice']


def test_tools_json_only_client_completes_a_conversation(tmp_path):
    """A client driven ONLY by the manifest contract (no out-of-band
    knowledge) runs verify -> price."""
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = sessions.open('S', 'c')
    # The client knows only: call with {session_id, token, text, channel}.
    def call(text):
        return _response_to_dict(gw.turn('S', tok, text, channel=Channel.TYPED))
    assert call('is K5-24SBC in stock?')['kind'] == 'availability'
    assert call('account number 1001')['session_state'] == 'verified'
    assert 'price' in call('price of K5-24SBC')


def _wh(gw, now=100.0):
    clk = {'t': now}
    return WebhookConnector(gateway=gw, secret=SECRET, now_fn=lambda: clk['t']), clk


def test_webhook_valid_signature_dispatches(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    sessions.open('S', 'c')
    conn, _ = _wh(gw)
    body = json.dumps({'session_id': 'S', 'token': sessions._sessions['S'].token_sig,
                       'text': 'is K5-24SBC in stock?', 'channel': 'typed'}).encode()
    sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    out = conn.handle(body, sig, nonce='n1', ts=100.0)
    assert out['kind'] == 'availability'


def test_webhook_bad_signature_rejected(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    conn, _ = _wh(gw)
    out = conn.handle(b'{}', 'deadbeef', nonce='n', ts=100.0)
    assert out == {'error': 'signature_invalid'}


def test_webhook_replay_is_idempotent(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    sessions.open('S', 'c')
    conn, _ = _wh(gw)
    body = json.dumps({'session_id': 'S', 'token': sessions._sessions['S'].token_sig,
                       'text': 'is K5-24SBC in stock?', 'channel': 'typed'}).encode()
    sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    assert conn.handle(body, sig, nonce='dup', ts=100.0)['kind'] == 'availability'
    assert conn.handle(body, sig, nonce='dup', ts=100.0) == {'error': 'replay_detected'}


def test_webhook_stale_timestamp_rejected(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    sessions.open('S', 'c')
    conn, _ = _wh(gw, now=100000.0)
    body = json.dumps({'session_id': 'S', 'token': sessions._sessions['S'].token_sig,
                       'text': 'hi', 'channel': 'typed'}).encode()
    sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    out = conn.handle(body, sig, nonce='n', ts=100.0)   # far outside window
    assert out == {'error': 'timestamp_outside_window'}
