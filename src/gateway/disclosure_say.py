"""Disclosure rendering (CONVERSATION_STATE_SPEC §7) — verbatim, guarded, and on
multi-part PER-PART-EXPLICIT. A multi-part availability disclosure names each part
with its own status; it must NEVER aggregate into a summary that loses which part
is which ("those are mostly available" is forbidden — the partial case is exactly
where a vague answer becomes a broken commitment). Availability is BOOLEAN; the
on-hand count is internal state (caught by say_guard / invariant 5). Deterministic
and pure — the model never authors these; the gateway does.
"""
from __future__ import annotations


def _cap(s: str) -> str:
    return (s[:1].upper() + s[1:]) if s else s


def render_availability(items) -> str:
    """`items`: list of (label, available: bool, lead_time_text | None). Each part
    is its OWN explicit sentence — structurally impossible to aggregate."""
    sentences = []
    for label, available, lead in items:
        if available:
            sentences.append(f"The {label} is in stock.")
        else:
            lt = f" Lead time {lead}." if lead else ""
            sentences.append(f"The {label} is not in stock.{lt}")
    return " ".join(sentences)


def render_price(label: str, price_text: str) -> str:
    return f"The price for the {label} is {price_text}."
