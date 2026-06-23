"""G6 — voice connector: SimulatedASR seam, confidence floor, keyterms, and
the degraded-transcript -> readback (never silent ID) behavior end-to-end.
"""
from __future__ import annotations

from gateway_fixtures import build_gateway

from gateway import (
    Channel,
    SimulatedASR,
    Transcript,
    keyterms_from_catalog,
    transcript_is_usable,
)
from gateway.voice import CONFIDENCE_FLOOR


def test_simulated_asr_is_deterministic():
    asr = SimulatedASR(script={'call1': Transcript('K5-24SBC', 0.92)})
    assert asr.transcribe('call1').text == 'K5-24SBC'
    assert asr.transcribe('unknown').confidence == 0.0   # graceful miss


def test_confidence_floor_gates_low_quality():
    assert transcript_is_usable(Transcript('K5-24SBC', CONFIDENCE_FLOOR + 0.1))
    assert not transcript_is_usable(Transcript('mumble', CONFIDENCE_FLOOR - 0.1))
    assert not transcript_is_usable(Transcript('', 0.99))   # empty never usable


def test_keyterms_come_from_catalog(tmp_path):
    gw, *_ = build_gateway(tmp_path)
    terms = keyterms_from_catalog(gw.catalog, limit=50)
    assert len(terms) == 50
    assert all(gw.catalog.is_canonical(t) for t in terms)


def test_degraded_transcript_routes_to_readback_not_silent_id(tmp_path):
    """The SS10.5 failure class, blocked by protocol: a usable-but-imperfect
    voice transcript that resolves still requires a readback before it counts."""
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = sessions.open('S', 'c')
    asr = SimulatedASR(script={
        'good': Transcript('5 inch chrome curved 24 long SB', 0.88),
    })
    t = asr.transcribe('good')
    assert transcript_is_usable(t)
    r = gw.turn('S', tok, t.text, channel=Channel.VOICE)
    assert r.needs_confirmation and r.availability is None   # readback first
