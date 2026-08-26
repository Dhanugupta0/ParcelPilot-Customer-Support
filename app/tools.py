"""The tools the agent chooses between.

Four categories, exceeding the three the brief requires:

  A. Document retrieval   search_policies
  B. Structured data      lookup_order, lookup_ticket, lookup_account, list_tickets
  C. Deterministic calc   calculate            <- the one that matters most
  D. State-changing       propose_action       (human-confirmed; see actions.py)

Category C is the reason the model can be trusted. The agent decides WHICH
calculation is relevant; `engine.py` decides what the answer is. So the model
routes and explains, and every fee, credit and deadline comes from code that a
unit test pins to a clause in the document pack.

Every tool takes a `User`. There is no path to a record that has not been
scoped — access control lives here, in the data layer, not in the prompt.
"""
from __future__ import annotations

from typing import Any, Callable

from app import actions, engine, knowledge, records, store
from app.access import User

CATEGORY = {
    "search_policies": "Document retrieval",
    "lookup_order": "Structured data",
    "lookup_ticket": "Structured data",
    "lookup_account": "Structured data",
    "list_tickets": "Structured data",
    "calculate": "Deterministic calculation",
    "propose_action": "State-changing action (needs confirmation)",
}

# Shown to a customer instead of the tool name: they do not need our vocabulary.
FRIENDLY = {
    "search_policies": "Checking ParcelPilot's policies",
    "lookup_order": "Looking up your order",
    "lookup_ticket": "Checking your support ticket",
    "lookup_account": "Checking your account",
    "list_tickets": "Reviewing your tickets",
    "calculate": "Applying the policy rules",
    "propose_action": "Preparing something for your confirmation",
}

SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "search_policies",
        "description": (
            "Search ParcelPilot's policies, SOPs, product documentation and customer "
            "agreements. Returns passages ranked by AUTHORITY first (signed agreement "
            "> current policy > product docs) and similarity second. Deprecated "
            "documents and other customers' agreements are removed before you see "
            "them and reported in `excluded`. Use for any question about rules, "
            "entitlements, contract terms or known issues."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Natural-language search."},
            "account_id": {"type": ["string", "null"],
                           "description": "Scopes contract clauses to one customer."},
        }, "required": ["query"]}}},

    {"type": "function", "function": {
        "name": "lookup_order",
        "description": "Fetch one order by id (e.g. ORD-1001).",
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string"}}, "required": ["order_id"]}}},

    {"type": "function", "function": {
        "name": "lookup_ticket",
        "description": "Fetch one support ticket by id (e.g. TKT-505).",
        "parameters": {"type": "object", "properties": {
            "ticket_id": {"type": "string"}}, "required": ["ticket_id"]}}},

    {"type": "function", "function": {
        "name": "lookup_account",
        "description": (
            "Fetch an account by id (ACCT-001) or by customer name ('Northstar'). "
            "Returns the plan and whether a signed agreement exists."),
        "parameters": {"type": "object", "properties": {
            "account_ref": {"type": ["string", "null"]}}, "required": []}}},

    {"type": "function", "function": {
        "name": "list_tickets",
        "description": "List tickets, optionally only open ones, for an account.",
        "parameters": {"type": "object", "properties": {
            "account_id": {"type": ["string", "null"]},
            "only_open": {"type": ["boolean", "null"]}}, "required": []}}},

    {"type": "function", "function": {
        "name": "calculate",
        "description": (
            "THE ONLY WAY to obtain a fee, a credit amount, an SLA target or a breach. "
            "Runs ParcelPilot's deterministic policy engine and returns the outcome, "
            "the amount, and the rule chain naming the clause behind each step — "
            "including where a signed agreement overrode the default policy. "
            "You must NEVER compute any of these yourself: reading a clause is not "
            "the same as applying it, and the engine is what resolves precedence, "
            "runs the business-hours clock and knows the approval threshold. "
            "kind='cancellation' or 'service_credit' need an order_id; kind='sla' "
            "needs a ticket_id."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string",
                     "enum": ["cancellation", "service_credit", "sla"]},
            "order_id": {"type": ["string", "null"]},
            "ticket_id": {"type": ["string", "null"]},
        }, "required": ["kind"]}}},

    {"type": "function", "function": {
        "name": "propose_action",
        "description": (
            "PREPARE a state-changing action for a human to confirm. Nothing is "
            "executed by this call and you have no tool that can execute one. After "
            "calling it, tell the user exactly what will happen and ask them to press "
            "Confirm. Never claim the action has been taken."),
        "parameters": {"type": "object", "properties": {
            "action_type": {"type": "string",
                            "enum": ["create_escalation", "update_ticket",
                                     "create_followup_task", "issue_service_credit"]},
            "reason": {"type": "string", "description": "Why, citing the source."},
            "ticket_id": {"type": ["string", "null"]},
            "order_id": {"type": ["string", "null"]},
            "severity": {"type": ["string", "null"], "enum": ["P1", "P2", "P3", None]},
            "details": {"type": ["string", "null"]},
            "amount_inr": {"type": ["number", "null"],
                           "description": "Only for issue_service_credit, and it must "
                                          "come from calculate() — never your own sum."},
        }, "required": ["action_type", "reason"]}}},
]


# --------------------------------------------------------------------------
# Implementations
# --------------------------------------------------------------------------

def _search_policies(user: User, query: str, account_id: str | None = None) -> dict:
    scope = user.scope() or account_id
    passages, excluded = knowledge.index().search(
        query, account_id=scope, limit=5,
        all_tenants=user.is_internal and scope is None)
    return {"passages": [p.to_dict() for p in passages],
            "excluded": excluded,
            "note": ("`excluded` lists sources that matched but were filtered. "
                     "If a deprecated policy was excluded and it contradicts your "
                     "answer, say so explicitly.")}


def _lookup_order(user: User, order_id: str) -> dict:
    o = records.order(user, order_id)
    if o is None:
        return {"not_found": True, "message":
                f"No order {order_id} is visible in this session."}
    return {"order": o}


def _lookup_ticket(user: User, ticket_id: str) -> dict:
    t = records.ticket(user, ticket_id)
    if t is None:
        return {"not_found": True, "message":
                f"No ticket {ticket_id} is visible in this session."}
    sev, why = engine.triage(t["subject"] or "", t["description"] or "")
    out = {"ticket": t, "triaged_severity": sev, "triage_rationale": why}
    if t.get("historical_resolution"):
        out["warning"] = (
            "`historical_resolution` is CONTEXT ONLY and the dataset README warns "
            "some past resolutions are incorrect. Never present it as the rule. If "
            "it disagrees with the current policy or the agreement, say it was wrong.")
    return out


def _lookup_account(user: User, account_ref: str | None = None) -> dict:
    if not account_ref:
        a = records.my_account(user)
        if a:
            return {"account": a, "has_agreement": a["account_id"] in engine.CONTRACTS}
        return {"accounts": records.accounts(user)}
    a = records.resolve_account(user, account_ref) or records.account(user, account_ref)
    if a is None:
        return {"not_found": True,
                "message": f"No account matching {account_ref!r} in this session."}
    return {"account": a, "has_agreement": a["account_id"] in engine.CONTRACTS,
            "agreement": (engine.CONTRACTS[a["account_id"]].cite
                          if a["account_id"] in engine.CONTRACTS else None)}


def _list_tickets(user: User, account_id: str | None = None,
                  only_open: bool | None = None) -> dict:
    rows = records.tickets(user, account_id, only_open=bool(only_open))
    for r in rows:
        r["triaged_severity"] = engine.triage(r["subject"] or "",
                                              r["description"] or "")[0]
    return {"tickets": rows, "count": len(rows)}


def _calculate(user: User, kind: str, order_id: str | None = None,
               ticket_id: str | None = None) -> dict:
    """The deterministic path. The agent picks `kind`; the engine decides."""
    if kind in ("cancellation", "service_credit"):
        if not order_id:
            return {"error": f"{kind} needs an order_id."}
        if records.order(user, order_id) is None:
            return {"access_denied": True,
                    "message": f"{order_id} is not visible in this session."}
        d = (engine.cancellation(order_id) if kind == "cancellation"
             else engine.service_credit(order_id))
    elif kind == "sla":
        if not ticket_id:
            return {"error": "sla needs a ticket_id."}
        if records.ticket(user, ticket_id) is None:
            return {"access_denied": True,
                    "message": f"{ticket_id} is not visible in this session."}
        d = engine.sla(ticket_id)
    else:
        return {"error": f"Unknown calculation {kind!r}."}
    return {"decision": d.to_dict(),
            "instruction": ("Report these figures exactly as given. Do not round, "
                            "convert, divide or otherwise derive a new number from "
                            "them. If a signed agreement overrode the default, say so.")}


def _propose_action(user: User, action_type: str, reason: str, **kw) -> dict:
    try:
        p = actions.propose(user, action_type, reason, **kw)
    except actions.Refused as e:
        return {"refused": True, "message": str(e)}
    return {"proposal_id": p.proposal_id, "status": "PREPARED — NOT EXECUTED",
            "summary": p.summary, "preview": p.preview,
            "requires_manager": p.requires_manager,
            "instruction": ("Nothing has been created. Show the user what will "
                            "happen and ask them to press Confirm. Do not say it "
                            "is done.")}


DISPATCH: dict[str, Callable[..., dict]] = {
    "search_policies": _search_policies,
    "lookup_order": _lookup_order,
    "lookup_ticket": _lookup_ticket,
    "lookup_account": _lookup_account,
    "list_tickets": _list_tickets,
    "calculate": _calculate,
    "propose_action": _propose_action,
}


def available(user: User) -> list[dict]:
    """Tool AVAILABILITY is itself an access-control surface.

    Removing a capability from the schema is stronger than declining it at call
    time: the model cannot be talked into invoking something it was never
    offered.
    """
    if user.is_internal:
        return SCHEMAS
    return [s for s in SCHEMAS if s["function"]["name"] != "list_tickets"]


def call(name: str, args: dict, user: User) -> dict:
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"Unknown tool {name!r}."}
    args = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        return fn(user, **args)
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}
    except Exception as e:                                        # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def summarise(name: str, result: dict) -> str:
    """One line per call, for the trace panel."""
    if result.get("access_denied") or result.get("not_found"):
        return result.get("message", "not available in this session")
    if result.get("error"):
        return f"error: {str(result['error'])[:80]}"
    if name == "search_policies":
        n, x = len(result.get("passages", [])), len(result.get("excluded", []))
        return f"{n} passage(s)" + (f", {x} source(s) excluded" if x else "")
    if name == "calculate":
        d = result.get("decision", {})
        amt = d.get("amount_inr")
        return (f"{d.get('outcome')}"
                + (f" · INR {amt:,.0f}" if amt is not None else "")
                + f" · via {d.get('authority_used')}")
    if name == "propose_action":
        return f"{result.get('proposal_id')} prepared — awaiting confirmation"
    if name in ("lookup_order", "lookup_ticket"):
        rec = result.get("order") or result.get("ticket") or {}
        return " · ".join(str(v) for v in list(rec.values())[:3])
    if name == "lookup_account":
        a = result.get("account", {})
        return f"{a.get('account_name')} · {a.get('plan')}" if a else "listed"
    if name == "list_tickets":
        return f"{result.get('count', 0)} ticket(s)"
    return "done"
