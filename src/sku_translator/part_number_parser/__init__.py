"""Industrial part-number parser.

Decodes a canonical SKU into structured fields. The extractor uses this for
full-SKU pass-through; the constructor runs the same grammar in reverse, so the
two satisfy the round-trip property:

    parse(construct(extract(sku))) == sku    for any catalog SKU.

Package layout:
    tables.py     family / finish / OEM meaning dictionaries (pure data)
    _patterns.py  ~300 compiled patterns + their decoders + the ordered
                  PATTERNS dispatch list
    _dispatch.py  _try_patterns() + parse() (the public entry point)
"""
from ._dispatch import parse

__all__ = ['parse']
