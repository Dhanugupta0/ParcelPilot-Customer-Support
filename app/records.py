"""Scoped reads. The only door to the tables.

Every function takes a `User` and applies their account scope in the WHERE
clause. An unauthorised lookup and a genuinely missing record return the same
"not found", so a customer cannot map another tenant's order IDs by watching
which ones answer differently.
"""
from __future__ import annotations

from app import store
from app.access import User


def _scoped(sql: str, user: User, args: tuple = ()) -> tuple[str, tuple]:
    if user.scope() is None:
        return sql, args
    joiner = " AND " if " WHERE " in sql.upper() else " WHERE "
    return sql + joiner + "account_id = ?", args + (user.scope(),)


def account(user: User, account_id: str) -> dict | None:
    sql, args = _scoped("SELECT * FROM accounts WHERE account_id = ?", user,
                        (account_id,))
    # accounts scopes on its own primary key, not a foreign key
    if user.scope() and account_id != user.scope():
        return None
    return store.one("SELECT * FROM accounts WHERE account_id = ?", (account_id,))


def my_account(user: User) -> dict | None:
    return account(user, user.account_id) if user.account_id else None


def order(user: User, order_id: str) -> dict | None:
    sql, args = _scoped("SELECT * FROM orders WHERE order_id = ?", user, (order_id,))
    return store.one(sql, args)


def ticket(user: User, ticket_id: str) -> dict | None:
    sql, args = _scoped("SELECT * FROM tickets WHERE ticket_id = ?", user,
                        (ticket_id,))
    return store.one(sql, args)


def orders(user: User, account_id: str | None = None) -> list[dict]:
    where, args = [], []
    scope = user.scope() or account_id
    if scope:
        where.append("account_id = ?"); args.append(scope)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return store.rows(f"SELECT * FROM orders{clause} ORDER BY booked_at DESC", args)


def tickets(user: User, account_id: str | None = None,
            only_open: bool = False) -> list[dict]:
    where, args = [], []
    scope = user.scope() or account_id
    if scope:
        where.append("account_id = ?"); args.append(scope)
    if only_open:
        where.append("LOWER(status) = 'open'")
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return store.rows(f"SELECT * FROM tickets{clause} ORDER BY created_at DESC", args)


def accounts(user: User) -> list[dict]:
    if user.scope():
        return store.rows("SELECT * FROM accounts WHERE account_id = ?",
                          (user.scope(),))
    return store.rows("SELECT * FROM accounts ORDER BY account_name")


def resolve_account(user: User, text: str) -> dict | None:
    """Find an account by id or name fragment, within the caller's scope."""
    t = (text or "").strip().lower()
    if not t:
        return None
    for a in accounts(user):
        if t == a["account_id"].lower():
            return a
        # Match the customer's name anywhere in the sentence: "can Northstar
        # cancel ORD-1001" names the account without using its id.
        first = a["account_name"].split()[0].lower()
        if first in t or a["account_name"].lower() in t:
            return a
    return None
