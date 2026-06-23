"""Unified resolution service (milestone M3): translator-first, BM25
candidate fallback, never-invent and tenant isolation verified adversarially
in tests/test_resolution_adversarial.py.
"""
from resolution.retrieval import BM25CatalogRetriever, RetrievedCandidate
from resolution.service import (
    Candidate,
    OpenQuestion,
    Resolution,
    ResolutionService,
    catalog_content_version,
)

__all__ = [
    'BM25CatalogRetriever', 'RetrievedCandidate', 'Candidate', 'OpenQuestion',
    'Resolution', 'ResolutionService', 'catalog_content_version',
]
