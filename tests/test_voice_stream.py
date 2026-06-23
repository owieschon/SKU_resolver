"""Streaming voice path (P3): Twilio Media Streams parsing + mu-law decode +
the simulated streaming-ASR bridge through the REAL gateway.

Pure/CI: no network, no real audio, no credentials. The live AssemblyAI v3
client is covered by the credential-gated smoke (test_live_voice_smoke.py).
"""
from __future__ import annotations

import base64
import json
import struct

from gateway_fixtures import build_gateway

from gateway import (
    SimulatedStreamingASR,
    SimulatedTTS,
    Transcript,
    TwilioMediaStream,
    mulaw_decode,
    mulaw_encode,
    parse_twilio_event,
    run_stream_turns,
    twilio_mark,
    twilio_media_messages,
)

# --- mu-law decode (reference values; audioop is gone in 3.14) ------------------

def test_mulaw_decode_reference_points():
    assert mulaw_decode(b'\xff') == (0).to_bytes(2, 'little', signed=True)
    assert int.from_bytes(mulaw_decode(b'\x00'), 'little', signed=True) == -32124
    assert int.from_bytes(mulaw_decode(b'\x80'), 'little', signed=True) == 32124
    # two bytes of PCM16 per mu-law byte
    assert len(mulaw_decode(b'\x01\x02\x03')) == 6


def test_mulaw_encode_roundtrip_within_quantization():
    # encode->decode should preserve sign and stay close (mu-law is lossy).
    samples = [0, 100, -100, 1000, -1000, 8000, -8000, 30000, -30000]
    pcm = struct.pack(f'<{len(samples)}h', *samples)
    out = struct.unpack(f'<{len(samples)}h', mulaw_decode(mulaw_encode(pcm)))
    assert out[0] == 0                                   # silence stays silence
    for orig, got in zip(samples, out):
        assert (orig >= 0) == (got >= 0) or orig == 0    # sign preserved
        assert abs(abs(got) - abs(orig)) <= abs(orig) * 0.10 + 256  # ~quantization
    # one mu-law byte per sample
    assert len(mulaw_encode(pcm)) == len(samples)


# --- Twilio frame envelope ------------------------------------------------------

def _media_frame(payload_bytes: bytes, stream_sid='MZ1', seq=1):
    return json.dumps({'event': 'media', 'streamSid': stream_sid,
                       'sequenceNumber': str(seq),
                       'media': {'payload': base64.b64encode(payload_bytes).decode()}})


def test_parse_start_and_media_events():
    start = json.dumps({'event': 'start', 'sequenceNumber': '1',
                        'start': {'callSid': 'CA9', 'streamSid': 'MZ1'}})
    ev = parse_twilio_event(start)
    assert ev.event == 'start' and ev.call_sid == 'CA9' and ev.stream_sid == 'MZ1'

    ev2 = parse_twilio_event(_media_frame(b'\xff' * 160))
    assert ev2.event == 'media' and ev2.mulaw == b'\xff' * 160


def test_media_stream_accumulates_audio_and_tracks_call():
    stream = TwilioMediaStream()
    stream.feed(json.dumps({'event': 'start',
                            'start': {'callSid': 'CA9', 'streamSid': 'MZ1'}}))
    stream.feed(_media_frame(b'\xff' * 160, seq=2))
    stream.feed(_media_frame(b'\x00' * 160, seq=3))
    stream.feed(json.dumps({'event': 'stop'}))
    assert stream.call_sid == 'CA9'
    assert len(stream.mulaw) == 320          # two 20ms frames
    assert len(stream.pcm16) == 640          # decoded PCM16
    assert stream.closed is True


# --- the bridge: stream -> simulated ASR turn -> gateway (gates intact) ----------

def test_stream_bridge_runs_a_gateway_turn_with_readback_gate(tmp_path):
    gw, sessions, _, _ = build_gateway(tmp_path)
    stream = TwilioMediaStream()
    stream.feed(json.dumps({'event': 'start',
                            'start': {'callSid': 'CA-stream-1', 'streamSid': 'MZ1'}}))
    stream.feed(_media_frame(b'\xff' * 160, seq=2))
    stream.feed(_media_frame(b'\xff' * 160, seq=3))

    asr = SimulatedStreamingASR(
        script=[Transcript(text='K5-24SBC', confidence=0.9)],
        bytes_per_turn=160)
    replies = run_stream_turns(stream, asr, gw, sessions)

    assert replies                              # a turn ran from the stream
    # VOICE channel still applies the discriminating-readback gate (#11):
    # the first turn reads the part back / asks, it does not silently act.
    assert 'K5' in replies[0] or '?' in replies[0]


def test_outbound_media_frame_builder():
    mulaw = b'\xff' * 400          # 2.5 frames of 160 bytes
    frames = twilio_media_messages(mulaw, 'MZ9')
    assert len(frames) == 3       # 160 + 160 + 80
    import base64
    import json
    f0 = json.loads(frames[0])
    assert f0['event'] == 'media' and f0['streamSid'] == 'MZ9'
    assert base64.b64decode(f0['media']['payload']) == b'\xff' * 160
    assert json.loads(twilio_mark('MZ9', 'reply-1'))['mark']['name'] == 'reply-1'


def test_voice_persona_accent_selects_voice_and_overrides():
    from gateway import ACCENT_VOICES, VoicePersona
    p = VoicePersona(name='Sam', accent='southern')
    assert p.resolved_voice_id() == ACCENT_VOICES['southern']
    assert 'Sam' in p.opening()
    # explicit voice id overrides accent
    assert VoicePersona(accent='midwest', voice_id='xyz').resolved_voice_id() == 'xyz'
    # unknown accent falls back to standard
    assert VoicePersona(accent='martian').accent == 'standard'
    # explicit greeting overrides the generated one
    assert VoicePersona(greeting='Hello there.').opening() == 'Hello there.'


def test_build_persona_from_env(monkeypatch):
    from runtime.config import build_persona
    monkeypatch.setenv('SKU_VOICE_NAME', 'Dale')
    monkeypatch.setenv('SKU_VOICE_ACCENT', 'northeast')
    p = build_persona()
    assert p.name == 'Dale' and p.accent == 'northeast'


def test_simulated_tts_returns_mulaw_sized_to_text():
    tts = SimulatedTTS()
    short, long = tts.synthesize('hi'), tts.synthesize('a much longer reply ' * 5)
    assert len(long) > len(short) > 0
    assert set(short) == {0xFF}    # mu-law silence


def test_elevenlabs_tts_builds_request_without_network(monkeypatch):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'xi-test')
    from runtime.tts_adapters import ElevenLabsTTS
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['key'] = req.headers.get('Xi-api-key')
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'\xff\xff\xff'
        return _R()

    tts = ElevenLabsTTS(urlopen=fake_urlopen)
    audio = tts.synthesize('hello')
    assert audio == b'\xff\xff\xff'
    assert 'output_format=ulaw_8000' in captured['url']   # Twilio-native, no resample
    assert captured['key'] == 'xi-test'


def test_stream_bridge_skips_unusable_transcript(tmp_path):
    gw, sessions, _, _ = build_gateway(tmp_path)
    stream = TwilioMediaStream()
    stream.feed(json.dumps({'event': 'start',
                            'start': {'callSid': 'CA-stream-2', 'streamSid': 'MZ2'}}))
    stream.feed(_media_frame(b'\xff' * 160, seq=2))
    # Below the confidence floor -> not usable -> no turn attempted.
    asr = SimulatedStreamingASR(
        script=[Transcript(text='garbled', confidence=0.1)],
        bytes_per_turn=160)
    assert run_stream_turns(stream, asr, gw, sessions) == []
