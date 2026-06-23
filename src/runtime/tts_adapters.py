"""Live TTS adapters (deployment layer — HTTP allowed here, not in gateway).

ElevenLabs renders the gateway's already-decided reply text to G.711 mu-law @
8 kHz (`output_format=ulaw_8000`) — Twilio-native, no resampling. Credential-
gated (ELEVENLABS_API_KEY); `urlopen` is injectable so request building is
unit-tested without network. The live byte fetch runs only behind the key.
"""
from __future__ import annotations

import json
import urllib.request


def _certifi_urlopen(req, timeout=30):
    """urlopen with a certifi CA bundle so TLS verifies on interpreters without
    a system trust store (same fix as the streaming ASR)."""
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:   # pragma: no cover
        ctx = None
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


class ElevenLabsTTS:
    name = 'elevenlabs_v1'
    _URL = ('https://api.elevenlabs.io/v1/text-to-speech/{voice}'
            '?output_format=ulaw_8000')

    def __init__(self, api_key: str | None = None, *,
                 voice_id: str = 'EXAVITQu4vr4xnSDxMaL',
                 model_id: str = 'eleven_turbo_v2_5', urlopen=None) -> None:
        import os
        self._key = api_key or os.environ.get('ELEVENLABS_API_KEY')
        if not self._key:
            raise RuntimeError('ELEVENLABS_API_KEY not set — live TTS only.')
        # Fail loud rather than speak in a wrong/placeholder voice: the persona
        # accent presets are placeholders the operator must replace with real
        # provider voice ids (per accent) before live TTS will run.
        if voice_id.startswith('voice-'):
            raise RuntimeError(
                f'persona voice id {voice_id!r} is a placeholder — set a real '
                f'ElevenLabs voice id via SKU_VOICE_ID or the per-accent presets '
                f'(gateway.persona.ACCENT_VOICES) before enabling live TTS.')
        self._voice = voice_id
        self._model = model_id
        self._urlopen = urlopen or _certifi_urlopen

    def build_request(self, text: str) -> urllib.request.Request:
        body = json.dumps({'text': text, 'model_id': self._model}).encode()
        return urllib.request.Request(
            self._URL.format(voice=self._voice), data=body, method='POST',
            headers={'xi-api-key': self._key,
                     'Content-Type': 'application/json',
                     'Accept': 'audio/basic'})

    def synthesize(self, text: str) -> bytes:
        with self._urlopen(self.build_request(text), timeout=30) as resp:
            return resp.read()      # already mu-law @ 8 kHz
