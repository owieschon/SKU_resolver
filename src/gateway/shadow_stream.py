"""Live-audio bridge for shadow onboarding — real call audio -> transcript ->
continuous-improvement loop.

This is the piece that makes the ride-along and post-handoff sources run on real
calls instead of hand-fed text. A dual-channel call (Twilio Media Streams sends
`media.track` = 'inbound'/'outbound') is transcribed per track by a streaming
ASR; finalized turns are tagged by speaker (inbound=customer, outbound=rep),
ordered, and fed to `ContinuousImprovement.ingest_call` — so the rep's handling
of anything the tool missed is harvested as a self-heal.

Observe-only: this never speaks or acts on the call. Pure orchestration over the
injected ASR + improvement loop, so it's tested with a simulated ASR and no
audio. (The /shadow-stream endpoint wires it to a real WebSocket.)
"""
from __future__ import annotations

from gateway.voice_stream import parse_twilio_event
from gateway.voice import transcript_is_usable

_DEFAULT_TRACKS = {'inbound': 'customer', 'outbound': 'rep'}


class ShadowStreamBridge:
    """open_session(track) -> a streaming-ASR session (feed/drain/close).
    improvement: a ContinuousImprovement (or None to just transcribe)."""

    def __init__(self, open_session, improvement=None, *,
                 track_speaker: dict | None = None) -> None:
        self._open = open_session
        self._imp = improvement
        self._map = track_speaker or _DEFAULT_TRACKS
        self._sessions: dict[str, object] = {}
        self._turns: list[tuple] = []      # (sequence, speaker, text)
        self.finished = False

    def feed(self, raw):
        ev = parse_twilio_event(raw)
        if ev.event == 'media' and ev.mulaw:
            track = ev.track or 'inbound'
            sess = self._sessions.get(track)
            if sess is None:
                sess = self._open(track)
                self._sessions[track] = sess
            sess.feed(ev.mulaw)
            speaker = self._map.get(track, 'customer')
            for t in sess.drain():
                if transcript_is_usable(t):
                    self._turns.append((ev.sequence or 0, speaker, t.text))
        elif ev.event == 'stop':
            self.finish()
        return ev

    def finish(self) -> list:
        """Drain, order by sequence, and feed the reconstructed transcript to
        the improvement loop (ride-along: harvests the rep's resolutions)."""
        if self.finished:
            return []
        self.finished = True
        for sess in self._sessions.values():
            for t in sess.drain():
                if transcript_is_usable(t):
                    self._turns.append((10 ** 9, self._speaker_of(sess), t.text))
            sess.close()
        turns = [(sp, tx) for _, sp, tx in sorted(self._turns, key=lambda x: x[0])]
        if turns and self._imp is not None:
            self._imp.ingest_call(turns)
        return turns

    def _speaker_of(self, sess) -> str:
        for track, s in self._sessions.items():
            if s is sess:
                return self._map.get(track, 'customer')
        return 'customer'
