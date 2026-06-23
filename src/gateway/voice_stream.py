"""Twilio Media Streams ingest — the durable Streaming-STT voice path.

The `<Gather>` path (runtime/app.py) is the simplest callable path: Twilio does
the ASR. The fidelity upgrade (the H1 call-capture finding) is Twilio Media
Streams -> AssemblyAI Streaming STT with catalog keyterms: Twilio opens a
WebSocket and streams raw call audio (G.711 mu-law, 8 kHz, 20 ms frames,
base64) to us; we forward it to a streaming ASR and run a gateway turn on each
finalized utterance. The gateway (and every gate) is unchanged — only the
transcription source improves.

This module is the PURE, CI-tested half: parse the Twilio frame envelope and
decode mu-law to linear PCM16. The live ASR socket lives in `asr_streaming.py`
and is credential-gated, never in CI.

Why hand-rolled mu-law: stdlib `audioop` was removed in Python 3.13+ (PEP 594),
so the G.711 decode is implemented here and unit-tested against reference
values — no third-party audio dependency.
"""
from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass

_BIAS = 0x84
_MULAW_TABLE: list[int] = []   # built once below: mu-law byte -> signed 16-bit PCM


def _build_mulaw_table() -> None:
    for b in range(256):
        u = ~b & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        _MULAW_TABLE.append(-sample if sign else sample)


_build_mulaw_table()


def mulaw_decode(payload: bytes) -> bytes:
    """G.711 mu-law bytes -> little-endian signed PCM16 bytes (2 bytes/sample).
    0xFF (mu-law zero) -> 0; 0x00 -> near max-negative; 0x80 -> near max-positive."""
    return struct.pack(f'<{len(payload)}h',
                       *(_MULAW_TABLE[b] for b in payload))


_SEG_END = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)


def _linear_to_mulaw(sample: int) -> int:
    """One PCM16 sample -> one G.711 mu-law byte (standard encoder)."""
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > 32635:
        sample = 32635          # clip
    sample += _BIAS
    exponent = 7
    for i, end in enumerate(_SEG_END):
        if sample <= end:
            exponent = i
            break
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def mulaw_encode(pcm16: bytes) -> bytes:
    """Little-endian signed PCM16 bytes -> G.711 mu-law bytes (the inverse of
    mulaw_decode, within mu-law quantization). For TTS audio headed to Twilio."""
    n = len(pcm16) // 2
    samples = struct.unpack(f'<{n}h', pcm16[:n * 2])
    return bytes(_linear_to_mulaw(s) for s in samples)


# --- Twilio Media Streams frame envelope ---------------------------------------

@dataclass(frozen=True)
class TwilioEvent:
    event: str                      # connected | start | media | stop | mark
    call_sid: str | None = None     # present on 'start'
    stream_sid: str | None = None   # present on 'start'/'media'
    mulaw: bytes = b''              # decoded base64 mu-law payload (media only)
    sequence: int | None = None
    track: str | None = None        # 'inbound' | 'outbound' (dual-channel calls)


def parse_twilio_event(raw: str | bytes) -> TwilioEvent:
    """Parse one Twilio Media Streams WebSocket message into a typed event.
    Reference shapes: https://www.twilio.com/docs/voice/media-streams ."""
    msg = json.loads(raw)
    event = msg.get('event', '')
    if event == 'start':
        start = msg.get('start', {})
        return TwilioEvent(event='start',
                           call_sid=start.get('callSid'),
                           stream_sid=start.get('streamSid'),
                           sequence=_int(msg.get('sequenceNumber')))
    if event == 'media':
        media = msg.get('media', {})
        payload = media.get('payload', '')
        return TwilioEvent(event='media',
                           stream_sid=msg.get('streamSid'),
                           mulaw=base64.b64decode(payload) if payload else b'',
                           sequence=_int(msg.get('sequenceNumber')),
                           track=media.get('track'))
    return TwilioEvent(event=event or 'unknown',
                       sequence=_int(msg.get('sequenceNumber')))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class TwilioMediaStream:
    """Accumulates a call's audio from Twilio frames. Holds the raw mu-law
    (forward this directly to AssemblyAI with encoding=pcm_mulaw, sample_rate=
    8000 — no resample) and can also yield decoded PCM16 for any ASR that wants
    linear audio. Tracks the CallSid so the stream maps to a gateway session."""

    def __init__(self) -> None:
        self.call_sid: str | None = None
        self.stream_sid: str | None = None
        self._mulaw = bytearray()
        self.closed = False

    def feed(self, raw: str | bytes) -> TwilioEvent:
        ev = parse_twilio_event(raw)
        if ev.event == 'start':
            self.call_sid = ev.call_sid
            self.stream_sid = ev.stream_sid
        elif ev.event == 'media':
            self._mulaw.extend(ev.mulaw)
        elif ev.event == 'stop':
            self.closed = True
        return ev

    @property
    def mulaw(self) -> bytes:
        return bytes(self._mulaw)

    @property
    def pcm16(self) -> bytes:
        return mulaw_decode(self.mulaw)


# --- outbound: audio back to the caller over the media stream -------------------

_FRAME_BYTES = 160   # 20 ms of mu-law @ 8 kHz


def twilio_media_messages(mulaw: bytes, stream_sid: str) -> list[str]:
    """Chunk outbound mu-law audio into Twilio `media` messages (base64, 20 ms
    frames) to play back to the caller — the TTS-reply leg of full duplex."""
    msgs = []
    for i in range(0, len(mulaw), _FRAME_BYTES):
        payload = base64.b64encode(mulaw[i:i + _FRAME_BYTES]).decode('ascii')
        msgs.append(json.dumps({'event': 'media', 'streamSid': stream_sid,
                                'media': {'payload': payload}}))
    return msgs


def twilio_mark(stream_sid: str, name: str) -> str:
    """A `mark` message — Twilio echoes it back when the audio finishes playing,
    so the bot knows the reply was heard before listening again."""
    return json.dumps({'event': 'mark', 'streamSid': stream_sid,
                       'mark': {'name': name}})
