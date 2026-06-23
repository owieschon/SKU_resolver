"""Runtime wiring — builds a Gateway from config, selecting scripted vs real
adapters by environment. "Go to production" = set env vars + provide
credentials; no code change. Every seam defaults to its deterministic/local
implementation so the app boots and serves with zero external dependencies.

Env switches (all optional; safe local defaults):
  SKU_CATALOG_PATH        catalog CSV (default: bundled fixture)
  SKU_INVENTORY_PATH      inventory json (default: bundled)
  SKU_SESSION_SECRET      HMAC session secret (default: dev secret + warning)
  SKU_LLM_PROVIDER        '' (none, rule-based/no-chooser) | anthropic | openai | openrouter
  SKU_CUSTOMER_DB         path to a customer json (default: tiny demo set)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent.parent
NY = ZoneInfo('America/New_York')

_DEMO_ACCOUNTS = [
    {'account_id': '1001', 'name': 'DEMO TRUCK CENTER', 'phone': None},
    {'account_id': '2055', 'name': 'NORTH FLEET TRUCK', 'phone': None},
]


def _now_dt():
    # Real wall clock for ship dates in the running service.
    return datetime.now(tz=NY)


def build_gateway():
    """Construct the live Gateway. Returns (gateway, session_manager)."""
    from fulfillment import load_inventory
    from gateway import (
        Account,
        ConversationJournal,
        Gateway,
        InMemoryCustomerDB,
        SessionManager,
        SyntheticPriceBook,
    )
    from resolution import ResolutionService, catalog_content_version
    from sku_translator import FixtureCatalogIndex, InMemoryStore

    catalog_path = os.environ.get('SKU_CATALOG_PATH',
                                  str(REPO / 'data' / 'catalog.csv'))
    inv_path = os.environ.get('SKU_INVENTORY_PATH',
                              str(REPO / 'data' / 'inventory.json'))
    catalog = FixtureCatalogIndex(catalog_path, tenant_id='tenant_001')
    version = catalog_content_version(catalog_path)
    inventory = load_inventory(inv_path)

    # Optional LLM seams (chooser + intent router) — only when a provider is set.
    chooser, intent_router = None, None
    provider_name = os.environ.get('SKU_LLM_PROVIDER', '').strip()
    if provider_name:
        from model_provider import LLMClient, make_provider
        from observability.cost import CostLedger
        llm = LLMClient(provider=make_provider(provider_name),
                        cost_ledger=CostLedger(REPO / 'state' / 'cost.jsonl'),
                        now_iso=lambda: datetime.now(tz=NY).isoformat())
        from gateway.intent import LLMIntentRouter
        from resolution.chooser import LLMChooser
        chooser = LLMChooser(llm)
        intent_router = LLMIntentRouter(llm)

    # One shared, optionally-persisted learned-alias store. The LIVE resolver
    # consults it (so confirmed corrections actually change the agent's answers)
    # and the continuous-improvement loop writes to it — same instance, so a
    # self-heal reaches production immediately, and survives restart when
    # SKU_CORRECTIONS_PATH is set.
    from gateway import CorrectionStore
    corrections = CorrectionStore(
        catalog, path=os.environ.get('SKU_CORRECTIONS_PATH') or None)

    service = ResolutionService(catalog, InMemoryStore(),
                               catalog_version=version, chooser=chooser,
                               learned_aliases=corrections)

    # CustomerDB: a .db/.sqlite path -> sqlite adapter; a .json path -> in-memory
    # from rows; nothing -> the tiny demo set. "Go to production" = point the env
    # var at a real DB file, no code change.
    db_path = os.environ.get('SKU_CUSTOMER_DB')
    if db_path and str(db_path).endswith(('.db', '.sqlite', '.sqlite3')):
        from gateway import SqliteCustomerDB
        customer_db = SqliteCustomerDB(db_path)
    else:
        rows = (json.loads(Path(db_path).read_text()) if db_path
                else _DEMO_ACCOUNTS)
        customer_db = InMemoryCustomerDB([Account(**r) for r in rows])

    price_db = os.environ.get('SKU_PRICEBOOK_DB')
    if price_db:
        from gateway import SqlitePriceBook
        pricebook = SqlitePriceBook(price_db)
    else:
        pricebook = SyntheticPriceBook.seeded(catalog.all_skus())

    if using_dev_secret():
        import warnings
        warnings.warn(
            'SKU_SESSION_SECRET is unset; signing sessions with an insecure dev '
            'key. Set SKU_SESSION_SECRET before any real deployment.',
            RuntimeWarning, stacklevel=2)
    secret = os.environ.get('SKU_SESSION_SECRET', 'dev-insecure-secret').encode()
    import time
    sessions = SessionManager(secret=secret, customer_db=customer_db,
                              now_fn=time.monotonic)
    journal = ConversationJournal(path=REPO / 'state' / 'conversation.jsonl',
                                  now_fn=lambda: datetime.now(tz=NY).isoformat())
    gateway = Gateway(service=service, catalog=catalog, inventory=inventory,
                      catalog_version=version, sessions=sessions,
                      journal=journal, pricebook=pricebook,
                      account_tier_of=lambda aid: 'preferred',
                      now_fn=_now_dt, intent_router=intent_router)
    gateway.corrections = corrections   # shared with the improvement loop
    return gateway, sessions


def using_dev_secret() -> bool:
    return not os.environ.get('SKU_SESSION_SECRET')


def build_streaming_asr():
    """Select the streaming ASR for the Twilio Media Streams path. Real
    AssemblyAI v3 when ASSEMBLYAI_API_KEY is set; otherwise a no-op simulated
    ASR so the app boots and the endpoint is wired without credentials."""
    from gateway import AssemblyAIStreamingASR, SimulatedStreamingASR
    if os.environ.get('ASSEMBLYAI_API_KEY'):
        return AssemblyAIStreamingASR()
    return SimulatedStreamingASR()   # emits nothing; live path needs the key


def build_improvement(gateway):
    """The always-on continuous self-improvement loop, wired to the live gateway
    (uses its resolution service + catalog). Off unless SKU_IMPROVEMENT is set,
    so it never runs in CI by default; injectable in tests."""
    if not os.environ.get('SKU_IMPROVEMENT'):
        return None
    from gateway import ContinuousImprovement, ShadowObserver
    from observability import ImprovementLog
    # Reuse the gateway's shared correction store so a self-heal reaches the LIVE
    # resolver (and persists), not just the shadow simulation.
    corrections = gateway.corrections
    observer = ShadowObserver(gateway.service, catalog=gateway.catalog,
                              corrections=corrections, log=ImprovementLog(
                                  os.environ.get('SKU_IMPROVEMENT_LOG') or None))
    return ContinuousImprovement(
        observer, corrections,
        review_every=int(os.environ.get('SKU_IMPROVEMENT_REVIEW_EVERY', '25')))


def build_persona():
    """The operator-configurable voice persona: name, accent, voice, style,
    greeting — all from env. Accent selects the voice (overridable)."""
    from gateway import VoicePersona
    accent = os.environ.get('SKU_VOICE_ACCENT', 'standard')
    # Real voice id resolution order: explicit SKU_VOICE_ID, then a per-accent
    # override SKU_VOICE_ID_<ACCENT> (e.g. SKU_VOICE_ID_SOUTHERN), else the
    # persona's placeholder (which makes live TTS fail loudly until set).
    voice_id = (os.environ.get('SKU_VOICE_ID')
                or os.environ.get(f'SKU_VOICE_ID_{accent.upper()}', ''))
    return VoicePersona(
        name=os.environ.get('SKU_VOICE_NAME', 'the parts department'),
        rep_name=os.environ.get('SKU_VOICE_REP_NAME', 'Sam'),
        accent=accent, voice_id=voice_id,
        style=os.environ.get('SKU_VOICE_STYLE', 'friendly, concise, professional'),
        greeting=os.environ.get('SKU_VOICE_GREETING', ''))


def build_tts(persona=None):
    """Select the TTS for the voice reply leg. Real ElevenLabs (ulaw_8000, no
    resample) when ELEVENLABS_API_KEY is set; otherwise SimulatedTTS so the
    reply leg is wired and testable without credentials. The persona's resolved
    voice id (from its accent, or an explicit override) selects the voice."""
    from gateway import SimulatedTTS
    persona = persona or build_persona()
    if os.environ.get('ELEVENLABS_API_KEY'):
        from runtime.tts_adapters import ElevenLabsTTS
        return ElevenLabsTTS(voice_id=persona.resolved_voice_id())
    return SimulatedTTS()
