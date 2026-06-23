"""Fault injection for the fault-injection check matrix.

Each function mutates a BCShapedTwin in place and returns a short
human-readable description of the planted fault — tests assert the harness
names exactly what was planted. A detector that has never caught a planted
fault is unproven (spec §2); this module is how faults get planted.
"""
from __future__ import annotations

from erp_twin.twin import BCShapedTwin, TwinField


def rename_field(twin: BCShapedTwin, entity: str, old: str, new: str) -> str:
    ent = twin._entities[entity]
    ent.fields = [TwinField(new, f.edm_type, f.nullable, f.field_number)
                  if f.name == old else f for f in ent.fields]
    for row in ent.rows:
        if old in row:
            row[new] = row.pop(old)
    return f'renamed {entity}.{old} -> {entity}.{new}'


def add_custom_field(twin: BCShapedTwin, entity: str, name: str,
                     field_number: int = 50001,
                     fill: str = 'X') -> str:
    assert field_number >= 50000, 'custom fields live in the 50000+ range'
    ent = twin._entities[entity]
    ent.fields.append(TwinField(name, 'Edm.String', True, field_number))
    for row in ent.rows:
        row[name] = fill
    return f'added custom field {entity}.{name} (fieldNumber={field_number})'


def hide_entity(twin: BCShapedTwin, entity: str) -> str:
    twin._entities[entity].hidden = True
    return f'hid entity {entity!r} from the API surface'


def change_type(twin: BCShapedTwin, entity: str, fld: str, new_type: str) -> str:
    ent = twin._entities[entity]
    ent.fields = [TwinField(f.name, new_type, f.nullable, f.field_number)
                  if f.name == fld else f for f in ent.fields]
    return f'changed type of {entity}.{fld} -> {new_type}'


def drop_nav_property(twin: BCShapedTwin, entity: str, nav: str) -> str:
    ent = twin._entities[entity]
    ent.nav_properties = [n for n in ent.nav_properties if n != nav]
    return f'dropped navigation property {entity}.{nav}'
