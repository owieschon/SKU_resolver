"""Holistic end-to-end: a new tenant's whole journey across every layer, with
real components (only audio/credentialed seams stand in). Proves the layers
actually connect — not each tested in isolation.

  onboard (decode an unknown catalog)  ->  resolve (deterministic, never-invent)
   ->  converse (chat: availability/pricing/verify over HTTP)
   ->  continuously improve (self-monitor + ride-along self-heal closes the loop)
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from erp_harness import LineCatalogSource, decode_catalog_source
from gateway import (
    ContinuousImprovement, CorrectionStore, ShadowObserver,
)
from resolution import ResolutionService
from sku_translator import InMemoryStore
from gateway_fixtures import _shared_catalog
from runtime.app import create_app

REAL_SKU = 'K5-24SBC'
FAIL = 'do you stock the qq9zz adapter'

_VENDOR_LINES = [
    'BEARINGS & BUSHINGS',
    'CUMMINS® DIRECT REPLACEMENT',
    'WA902-17-6674   116391   Bushing            Fits CUMMINS® NTC',
    'WA902-17-6675   132770   Gear Cover Bushing Fits CUMMINS® NTC',
    'WA902-17-6677   187420   Rod Bushing        Fits CUMMINS® NT855',
    'WA901-17-6601   4W5739   Rod Bearing        Fits CATERPILLAR® 3300',
    'WA901-17-6600   8N8221   Rod Bearing        Fits CATERPILLAR® 3300',
    'WA903-01-1021   8929310  Drive Gear         Fits DETROIT® 60 Series',
]


def test_new_tenant_journey_across_all_layers():
    # 1. ONBOARDING — decode an unknown vendor catalog with no ERP, from text.
    report = decode_catalog_source(LineCatalogSource(_VENDOR_LINES))
    wa = next(f for f in report.families if f.family_code == 'WA')
    assert wa.member_count >= 5
    # the engine-line segment classifies from the fitment/section evidence
    assert any(r.role == 'classifier' for r in wa.segment_roles)

    # 2. RESOLUTION — deterministic, and accurate about misses (never-invent).
    cat, ver = _shared_catalog()
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver)
    assert svc.resolve(REAL_SKU).state == 'resolved'
    assert svc.resolve('completely made up nonsense').state != 'resolved'

    # 3. CONVERSATION — availability ungated; pricing gated behind verification;
    #    full flow over the real HTTP surface.
    client = TestClient(create_app())
    s = client.post('/v1/sessions', params={'channel_id': 'e2e'}).json()
    sid, tok = s['session_id'], s['token']

    def turn(text):
        return client.post('/v1/turns', json={
            'session_id': sid, 'token': tok, 'text': text,
            'channel': 'typed'}).json()

    assert turn(f'is {REAL_SKU} in stock?')['kind'] == 'availability'
    assert turn(f'how much is {REAL_SKU}?').get('refused') == 'pricing_unauthorized'
    assert turn('my account number is 1001')['session_state'] == 'verified'
    priced = turn(f'how much is {REAL_SKU}?')
    assert priced.get('price') and priced['price']['source'] == 'verified_account_self'

    # 4. CONTINUOUS IMPROVEMENT — the always-on loop, end to end, FED INTO THE
    #    LIVE RESOLVER (one shared correction store, like production wiring).
    corr = CorrectionStore(cat)
    live = ResolutionService(cat, InMemoryStore(), catalog_version=ver,
                             learned_aliases=corr)
    assert live.resolve(FAIL).state != 'resolved'        # agent can't yet
    ci = ContinuousImprovement(
        ShadowObserver(live, catalog=cat, corrections=corr), corr, review_every=1)
    # (a) a call the agent ran itself: an uncertain moment becomes a review item
    ci.ingest_self_monitored_call([('customer', FAIL)])
    assert ci.pending_review().opportunities
    # (b) a ride-along call where the rep resolves it -> PROPOSES (gated)
    ci.ingest_call([('customer', FAIL), ('rep', f'that is {REAL_SKU}')])
    # proposed but NOT live — alias_for returns None for non-ACTIVE
    from gateway.alias_store import PROPOSED, on_confirm
    from learning.eval_battery import Verdict
    a = corr.get_alias(FAIL)
    assert a is not None and a.state == PROPOSED
    assert corr.alias_for(FAIL) is None               # gate holds
    # (c) gate it through: bump confidence, battery pass, human release
    on_confirm(a, 'order_not_returned', now=2000.0)    # +0.40 -> >=0.70
    assert corr.clear_for_release(FAIL, verdict=Verdict.injected_pass())
    corr.release(FAIL)
    # (d) THE POINT: the LIVE resolver now answers the previously-failing phrasing
    after = live.resolve(FAIL)
    assert after.state == 'resolved' and after.sku == REAL_SKU
    assert after.source == 'learned_alias'
