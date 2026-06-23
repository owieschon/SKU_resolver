"""Streaming ASR seam — Simulated (CI) and AssemblyAI v3 (live, credential-gated).

The batch `ASR` in voice.py maps an audio ref to a transcript. Real telephony is
streaming: audio arrives continuously and the ASR emits *turns* (finalized
utterances). This seam models that:

  StreamingASR.open(...) -> StreamingSession
  session.feed(audio_chunk)          # push raw audio frames as they arrive
  session.drain() -> list[Transcript]  # finalized turns since the last drain
  session.close()

CI runs `SimulatedStreamingASR` (deterministic, no network). Production uses
`AssemblyAIStreamingASR` — a thin client over the documented v3 protocol
(wss://streaming.assemblyai.com/v3/ws, Authorization: <key>, binary audio,
server 'Turn' messages with end_of_turn). The key loads from ASSEMBLYAI_API_KEY
at runtime; nothing credential-bearing is in the repo. The live client is
exercised only by the credential-gated smoke, never CI.

The seam keeps the binding decision OUT of the ASR: a finalized transcript is
fed to the gateway, which applies the discriminating-readback gate (#11) before
acting. This is the durable choice over a hosted speech-to-speech agent — the
deterministic gateway stays the brain (see VOICE_RUNBOOK / DECISION_LOG).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from gateway.voice import Transcript

# Twilio telephony defaults: G.711 mu-law, 8 kHz.
TELEPHONY_SAMPLE_RATE = 8000
TELEPHONY_ENCODING = 'pcm_mulaw'

# RFC 3986 unreserved characters; everything else in a value is percent-encoded.
_UNRESERVED = frozenset(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~')


def _pct(value: str) -> str:
    return ''.join(c if c in _UNRESERVED else
                   ''.join(f'%{b:02X}' for b in c.encode('utf-8'))
                   for c in value)


def _querystring(params: dict) -> str:
    """Minimal application/x-www-form-urlencoded builder (stdlib-only; the
    gateway package must not import urllib at module scope — purity guard)."""
    return '&'.join(f'{_pct(str(k))}={_pct(str(v))}' for k, v in params.items())


class StreamingSession(Protocol):
    def feed(self, audio: bytes) -> None: ...
    def drain(self) -> list[Transcript]: ...
    def close(self) -> None: ...


class StreamingASR(Protocol):
    def open(self, *, sample_rate: int, encoding: str,
             keyterms: list[str] | None = None) -> StreamingSession: ...


def parse_turn_message(raw) -> Transcript | None:
    """Map one AssemblyAI v3 server message to a finalized Transcript, or None.

    Pure (no socket) so it is unit-tested against synthetic payloads instead of
    only running live. Returns a Transcript ONLY for a finalized turn
    (type 'Turn' with end_of_turn=true); partials, 'Begin'/'Termination', and
    malformed frames return None. Confidence is the mean word confidence, or
    1.0 when the server sends no per-word scores."""
    import json
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get('type') != 'Turn' or not msg.get('end_of_turn'):
        return None
    words = msg.get('words') or []
    confs = [w.get('confidence', 0.0) for w in words if isinstance(w, dict)]
    conf = sum(confs) / len(confs) if confs else 1.0
    return Transcript(text=msg.get('transcript', ''), confidence=conf,
                      is_final=True)


# --- Simulated (CI) -------------------------------------------------------------

@dataclass
class _SimSession:
    """Emits a scripted finalized transcript once enough audio has been fed,
    so the bridge (frames -> turn) is testable with no real audio/network."""
    script: list[Transcript]
    bytes_per_turn: int
    _buf: int = 0
    _emitted: int = 0

    def feed(self, audio: bytes) -> None:
        self._buf += len(audio)

    def drain(self) -> list[Transcript]:
        out: list[Transcript] = []
        while (self._emitted < len(self.script)
               and self._buf >= self.bytes_per_turn * (self._emitted + 1)):
            out.append(self.script[self._emitted])
            self._emitted += 1
        return out

    def close(self) -> None:
        pass


@dataclass
class SimulatedStreamingASR:
    """Deterministic streaming ASR for CI. `script` is the sequence of finalized
    turns to emit; one is released for every `bytes_per_turn` of fed audio."""
    script: list[Transcript] = field(default_factory=list)
    bytes_per_turn: int = 320   # ~one 20ms PCM16 frame at 8kHz

    def open(self, *, sample_rate: int, encoding: str,
             keyterms: list[str] | None = None) -> StreamingSession:
        return _SimSession(script=list(self.script),
                           bytes_per_turn=self.bytes_per_turn)


# --- AssemblyAI v3 (live, credential-gated) -------------------------------------

class AssemblyAIStreamingASR:
    """Live streaming ASR over AssemblyAI Universal-Streaming v3.

    Protocol (per AssemblyAI docs, 2026-06):
      - connect wss://streaming.assemblyai.com/v3/ws?sample_rate=..&encoding=..
      - header Authorization: <API_KEY>   (NO 'Bearer'; server-side only)
      - send raw audio as BINARY frames
      - receive JSON; type 'Turn' carries 'transcript', 'end_of_turn' (bool),
        and 'words'[].confidence; finalize on end_of_turn
      - terminate with {"type": "Terminate"}

    Needs the `[voice]` extra (websocket-client). Key from ASSEMBLYAI_API_KEY.
    """
    BASE_URL = 'wss://streaming.assemblyai.com/v3/ws'

    def __init__(self, api_key: str | None = None, *,
                 speech_model: str = 'u3-rt-pro') -> None:
        import os
        self._key = api_key or os.environ.get('ASSEMBLYAI_API_KEY')
        if not self._key:
            raise RuntimeError('ASSEMBLYAI_API_KEY not set — live ASR only.')
        self._speech_model = speech_model

    def open(self, *, sample_rate: int = TELEPHONY_SAMPLE_RATE,
             encoding: str = TELEPHONY_ENCODING,
             keyterms: list[str] | None = None) -> StreamingSession:
        try:
            import websocket   # websocket-client ([voice] extra)
        except ImportError as e:   # pragma: no cover - env-dependent
            raise RuntimeError("live streaming ASR needs the [voice] extra: "
                               "pip install '.[voice]'") from e
        params = {'sample_rate': sample_rate, 'encoding': encoding,
                  'speech_model': self._speech_model, 'format_turns': 'true'}
        if keyterms:
            # AssemblyAI v3 requires keyterms_prompt as a JSON array (error 3006
            # on a bare comma-joined string — caught by the live smoke).
            import json
            params['keyterms_prompt'] = json.dumps(list(keyterms[:100]))
        # Build the query string with stdlib only (the gateway package is
        # import-pure: no urllib at module scope — see test_gateway_purity).
        url = f'{self.BASE_URL}?{_querystring(params)}'
        # Use a real CA bundle for TLS verification — without it the handshake
        # fails on interpreters that don't use the system trust store (e.g.
        # python.org macOS builds). certifi ships with the [voice] stack.
        sslopt = None
        try:
            import certifi
            sslopt = {'ca_certs': certifi.where()}
        except ImportError:   # pragma: no cover
            pass
        ws = websocket.create_connection(
            url, header=[f'Authorization: {self._key}'], sslopt=sslopt)
        return _AaiSession(ws)


class _AaiSession:
    def __init__(self, ws) -> None:
        self._ws = ws
        self._ws.settimeout(0.01)

    def feed(self, audio: bytes) -> None:
        import websocket
        self._ws.send_binary(audio)
        # opportunistically drain so the socket buffer doesn't grow unbounded
        self._pump()

    def _pump(self) -> list[Transcript]:
        import websocket
        out: list[Transcript] = []
        while True:
            try:
                raw = self._ws.recv()
            except (websocket.WebSocketTimeoutException, BlockingIOError):
                break
            except Exception:   # connection closed / other
                break
            if not raw:
                break
            t = parse_turn_message(raw)   # pure, unit-tested
            if t is not None:
                out.append(t)
        self._pending = getattr(self, '_pending', [])
        self._pending.extend(out)
        return out

    def drain(self) -> list[Transcript]:
        self._pump()
        pending = getattr(self, '_pending', [])
        self._pending = []
        return pending

    def close(self) -> None:
        import json
        try:
            self._ws.send(json.dumps({'type': 'Terminate'}))
        except Exception:
            pass
        try:
            self._ws.close()
        except Exception:
            pass


# --- the bridge: finalized transcripts -> gateway turns -------------------------

def run_stream_turns(stream, asr: StreamingASR, gateway, sessions, *,
                     keyterms: list[str] | None = None):
    """Drive gateway turns from a Twilio media stream via a streaming ASR.

    Pure orchestration (no socket I/O of its own): caller feeds raw Twilio
    frames into `stream` and audio into the ASR session; this yields the
    gateway's reply text for each finalized, usable transcript. Every reply
    still passes through the gateway's gates — the ASR only transcribes.
    """
    from gateway import Channel
    from gateway.voice import transcript_is_usable

    session = asr.open(sample_rate=TELEPHONY_SAMPLE_RATE,
                       encoding=TELEPHONY_ENCODING, keyterms=keyterms)
    sid = stream.call_sid
    token = sessions.open(sid, f'twilio:{sid}')
    replies: list[str] = []
    session.feed(stream.mulaw)
    for t in session.drain():
        if not transcript_is_usable(t):
            continue
        resp = gateway.turn(sid, token, t.text, channel=Channel.VOICE)
        replies.append(resp.text)
    session.close()
    return replies
