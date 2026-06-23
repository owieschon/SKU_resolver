"""Twilio webhook signature validation — fail-open in dev, fail-closed in prod.

Fault-injection check: we plant a spoofed/tampered request and prove the
validator rejects it when a token is configured, AND prove a genuine Twilio
signature passes.
"""
from __future__ import annotations

from runtime import twilio_sig

TOKEN = '12345678901234567890123456789012'  # example auth token (not a secret)
URL = 'https://example.ngrok.app/voice'
PARAMS = {'CallSid': 'CA-abc', 'SpeechResult': 'is K5-24SBC in stock'}


def _good_sig():
    return twilio_sig.expected_signature(TOKEN, URL, PARAMS)


def test_genuine_signature_validates():
    assert twilio_sig.validate(URL, PARAMS, _good_sig(), auth_token=TOKEN)


def test_tampered_params_rejected():
    tampered = dict(PARAMS, SpeechResult='how much is K5-24SBC')
    assert not twilio_sig.validate(URL, tampered, _good_sig(), auth_token=TOKEN)


def test_tampered_url_rejected():
    other = 'https://evil.example/voice'
    assert not twilio_sig.validate(other, PARAMS, _good_sig(), auth_token=TOKEN)


def test_missing_signature_rejected_when_token_present():
    assert not twilio_sig.validate(URL, PARAMS, '', auth_token=TOKEN)


def test_fail_open_when_no_token_configured():
    # Local/ngrok dev: no token => validation disabled (signature ignored).
    assert twilio_sig.validate(URL, PARAMS, 'anything', auth_token='')
    assert twilio_sig.validate(URL, PARAMS, '', auth_token='')


def test_param_order_independence():
    # Twilio sorts params by key; dict insertion order must not matter.
    reordered = {'SpeechResult': PARAMS['SpeechResult'],
                 'CallSid': PARAMS['CallSid']}
    assert twilio_sig.validate(URL, reordered, _good_sig(), auth_token=TOKEN)


def test_signature_enforced_reflects_env(monkeypatch):
    monkeypatch.delenv('TWILIO_AUTH_TOKEN', raising=False)
    assert twilio_sig.signature_enforced() is False
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', TOKEN)
    assert twilio_sig.signature_enforced() is True
