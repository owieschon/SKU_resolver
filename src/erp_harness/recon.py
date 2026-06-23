"""C1 — Recon & Permissions Manifest Generator (Phase A, pre-access).

From an ERP identity descriptor alone, emit the least-privilege grant list
Phase B will need — nothing more, nothing absent. The .md rendering is the
IT-facing artifact: every grant carries its one-line why, and the manifest
states explicitly what is NOT requested.

Deterministic by DoD: same descriptor -> identical manifest. The knowledge
table is data, not branching logic — adding an ERP class is a table entry.
"""
from __future__ import annotations

from erp_harness.models import (
    ERPClass, ERPDescriptor, Grant, InvariantViolation, PermissionsManifest,
    UnsupportedERPError,
)

# Knowledge table: what Phase B touches, per supported ERP class. The DoD
# couples this to discovery/probes mechanically — test_harness_c1 asserts
# every object Phase B requests appears here (sufficiency) and that revoking
# any single grant produces a named failure (minimality).
_BC_GRANTS = (
    Grant('metadata', 'OData $metadata read',
          'Schema discovery — the only surface where tenant tableextension '
          'custom fields are visible'),
    Grant('items', 'API read: items',
          'Item-master profiling and catalog grammar analysis'),
    Grant('customers', 'API read: customers',
          'Customer entity mapping for the canonical contract'),
    Grant('salesOrders', 'API read: salesOrders',
          'Order entity mapping for the canonical contract'),
    Grant('status', 'API read: system status',
          'Posting-queue observation for the consistency probe (read-only)'),
)

_NOT_REQUESTED = (
    'No write, create, modify, or delete permission of any kind',
    'No admin or configuration scopes',
    'No user, payroll, or HR data',
    'No general-ledger postings (cost-ledger access is a named gap, '
    'requested separately only if onboarding proceeds)',
)

_SUPPORTED = {
    ERPClass.BC_SAAS: _BC_GRANTS,
    ERPClass.NAV_ONPREM: tuple(
        Grant(g.object_name, f'read-only SQL grant: {g.object_name}', g.why)
        for g in _BC_GRANTS if g.object_name != 'metadata'
    ) + (Grant('metadata', 'read-only SQL grant: information_schema',
               'Schema discovery for on-prem NAV (information_schema crawl)'),),
}

_WRITE_DENYLIST = ('write', 'create', 'modify', 'delete', 'admin', 'full')


def generate_manifest(erp: ERPDescriptor) -> PermissionsManifest:
    grants = _SUPPORTED.get(erp.erp_class)
    if grants is None:
        raise UnsupportedERPError(
            f'{erp.erp_class.value} is not supported in v1: no public '
            f'instance access exists to validate against (spec §6). '
            f'Supported: {[c.value for c in _SUPPORTED]}'
        )
    for g in grants:  # mechanical least-privilege check, not convention
        lowered = g.scope.lower()
        if g.access != 'read' or any(w in lowered for w in _WRITE_DENYLIST):
            raise InvariantViolation(
                f'least-privilege manifest invariant violated: non-read '
                f'grant {g!r}')
    return PermissionsManifest(erp=erp, grants=grants,
                               not_requested=_NOT_REQUESTED)


def render_markdown(manifest: PermissionsManifest) -> str:
    lines = [
        f'# Access request — {manifest.erp.erp_class.value} '
        f'({manifest.erp.product_version})',
        '',
        'Read-only, exploration-phase access. Every grant below is the '
        'minimum needed for the schema-discovery step; nothing else is '
        'requested.',
        '',
        '| Object | Grant | Why |',
        '|---|---|---|',
    ]
    for g in manifest.grants:
        lines.append(f'| `{g.object_name}` | {g.scope} | {g.why} |')
    lines += ['', '## Explicitly NOT requested', '']
    lines += [f'- {n}' for n in manifest.not_requested]
    return '\n'.join(lines) + '\n'
