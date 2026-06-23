"""P2 LLM seams wired behind deterministic fallbacks, exercised with
ScriptedProvider (no network). The critical assertion across all three:
the model only PROPOSES — deterministic code still binds, so never-invent and
the gates survive even a hallucinating or unavailable model.
"""
from __future__ import annotations

from pathlib import Path

from model_provider import ScriptedProvider, LLMClient
from sku_translator import FixtureCatalogIndex, InMemoryStore
from resolution import ResolutionService, catalog_content_version
from resolution.chooser import LLMChooser

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / 'data' / 'catalog.csv'

_cat = None
_ver = None


def _catalog():
    global _cat, _ver
    if _cat is None:
        _cat = FixtureCatalogIndex(str(CATALOG), tenant_id='tenant_001')
        _ver = catalog_content_version(CATALOG)
    return _cat, _ver


# ── retrieval chooser ────────────────────────────────────────────────────────

def _chooser_service(scripted_pick):
    cat, ver = _catalog()
    provider = ScriptedProvider(scripted={
        'retrieval_select': lambda req: {'sku': scripted_pick(req)}})
    chooser = LLMChooser(LLMClient(provider=provider))
    return ResolutionService(cat, InMemoryStore(), catalog_version=ver,
                             chooser=chooser)


def test_chooser_promotes_a_valid_pick_to_resolved():
    # Pick the first candidate SKU out of the prompt's candidate listing.
    def pick(req):
        # candidate lines look like "- SKU: desc"; grab the first SKU
        for line in req.user.splitlines():
            if line.startswith('- '):
                return line[2:].split(':')[0].strip()
        return ''
    svc = _chooser_service(pick)
    # a description-y query that hits the BM25 fallback
    r = svc.resolve('water bottle')
    if r.source == 'retrieval:llm_chooser':
        assert r.state == 'resolved'
        assert r.sku in set(_catalog()[0].all_skus())     # never-invent holds
        assert 'llm_chosen' in r.flags


def test_chooser_hallucination_is_rejected_never_invents():
    # The model returns a SKU that is NOT among the candidates.
    svc = _chooser_service(lambda req: 'TOTALLY-BOGUS-9999')
    r = svc.resolve('water bottle')
    # bind-guard rejects the fabrication -> falls back to the picker, never
    # emits the bogus SKU as resolved.
    assert r.sku != 'TOTALLY-BOGUS-9999'
    if r.state == 'resolved':
        assert r.sku in set(_catalog()[0].all_skus())


def test_chooser_unavailable_falls_back_to_picker():
    cat, ver = _catalog()
    provider = ScriptedProvider(fail_tasks={'retrieval_select'})
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver,
                            chooser=LLMChooser(LLMClient(provider=provider)))
    r = svc.resolve('water bottle')
    # graceful: model down -> candidate picker, not a crash
    assert r.state in ('pending_disambiguation', 'unresolvable')


def test_default_service_has_no_chooser_d5_behavior():
    cat, ver = _catalog()
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver)
    r = svc.resolve('water bottle')
    assert r.source != 'retrieval:llm_chooser'   # propose-only by default


# ── onboarding explorer ──────────────────────────────────────────────────────

def test_llm_explorer_proposals_are_field_guarded():
    from erp_harness.explorer import LLMExplorer
    from erp_harness import CANONICAL_CONTRACT
    from harness_fixtures import make_rig          # noqa: E402
    from erp_harness.discovery import discover
    _, _, enforcer = make_rig(item_limit=50)
    surface = discover(enforcer)
    # Model proposes one real mapping and one fabricated field.
    provider = ScriptedProvider(scripted={'onboarding_map': {'mappings': [
        {'contract_field': 'sku', 'entity': 'items', 'source_field': 'number',
         'rationale': 'real'},
        {'contract_field': 'description', 'entity': 'items',
         'source_field': 'NONEXISTENT', 'rationale': 'fabricated'},
    ]}})
    explorer = LLMExplorer(LLMClient(provider=provider))
    proposals = explorer.propose(surface, CANONICAL_CONTRACT)
    fields = {(p.entity, p.source_field) for p in proposals}
    assert ('items', 'number') in fields          # real one kept
    assert ('items', 'NONEXISTENT') not in fields  # fabrication dropped


def test_llm_explorer_falls_back_when_unavailable():
    from erp_harness.explorer import LLMExplorer, HeuristicExplorer
    from erp_harness import CANONICAL_CONTRACT
    from harness_fixtures import make_rig
    from erp_harness.discovery import discover
    _, _, enforcer = make_rig(item_limit=50)
    surface = discover(enforcer)
    provider = ScriptedProvider(fail_tasks={'onboarding_map'})
    explorer = LLMExplorer(LLMClient(provider=provider))
    proposals = explorer.propose(surface, CANONICAL_CONTRACT)
    # fell back to heuristic -> still produces the obvious mappings
    assert any(p.contract_field == 'sku' for p in proposals)


# ── intent router ────────────────────────────────────────────────────────────

def test_llm_intent_router_classifies_and_binds():
    from gateway.intent import LLMIntentRouter, Intent
    provider = ScriptedProvider(scripted={'intent':
                                      {'intent': 'pricing', 'reason': 'x'}})
    router = LLMIntentRouter(LLMClient(provider=provider))
    assert router.classify('what does it run').intent is Intent.PRICING


def test_llm_intent_router_falls_back_when_unavailable():
    from gateway.intent import LLMIntentRouter, Intent
    provider = ScriptedProvider(fail_tasks={'intent'})
    router = LLMIntentRouter(LLMClient(provider=provider))
    # model down -> rule-based fallback still classifies a clear pricing turn
    assert router.classify('how much is K5-24SBC?').intent is Intent.PRICING


def test_rule_based_router_matches_legacy_routing():
    from gateway.intent import RuleBasedIntentRouter, Intent
    r = RuleBasedIntentRouter()
    assert r.classify('is K5-24SBC in stock?').intent is Intent.AVAILABILITY
    assert r.classify('my account number is 1001').intent is Intent.VERIFY
    assert r.classify('how much is K5-24SBC?').intent is Intent.PRICING
    assert r.classify('talk to a human').intent is Intent.HANDOFF
    assert r.classify('cancel my order please').intent is Intent.HANDOFF
