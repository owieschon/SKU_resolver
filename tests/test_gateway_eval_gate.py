"""Eval-as-blocking-gate (spec §2.5, borrowed from an eval-dataset gate).

The conversational corpus is a readiness input: every required failure-mode
id must be PRESENT and its case must PASS, and the rubber-stamp canary (a case
marked should_fail) MUST be rejected by the gate — proving the gate can say no.
"""
from __future__ import annotations

import json
from pathlib import Path

from gateway import Channel
from gateway.session import MAX_VERIFY_ATTEMPTS
from gateway_fixtures import build_gateway

CASES = json.loads((Path(__file__).resolve().parent.parent /
                    'data' / 'gateway_eval_cases.json').read_text())


def test_required_failure_modes_present():
    required = set(CASES['_meta']['required_failure_modes'])
    have = {c['failure_mode'] for c in CASES['cases']}
    assert required <= have, f'missing required cases: {required - have}'


def test_corpus_contains_a_should_fail_canary():
    # Rubber-stamp mitigation: at least one case the gate MUST reject.
    assert any(c.get('should_fail') for c in CASES['cases'])


def _run_case(gw, sessions, c):
    tok = sessions.open(c['id'], f"chan-{c['id']}")
    if c['failure_mode'] == 'enumeration_attack':
        for i in range(c['expect_lockout_after'] + 1):
            gw.turn(c['id'], tok, f'account number {800000 + i}',
                    channel=Channel.TYPED)
        r = gw.turn(c['id'], tok, 'account number 900001', channel=Channel.TYPED)
        return r.refused == 'verification_locked'
    if c['failure_mode'] == 'cross_account_pricing':
        gw.turn(c['id'], tok, f"account number {c['verify_as']}",
                channel=Channel.TYPED)
        auth = sessions.issue_authorization(c['id'], tok,
                                            c['price_target_account'])
        return auth.granted is False
    # turn-based cases
    r = gw.turn(c['id'], tok, c['turn'], channel=Channel(c['channel']))
    ok = True
    if c.get('expect_no_price'):
        ok &= r.price is None
    if c.get('expect_state'):
        ok &= sessions.state_of(c['id'], tok).value == c['expect_state']
    if c.get('expect_needs_confirmation'):
        ok &= r.needs_confirmation
    if c.get('expect_no_silent_action'):
        ok &= (r.availability is None and r.price is None)
    if c.get('expect_scrubbed'):
        # the journal must not contain the raw value
        raw = (gw.journal.path).read_text()
        ok &= c['expect_scrubbed'] not in raw
    return ok


def test_all_required_cases_pass_and_canary_is_rejected(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    for c in CASES['cases']:
        result = _run_case(gw, sessions, c)
        if c.get('should_fail'):
            # The canary is an unverified pricing request — "passing" it would
            # mean a price came back. The gate MUST refuse, so the case's
            # success condition (no price) holds == the gate rejected it.
            tok = sessions.open('canary2', 'c')
            r = gw.turn('canary2', tok, c['turn'], channel=Channel(c['channel']))
            assert r.price is None, 'rubber-stamp canary leaked a price — gate broken'
        else:
            assert result, f"required eval case failed: {c['id']}"
