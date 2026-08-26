"""Who is asking, and what they may see.

Deliberately tiny. There are two kinds of user in this product and one rule
that matters: a customer sees their own account and nothing else. That rule is
enforced by passing a `User` into every data call, so there is no path to a
record that has not been scoped -- rather than by asking the model to behave.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: str                      # "customer" | "agent" | "manager"
    account_id: str | None = None

    @property
    def is_customer(self) -> bool:
        return self.role == "customer"

    @property
    def is_internal(self) -> bool:
        return self.role in ("agent", "manager")

    @property
    def may_approve(self) -> bool:
        """SOP v4 §3: credits above INR 1,000 need a manager."""
        return self.role == "manager"

    def scope(self) -> str | None:
        """The account_id a query must be restricted to, or None for all."""
        return self.account_id if self.is_customer else None


DIRECTORY: dict[str, User] = {
    "cust_northstar": User("cust_northstar", "Ravi Menon", "customer", "ACCT-001"),
    "cust_lumenworks": User("cust_lumenworks", "Sara Iyer", "customer", "ACCT-002"),
    "cust_beacon": User("cust_beacon", "Dev Sharma", "customer", "ACCT-003"),
    "cust_axis": User("cust_axis", "Meera Nair", "customer", "ACCT-004"),
    "agent_rohit": User("agent_rohit", "Rohit", "agent"),
    "agent_maya": User("agent_maya", "Maya", "agent"),
    "mgr_priya": User("mgr_priya", "Priya Mehta", "manager"),
}


class Denied(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def get(user_key: str) -> User:
    u = DIRECTORY.get(user_key)
    if u is None:
        raise Denied(f"Unknown user {user_key!r}.")
    return u
