"""Text-to-speech seam for the voice reply leg.

TTS is a non-binding I/O leg: the gateway has already decided WHAT to say (every
gate applied); TTS only renders those exact words to audio. So, like ASR, it is
a swappable seam — CI runs `SimulatedTTS` (deterministic bytes, no network), a
real adapter renders actual speech. It returns G.711 mu-law @ 8 kHz, the Twilio
telephony wire format, so no resampling sits between TTS and the call.

The hosted-speech-agent path was rejected for the same reason as the ASR side:
it would let a model speak unmandated words. Here the words are fixed by the
deterministic gateway before TTS ever runs — TTS cannot change them.
"""
from __future__ import annotations

from typing import Protocol

TELEPHONY_RATE = 8000


class TTS(Protocol):
    def synthesize(self, text: str) -> bytes:   # mu-law @ 8 kHz
        ...


class SimulatedTTS:
    """Deterministic CI TTS: emits mu-law silence sized to the text (~60 ms per
    character, capped) so the reply leg is exercised end-to-end without audio
    or network. Proves framing/playback wiring; not speech."""

    name = 'simulated'

    def __init__(self, ms_per_char: int = 60, max_ms: int = 8000) -> None:
        self._ms_per_char = ms_per_char
        self._max_ms = max_ms

    def synthesize(self, text: str) -> bytes:
        ms = min(self._max_ms, max(200, len(text or '') * self._ms_per_char))
        n = int(TELEPHONY_RATE * ms / 1000)
        return b'\xff' * n          # 0xFF == mu-law silence


# The live HTTP-based TTS adapter (ElevenLabs, ulaw_8000) lives in
# runtime/tts_adapters.py — the gateway package stays free of any network stack
# (test_gateway_purity), so HTTP adapters live in the deployment layer.
