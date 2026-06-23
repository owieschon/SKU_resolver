"""The disclosure gate proven in ISOLATION (CONVERSATION_STATE_SPEC build order
step 2: "Unit-test invariants 2-4 first"). The gate is the floor the orchestration
wires against and cannot talk around, so it is proven before any agency is built on
it — pure-function in, deterministic verdict out, no orchestration involved.

Covers invariants 2 (precondition AND fresh), 3 (price needs the part's OWN
identification, never inherited from a sibling — the multi-part × shared-account
attack), and 4 (no binding fact without an as_of within horizon), plus the price
account-scoping and the §8 pricing-unreadable interim, which fall out of the gate.
"""
from __future__ import annotations

from gateway.conversation_state import (
    AccountState,
    Fact,
    FactType,
    IdentityState,
    PartContext,
)
from gateway.disclosure_gate import (
    Horizons,
    discloseable,
    fresh,
    precondition_met,
)

NOW = 1_000_000.0
ACCT = AccountState.established('1001')


def _identified_part(ctx='p1', sku='K5-24SBC'):
    return PartContext(ctx_id=ctx, identity=IdentityState.identified(sku))


def _fresh_avail(part, *, as_of=NOW):
    part.availability = Fact.read(True, as_of=as_of)
    return part


def _fresh_price(part, *, account_id='1001', as_of=NOW):
    part.price = Fact.read(42.0, as_of=as_of, account_id=account_id)
    return part


# -- invariant 2: discloseable iff precondition_met AND fresh ----------------

def test_inv2_both_must_hold():
    part = _fresh_avail(_identified_part())
    assert discloseable(part, FactType.AVAILABILITY, ACCT, NOW)     # both true


def test_inv2_precondition_fail_blocks_even_when_fresh():
    # a freshly-read availability on an UNIDENTIFIED part: fresh, precondition unmet
    part = PartContext(ctx_id='p', identity=IdentityState.unknown())
    part.availability = Fact.read(True, as_of=NOW)
    assert fresh(part, FactType.AVAILABILITY, ACCT, NOW) is True
    assert precondition_met(part, FactType.AVAILABILITY, ACCT) is False
    assert discloseable(part, FactType.AVAILABILITY, ACCT, NOW) is False


def test_inv2_fresh_fail_blocks_even_when_precondition_met():
    # identified part (precondition met) but the fact was never read
    part = _identified_part()
    assert precondition_met(part, FactType.AVAILABILITY, ACCT) is True
    assert fresh(part, FactType.AVAILABILITY, ACCT, NOW) is False    # unread
    assert discloseable(part, FactType.AVAILABILITY, ACCT, NOW) is False


# -- invariant 3: price needs the part's OWN identity, never inherited --------

def test_inv3_inherited_disclosability_is_forbidden():
    # the multi-part × shared-account attack (§3.3 / §10): account ESTABLISHED;
    # part B identified + fresh-priced; part C still AMBIGUOUS under the SAME
    # account. B may disclose price; C must NOT inherit it.
    b = _fresh_price(_identified_part('B', 'K5-24SBC'))
    c = PartContext(ctx_id='C', identity=IdentityState.ambiguous(['X', 'Y']))
    c.price = Fact.read(99.0, as_of=NOW, account_id='1001')          # even a "read" price
    assert discloseable(b, FactType.PRICE, ACCT, NOW) is True
    assert precondition_met(c, FactType.PRICE, ACCT) is False        # C's identity, not B's
    assert discloseable(c, FactType.PRICE, ACCT, NOW) is False


def test_inv3_price_without_account_is_undefined_not_blocked():
    # §10 price-without-account: identified part, account UNKNOWN -> precondition fail
    part = _fresh_price(_identified_part(), account_id=None)
    assert discloseable(part, FactType.PRICE, AccountState.unknown(), NOW) is False
    # the same part, once the (shared) account is established and read for it:
    part = _fresh_price(_identified_part())
    assert discloseable(part, FactType.PRICE, ACCT, NOW) is True


# -- invariant 4: no binding fact without an as_of within horizon -------------

def test_inv4_unread_is_never_fresh():
    part = _identified_part()
    assert fresh(part, FactType.AVAILABILITY, ACCT, NOW) is False


def test_inv4_read_past_horizon_is_stale():
    part = _identified_part()
    # availability horizon is 120s; read 200s ago -> stale
    part.availability = Fact.read(True, as_of=NOW - 200)
    assert fresh(part, FactType.AVAILABILITY, ACCT, NOW) is False
    assert discloseable(part, FactType.AVAILABILITY, ACCT, NOW) is False


def test_inv4_read_within_horizon_is_fresh():
    part = _identified_part()
    part.availability = Fact.read(True, as_of=NOW - 60)              # within 120s
    assert discloseable(part, FactType.AVAILABILITY, ACCT, NOW) is True


def test_inv4_horizon_is_per_fact_type_and_injectable():
    part = _fresh_price(_identified_part(), as_of=NOW - 200)
    # price default horizon (86400s) easily covers 200s -> fresh
    assert discloseable(part, FactType.PRICE, ACCT, NOW) is True
    # a tightened horizon makes the same read stale -> tunable without code change
    tight = Horizons(price=100.0)
    assert discloseable(part, FactType.PRICE, ACCT, NOW, tight) is False


# -- price account-scoping (the freshness account clause) --------------------

def test_price_read_for_a_different_account_is_not_fresh():
    part = _fresh_price(_identified_part(), account_id='9999')       # read for acct 9999
    assert precondition_met(part, FactType.PRICE, ACCT) is True      # acct established
    assert fresh(part, FactType.PRICE, ACCT, NOW) is False           # but wrong index
    assert discloseable(part, FactType.PRICE, ACCT, NOW) is False


# -- §8 pricing-unreadable interim falls out of the gate ---------------------

def test_unreadable_price_is_never_discloseable():
    part = _identified_part()
    part.price = Fact.unreadable()                                   # no source wired
    assert precondition_met(part, FactType.PRICE, ACCT) is True
    assert fresh(part, FactType.PRICE, ACCT, NOW) is False           # not READ
    assert discloseable(part, FactType.PRICE, ACCT, NOW) is False    # -> can't-quote handoff
