"""Spoken-form rendering for the voice channel.

Two jobs, both pure (no network — stays inside gateway purity):

1. `spoken_description(row)` — turn the PARSER'S DECODED MEANINGS into a natural
   phrase. The SKU grammar already decodes K5-24SBC into diameter=5, length=24,
   family='Curved-top stack', body='Straight Bottom', finish='Chrome'. Those
   meanings ARE the part's attributes — so the agent should *state them back*
   ("the 5 by 24 inch curved-top stack, straight bottom, chrome"), never ask the
   caller to recite a diameter/finish the part number already encodes.

2. `to_spoken(text)` — a safety net that normalizes any residual raw-catalog
   notation that leaks into spoken text, so TTS reads dimensions as a human would:
   `5"X24` / `5" x 24"` / `5x24` -> "5 by 24 inch", and a lone inch-mark -> "inch".
   Applied at the synth boundary so the audio leg never speaks `5 inch ex 24`.
"""
from __future__ import annotations

import re

# 5"X24  | 5" x 24" | 5x24 | 5 X 24  -> spoken "5 by 24 inch"
_DIM_PAIR = re.compile(r'(\d+(?:\.\d+)?)\s*"?\s*[xX]\s*(\d+(?:\.\d+)?)\s*"?')
# a lone inch-mark following a number: 24"  -> "24 inch"
_LONE_INCH = re.compile(r'(\d+(?:\.\d+)?)\s*"')


def _num(v) -> str:
    """5.0 -> '5', 5.5 -> '5.5' (don't speak a trailing '.0')."""
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _clean_meaning(val: str) -> str:
    """'Straight Bottom (OD-fit)' -> 'straight bottom' — drop the parenthetical
    engineering note; it's not for the caller's ear."""
    return re.sub(r'\s*\([^)]*\)', '', str(val)).strip().lower()


def spoken_description(row) -> str | None:
    """Build a natural spoken phrase from the decoded parser meanings, e.g.
    'the 5 by 24 inch curved-top stack, straight bottom, chrome'. Returns None
    when the row carries no decode (caller should fall back to to_spoken on the
    raw description)."""
    if row is None:
        return None
    parsed = getattr(row, 'raw_parser_result', None) or {}
    if not parsed:
        return None

    dia, length = parsed.get('diameter'), parsed.get('length')
    if dia is not None and length is not None:
        dims = f"{_num(dia)} by {_num(length)} inch"
    elif dia is not None:
        dims = f"{_num(dia)} inch"
    else:
        dims = ''

    family = _clean_meaning(parsed['family_meaning']) if parsed.get('family_meaning') else ''
    body = _clean_meaning(parsed['body_meaning']) if parsed.get('body_meaning') else ''
    finish = _clean_meaning(parsed['finish_meaning']) if parsed.get('finish_meaning') else ''

    head = ' '.join(p for p in (dims, family) if p).strip()
    tail = [p for p in (body, finish) if p]
    if not head and not tail:
        return None
    phrase = head if head else ''
    if tail:
        phrase = f"{phrase}, {', '.join(tail)}" if phrase else ', '.join(tail)
    return f"the {phrase}"


def to_spoken(text: str) -> str:
    """Normalize residual raw notation in any spoken string so TTS reads it
    naturally. Idempotent and safe on already-clean text (dates, prices, prose
    pass through unchanged — there's no bare `N x M` or inch-mark there)."""
    if not text:
        return text
    out = _DIM_PAIR.sub(lambda m: f"{m.group(1)} by {m.group(2)} inch", text)
    out = _LONE_INCH.sub(lambda m: f"{m.group(1)} inch", out)
    return out


def spoken_sku(sku: str) -> str:
    """Spell a part number the way a counter person reads it aloud: letters one
    at a time, number groups whole, hyphens as a brief pause. 'K5-24SBC' ->
    'K 5, 24 S B C' (TTS: "K five, twenty-four, S B C"). This is the real fix for
    TTS mangling alphanumeric SKUs — ElevenLabs phoneme tags are silently ignored
    on the flash/turbo models, so we control rendering here, deterministically."""
    out = []
    for m in re.finditer(r'[A-Za-z]+|\d+|[^A-Za-z\d]+', sku):
        tok = m.group()
        if tok.isalpha():
            out.append(' '.join(tok.upper()))      # S B C
        elif tok.isdigit():
            out.append(tok)                         # 24 (read "twenty-four")
        else:
            out.append(',')                         # hyphen/sep -> pause
    s = re.sub(r'\s*,\s*', ', ', ' '.join(out))
    return re.sub(r'\s+', ' ', s).strip(' ,')


# A part-number-shaped token: alnum (with optional hyphenated groups) that
# contains BOTH a letter and a digit — so "K5-24SBC" matches but "stock", "2026",
# "58", and account number "1001" do not.
_SKU_TOKEN = re.compile(
    r'\b(?=[A-Za-z-]*\d)(?=[\d-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b')


def voice_render(text: str) -> str:
    """Voice-only rendering: normalize dimensions AND spell out part numbers.
    Applied at the speech boundary (TTS / the voice-agent `say`), never to the
    typed/chat/API text or structured fields — a chat client still gets the
    literal 'K5-24SBC'."""
    if not text:
        return text
    text = to_spoken(text)
    return _SKU_TOKEN.sub(lambda m: spoken_sku(m.group()), text)
