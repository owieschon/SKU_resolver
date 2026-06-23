"""Structured, leveled logging — one configured logger tree under `sku`.

Emits `event key=value key=value` records so logs are greppable and parseable.
Level from SKU_LOG_LEVEL (default WARNING). Callers pass already-structured
fields (ids, labels, counts) — never raw customer free text — so logs carry no
PII; for the rare free-text field, scrub with observability.scrub_pii first.
"""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s %(message)s'))
    root = logging.getLogger('sku')
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get('SKU_LOG_LEVEL', 'WARNING').upper())
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str = 'sku') -> logging.Logger:
    _configure()
    return logging.getLogger(name if name.startswith('sku') else f'sku.{name}')


def log_event(logger: logging.Logger, level: str, event: str, **fields) -> None:
    """Emit one structured record: `<event> k=v k=v`."""
    suffix = ' '.join(f'{k}={v}' for k, v in fields.items())
    logger.log(getattr(logging, level.upper(), logging.INFO),
               f'{event} {suffix}'.rstrip())
