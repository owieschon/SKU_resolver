"""Tool provenance is the keystone of fabrication containment: the router decides
fact-turn (verbatim say) vs free-turn from surfaced_values, never from prose. So a
disclosure must NEVER be able to under-report and masquerade as a free turn — and
incidental numbers (dimensions) must not be mistaken for binding facts.
"""
from __future__ import annotations

from gateway import Channel
from gateway.models import TurnResponse
from gateway.provenance import assert_complete, has_binding_value_token, surfaced
from gateway.say_guard import internal_state_tokens
from gateway_fixtures import build_gateway


def _conv(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    return gw, sessions, sessions.open('S', 'c')


def test_completeness_holds_across_every_disclosure_path(tmp_path):
    gw, sessions, tok = _conv(tmp_path)

    def turn(t):
        r = gw.turn('S', tok, t, channel=Channel.VOICE)
        assert_complete(r)                     # never under-reports
        return r

    rb = turn('K5-24SBC')                      # readback: no binding fact disclosed
    assert rb.kind == 'identify'
    assert surfaced(rb)[0] == ('K5-24SBC',) and surfaced(rb)[1] == {}
    assert not has_binding_value_token(rb.text)   # "5 by 24 inch" is NOT a binding value

    av = turn('yes the chrome one')            # availability: BOOLEAN say + ship date
    # invariant 5 (§7): the on-hand count is surfaced internally but NEVER spoken —
    # the say carries no quantity leak, while surfaced_values still carries qty.
    assert internal_state_tokens(av.text) == []
    skus, vals = surfaced(av)
    assert 'K5-24SBC' in skus and vals['qty'] > 0 and vals['ship_by']

    turn('my account number is 1001')
    pr = turn("what's the price?")             # pricing: unit_price
    assert surfaced(pr)[1].get('unit_price') and has_binding_value_token(pr.text)


def test_dimensions_do_not_count_as_binding_values():
    assert not has_binding_value_token('the 5 by 24 inch curved stack')
    assert has_binding_value_token('it is $187.71 each')
    assert has_binding_value_token('we have 58 on hand')
    assert has_binding_value_token('ships by June 9')


def test_provenance_under_report_is_caught():
    # a reply that STATES a price but carries no surfaced_values must raise —
    # otherwise the router would route a disclosure to the free/model path.
    bad = TurnResponse(kind='pricing', text='That part is $187.71 each.',
                       session_state='verified')
    import pytest
    with pytest.raises(AssertionError, match='under-report'):
        assert_complete(bad)
