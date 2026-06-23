"""Live voice smoke — REAL AssemblyAI v3 streaming, credential-gated.

Proves the AssemblyAIStreamingASR adapter speaks the real protocol: connects,
streams audio, parses server messages, terminates cleanly. NOT part of CI —
skipped unless ASSEMBLYAI_API_KEY is set and the [voice] extra is installed.

Run on demand:
    ASSEMBLYAI_API_KEY=... pytest tests/test_live_voice_smoke.py -v

Cost: AssemblyAI streaming is billed by audio-seconds; this feeds ~1 second of
audio, fractions of a cent. We assert the session opens, accepts audio, and the
drain/close path runs without error. Drop a real spoken-word PCM clip in to
assert an actual transcript.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get('ASSEMBLYAI_API_KEY'),
    reason='ASSEMBLYAI_API_KEY not set — live voice smoke is opt-in, not CI.')


def test_assemblyai_streaming_connects_and_terminates():
    pytest.importorskip('websocket')   # [voice] extra
    from gateway.asr_streaming import AssemblyAIStreamingASR

    asr = AssemblyAIStreamingASR()
    # PCM16 @ 16k is the documented default encoding; 1s of silence.
    session = asr.open(sample_rate=16000, encoding='pcm_s16le')
    try:
        one_second = b'\x00\x00' * 16000
        for i in range(0, len(one_second), 1600):   # ~50ms chunks
            session.feed(one_second[i:i + 1600])
        finals = session.drain()
        assert isinstance(finals, list)   # protocol parsed without error
    finally:
        session.close()


@pytest.mark.skipif(not os.environ.get('ELEVENLABS_API_KEY'),
                    reason='ELEVENLABS_API_KEY not set — live TTS smoke is opt-in.')
def test_elevenlabs_tts_synthesizes_real_mulaw_audio():
    from runtime.tts_adapters import ElevenLabsTTS
    audio = ElevenLabsTTS().synthesize('Your part is in stock.')
    # ElevenLabs returns G.711 mu-law @ 8kHz (ulaw_8000) — real, non-trivial audio
    assert isinstance(audio, bytes) and len(audio) > 500


def test_assemblyai_streaming_accepts_telephony_mulaw():
    pytest.importorskip('websocket')
    from gateway.asr_streaming import AssemblyAIStreamingASR

    asr = AssemblyAIStreamingASR()
    # The Twilio path: forward mu-law @ 8k directly, no resample.
    session = asr.open(sample_rate=8000, encoding='pcm_mulaw',
                       keyterms=['K5-24SBC', 'curved stack'])
    try:
        silence_mulaw = b'\xff' * 8000      # ~1s of mu-law silence
        for i in range(0, len(silence_mulaw), 160):
            session.feed(silence_mulaw[i:i + 160])
        assert isinstance(session.drain(), list)
    finally:
        session.close()
