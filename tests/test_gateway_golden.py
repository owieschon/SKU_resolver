"""Golden conversations — the tone + behavior spec for the gateway (each a
named scenario from the spec §4). These pin plain-language output and the
gate behavior an operator would walk through.
"""
from __future__ import annotations

from gateway import Channel
from gateway_fixtures import build_gateway


def _open(gw, sessions, sid='S'):
    return sessions.open(sid, f'chan-{sid}')


# G-conv-1: availability without verification (allowed)
def test_availability_ungated(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, 'is K5-24SBC in stock?', channel=Channel.TYPED)
    assert r.kind == 'availability' and r.availability is not None
    assert r.availability.basis                # provenance present
    assert r.availability.catalog_version
    assert 'stock' in r.text.lower()


# G-conv-2: pricing attempt unverified -> refused + offer, never a price
def test_pricing_unverified_refused_with_offer(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, 'how much is K5-24SBC?', channel=Channel.TYPED)
    assert r.kind == 'pricing' and r.price is None
    assert r.refused == 'pricing_unauthorized'
    assert 'verify' in r.text.lower()          # the offer
    assert journal.events(__import__('gateway').EventType.PRICING_REFUSED)


# G-conv-3: verify by number -> pricing disclosed + journaled
def test_verify_by_number_then_price(tmp_path):
    from gateway import EventType
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    gw.turn('S', tok, 'my account number is 1001', channel=Channel.TYPED)
    r = gw.turn('S', tok, 'how much is K5-24SBC?', channel=Channel.TYPED)
    assert r.kind == 'pricing' and r.price is not None
    assert r.price.source == 'verified_account_self'
    disclosures = journal.events(EventType.PRICING_DISCLOSED)
    assert disclosures and disclosures[-1]['account_id'] == '1001'


# G-conv-4: ambiguous name -> disambiguation -> verify
def test_ambiguous_name_disambiguation(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, 'my account name is TRUCK PARTS', channel=Channel.TYPED)
    assert r.needs_confirmation and 'which one' in r.text.lower()


# G-conv-5: wrong-SKU voice readback corrected
def test_voice_readback_correction(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, 'I need a five inch chrome curved stack twenty four long SB',
                channel=Channel.VOICE)
    assert r.needs_confirmation and r.kind == 'identify'   # readback first
    # caller affirms a discriminating attribute -> proceeds to availability
    r2 = gw.turn('S', tok, 'yes the chrome one', channel=Channel.VOICE)
    assert r2.kind == 'availability' and r2.availability is not None


# G-conv-6: out-of-horizon accurate refusal (no guessed date)
def test_out_of_horizon_honest_refusal(tmp_path):
    from datetime import datetime
    from gateway_fixtures import NY
    # An OOS item with a long lead, ordered near the calendar edge.
    gw, sessions, journal, _ = build_gateway(
        tmp_path, now=datetime(2027, 12, 1, 10, tzinfo=NY))
    # find an OOS sku
    oos = next(s for s, r in gw.inventory.items()
               if r.qty_on_hand == 0 and r.lead_time_days and r.lead_time_days >= 20
               and gw.catalog.is_canonical(s))
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, oos, channel=Channel.TYPED)   # bare SKU, verbatim
    assert r.availability is not None
    assert r.availability.basis == 'beyond_calendar_horizon'
    assert "can't quote" in r.text.lower()      # accurate, not a guessed date


# G-conv-7: anaphora — "the K5 one" resolves against session context
def test_anaphora_resolves_recent_sku(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    gw.turn('S', tok, 'is K5-24SBC in stock?', channel=Channel.TYPED)  # seeds context
    r = gw.turn('S', tok, 'and is that one available too?', channel=Channel.TYPED)
    # anaphora -> confirm-first, naming the remembered SKU
    assert 'K5-24SBC' in r.text


# Regression: bugs found on a live call (2026-06-07) ---------------------------

def test_pricing_uses_remembered_part_after_confirmation(tmp_path):
    # The live-call showstopper: verify, identify+confirm a part, then ask the
    # price WITHOUT renaming it -> must price the remembered part, not escalate.
    from gateway import Channel
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    gw.turn('S', tok, 'my account number is 1001', channel=Channel.VOICE)
    gw.turn('S', tok, 'K5-24SBC', channel=Channel.VOICE)              # readback
    gw.turn('S', tok, 'yes', channel=Channel.VOICE)                  # confirm
    r = gw.turn('S', tok, "what's the price?", channel=Channel.VOICE)
    assert r.kind == 'pricing' and r.price is not None                # NOT escalate
    assert r.price.sku == 'K5-24SBC'


def test_bare_yes_does_not_escalate(tmp_path):
    from gateway import Channel
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, 'Yes.', channel=Channel.VOICE)
    assert r.kind != 'escalate'                                       # was: escalate


def test_voice_readback_states_decoded_attributes_not_a_question(tmp_path):
    # The live complaint: the agent ASKED for diameter/finish that the SKU
    # already encodes. Now it STATES the decoded attributes for a yes/no.
    from gateway import Channel
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, 'K5-24SBC', channel=Channel.VOICE)
    assert r.kind == 'identify' and r.needs_confirmation
    low = r.text.lower()
    assert '5 by 24 inch' in low          # decoded dims, spoken naturally
    assert 'chrome' in low                # decoded finish, stated not asked
    assert 'what diameter' not in low     # no interrogation
    assert 'what size or finish' not in low
    assert 'x24' not in low and '"x' not in low   # never the raw 5"X24 notation
