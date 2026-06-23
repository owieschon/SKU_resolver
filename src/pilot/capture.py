"""Shadow capture + scrub-at-ingestion. The shadow front-end is the first thing
that captures the HUMAN's actual call — a far larger PII surface than the agent's
own decision traces — so the same filter-on-raw-store-scrubbed discipline applies,
now to the customer's call:

  * the divergence detector and the catalog resolution check run on the RAW text
    (a scrubbed account number or part token would break detection / the catalog
    lookup), inside the ingestion window;
  * only the SCRUBBED transcript + the structured decision points persist — never
    raw audio (audio is transient input to STT at ingestion and is dropped).

`HumanMove` is the rep's actual action per turn, proposed structurally by the
detector and REP-ADJUDICATED (incl. the divergence location) — never inferred and
trusted blindly. Consent to capture the customer's side of a recorded call is a
pilot-agreement matter (two-party-consent jurisdictions), NOT something the code
resolves — flagged in docs/PILOT_HARNESS.md as a precondition.
"""
from __future__ import annotations

from dataclasses import dataclass

from observability import scrub_pii

# The shared move vocabulary — both the agent's decision and the human's action map
# into it, so they are comparable per stream.
BRANCHES = (
    'disclose_availability', 'disclose_price', 'gate_price', 'escalate',
    'clarify', 'establish_account', 'other', 'none',
)


@dataclass(frozen=True)
class HumanMove:
    """The rep's actual action this turn (structurally proposed + rep-adjudicated)."""
    resolved_sku: str | None = None       # the part the rep landed on (catalog-checkable)
    branch: str = 'none'                  # one of BRANCHES
    established_account: str | None = None


@dataclass(frozen=True)
class CallTurn:
    caller_text: str                      # RAW (scrubbed only on persistence)
    rep_text: str
    human: HumanMove


@dataclass(frozen=True)
class RawCall:
    call_id: str
    turns: tuple = ()                     # tuple[CallTurn]


def scrub_text(text: str, names=()) -> str:
    """scrub_pii (account numbers / phones / emails) PLUS the customer's known
    account names (the caller-name surface scrub_pii doesn't cover; the name list
    comes from the customer DB at ingestion)."""
    out = scrub_pii(text or '')
    for nm in names:
        if nm:
            out = out.replace(nm, '[NAME]')
    return out


@dataclass(frozen=True)
class ScrubbedCall:
    """What PERSISTS: scrubbed transcript only. No raw text, no audio. The decision
    points (which reference catalog SKUs, not PII) are stored alongside, separately."""
    call_id: str
    turns: tuple = ()                    # tuple[(scrubbed_caller, scrubbed_rep)]


def scrub_call(raw: RawCall, names=()) -> ScrubbedCall:
    return ScrubbedCall(
        call_id=raw.call_id,
        turns=tuple((scrub_text(t.caller_text, names), scrub_text(t.rep_text, names))
                    for t in raw.turns))
