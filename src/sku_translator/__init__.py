"""SKU Translator package — translate free-form human input into canonical SKUs.

The full pipeline:

    text → normalize → extract → [parser | construct | fuzzy | memory
                                  | disambiguate] → TranslationResult

Quick start
-----------
>>> from sku_translator import translate, FixtureCatalogIndex, InMemoryStore
>>> catalog = FixtureCatalogIndex('catalog.csv', tenant_id='tenant_001')
>>> mem = InMemoryStore()
>>> result = translate(
...     '5 inch chrome curved 24 long SB',
...     catalog=catalog,
...     memory=mem,
...     customer='DEMO',
... )
>>> if result.state == 'resolved':
...     print(result.sku)

Architecture
------------
- The CatalogIndex protocol abstracts over the data source. Production
  uses ERPCatalogIndex (backed by Supabase materialized view synced
  from the tenant's ERP). Development and tests use FixtureCatalogIndex
  (CSV-backed).
- The translator is read-only with respect to memory. Recording rep
  choices is a separate explicit call (record_translation_choice) so
  speculative outputs don't pollute history.
- Proprietary-customer policy is enforced at the orchestrator level
  using the customer parameter.
- "Rules own canonical output, LLMs propose only" — the parser's
  grammar is auditable; fuzzy matching is plain Levenshtein with
  bucket-scoping; no model calls in the hot path.
"""
from sku_translator.normalizer import (
    normalize_input,
    normalize_surface,
    normalize_dimension,
    normalize_compound_dimension,
    normalize_finish,
    normalize_family_word,
    normalize_body,
    normalize_fit,
    normalize_oem,
    normalize_truck_model,
    normalize_sku_fragment,
    NormalizedInput,
    NormalizedToken,
)
from sku_translator.extractor import (
    extract_spec,
    PartSpec,
    Ambiguity,
)
from sku_translator.constructor import (
    construct_sku,
    ConstructionError,
    InsufficientSpecError,
    UnsupportedFamilyError,
)
from sku_translator.part_number_parser import parse
from sku_translator.catalog_index import (
    CatalogIndex,
    ParsedRow,
    family_prefix_for,
    is_proprietary_marker,
    is_excluded_ipg,
    is_excluded_pgc,
    PROPRIETARY_SPELLINGS,
    EXCLUDED_IPG_VALUES,
    EXCLUDED_PGC_VALUES,
)
from sku_translator.fixture_catalog import FixtureCatalogIndex
from sku_translator.erp_catalog import ERPCatalogIndex
from sku_translator.fuzzy_matcher import (
    FuzzyMatch,
    fuzzy_match,
)
from sku_translator.disambiguator import (
    Candidate,
    DisambiguationResult,
    disambiguate,
)
from sku_translator.memory import (
    MemoryStore,
    InMemoryStore,
    TranslatorEvent,
    ReplayDecision,
    consult_memory,
    record_choice,
)
from sku_translator.translator import (
    TranslationResult,
    translate,
    record_translation_choice,
    RESOLVED,
    PENDING_DISAMBIGUATION,
    UNRESOLVABLE,
)

__all__ = [
    # Normalizer
    'normalize_input', 'normalize_surface', 'normalize_dimension',
    'normalize_compound_dimension', 'normalize_finish', 'normalize_family_word',
    'normalize_body', 'normalize_fit', 'normalize_oem', 'normalize_truck_model',
    'normalize_sku_fragment', 'NormalizedInput', 'NormalizedToken',
    # Extractor
    'extract_spec', 'PartSpec', 'Ambiguity',
    # Constructor
    'construct_sku', 'ConstructionError', 'InsufficientSpecError', 'UnsupportedFamilyError',
    # Parser
    'parse',
    # Catalog
    'CatalogIndex', 'ParsedRow', 'family_prefix_for',
    'is_proprietary_marker', 'is_excluded_ipg', 'is_excluded_pgc',
    'PROPRIETARY_SPELLINGS', 'EXCLUDED_IPG_VALUES', 'EXCLUDED_PGC_VALUES',
    'FixtureCatalogIndex', 'ERPCatalogIndex',
    # Fuzzy matcher
    'FuzzyMatch', 'fuzzy_match',
    # Disambiguator
    'Candidate', 'DisambiguationResult', 'disambiguate',
    # Memory
    'MemoryStore', 'InMemoryStore', 'TranslatorEvent', 'ReplayDecision',
    'consult_memory', 'record_choice',
    # Orchestrator
    'TranslationResult', 'translate', 'record_translation_choice',
    'RESOLVED', 'PENDING_DISAMBIGUATION', 'UNRESOLVABLE',
]
