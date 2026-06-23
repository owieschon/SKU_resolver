"""Customer directory for verification (G2). Protocol + an in-memory impl.

Verification matches by account number (exact) or name (0/1/many rule). The
lookup intentionally exposes NO existence oracle to callers — the gateway
maps all non-single-match outcomes to one neutral refusal (G2 DoD).
"""
from __future__ import annotations

from typing import Protocol

from gateway.models import Account


class CustomerDB(Protocol):
    def by_number(self, account_no: str) -> Account | None: ...
    def by_name(self, name: str) -> list[Account]: ...


class InMemoryCustomerDB:
    def __init__(self, accounts: list[Account]) -> None:
        self._by_no = {a.account_id: a for a in accounts}
        self._accounts = list(accounts)

    def by_number(self, account_no: str) -> Account | None:
        return self._by_no.get(account_no.strip())

    def by_name(self, name: str) -> list[Account]:
        q = name.strip().lower()
        if not q:
            return []
        # Substring match, deterministic order — the 0/1/many rule is applied
        # by the caller (session.py), not here.
        return [a for a in self._accounts if q in a.name.lower()]
