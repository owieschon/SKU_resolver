"""BC-shaped synthetic validation twin for the ERP adapter harness (D8).

Test infrastructure with behavioral fidelity to documented BC quirks, plus
the fault-injection API the fault-injection check matrix requires. Not
production code; not a Business Central emulator.
"""
from erp_twin.twin import AuditEntry, BCShapedTwin, TwinEntity, TwinField
from erp_twin.seed import STANDARD_GRANTS, seeded_twin

__all__ = ['AuditEntry', 'BCShapedTwin', 'TwinEntity', 'TwinField',
           'STANDARD_GRANTS', 'seeded_twin']
