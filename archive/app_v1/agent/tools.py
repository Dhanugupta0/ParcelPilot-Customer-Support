"""Tool definitions and dispatch.

Five categories, exceeding the three the brief requires:

  A. Document retrieval      search_policy_documents
  B. Structured data         lookup_account, lookup_order, lookup_tickets
  C. Deterministic decisions evaluate_policy_decision      <- the addition
  D. State-changing action   propose_action  (human-confirmed, see actions.py)
  E. Operations intelligence get_operational_signals       (internal roles only)

Category C is the one that is not asked for and matters most. Without it the
model reads a contract and a policy and does arithmetic in its head, which is
exactly how a confidently wrong answer gets produced. With it, the model's job
becomes routing and explanation, and every entitlement, amount and deadline
comes from code that can be unit-tested.

Every dispatch takes a Principal. There is no path to data that does not.
"""
from __future__ import annotations

from typing import Any, Callable

from app.core import audit, clock
from app.core.models import Severity
from app.core.principal import AccessDenied, Perm, Principal
from app.data.repository import Repository
from app.ingest.contract_terms import terms_for
from app.policy import engine
from app.policy.triage import triage
from app.retrieval.governed import GovernedRetriever

# --------------------------------------------------------------------------
# Schemas (OpenAI function-calling format)
# --------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_policy_documents",
            "description": (
                "Search ParcelPilot's policies, SOPs, product documentation and customer "
                "agreements. Returns passages ranked by relevance, each labelled with its "
                "authority tier, status and citation. Deprecated documents, resolved known "
                "issues and other customers' agreements are removed before you see them, "
                "and anything removed is reported in `excluded_sources` with the reason. "
                "If `conflicts_detected` is non-empty, two sources disagree and you must "
                "say so explicitly in your answer. Use this for any question about rules, "
                "entitlements, contract terms, known issues or process."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Natural-language search query."},
                    "account_id": {
                        "type": "string",
                        "description": ("Account the question concerns, e.g. ACCT-001. "
                                        "Pass it whenever known: it scopes contract "
                                        "clauses to the right customer.")},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_account",
            "description": (
                "Look up a ParcelPilot account by id (ACCT-001) or company name "
                "(Northstar). Returns plan, status, whether a signed agreement exists, and "
                "a summary of any contractual terms that override standard policy. Call "
                "this early: almost every entitlement question depends on the plan and on "
                "whether a contract overrides the default."),
            "parameters": {
                "type": "object",
                "properties": {
                    "account_ref": {"type": "string",
                                    "description": "Account id or company name. Omit in a "
                                                   "customer session to use their own account."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": (
                "Look up a shipment/order by id (e.g. ORD-1001). Returns status, carrier, "
                "booking time, pickup window, fee, fault flags, and derived facts such as "
                "minutes between booking and any cancellation request, and how late the "
                "pickup is. Does NOT decide anything -- use evaluate_policy_decision for that."),
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_tickets",
            "description": (
                "Look up support tickets: by id, by free-text search, or by account. "
                "Returns each ticket with an automatic severity triage explaining which "
                "policy definition it matched. Historical resolutions on closed tickets are "
                "returned only to internal staff and are always marked UNRELIABLE -- they "
                "are context, never authority, and some are known to be wrong."),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "query": {"type": "string", "description": "Free-text search."},
                    "account_id": {"type": "string"},
                    "only_open": {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_policy_decision",
            "description": (
                "Run ParcelPilot's deterministic policy engine. This is the ONLY correct "
                "way to decide a cancellation fee, a service-credit entitlement or an SLA "
                "target. Never compute these yourself and never quote an amount or a "
                "deadline that did not come from this tool. It resolves contract clauses "
                "over policy defaults automatically and returns the full rule chain with "
                "citations, any overrides applied, caveats, and a confidence level. "
                "If it returns INSUFFICIENT_DATA or LOW confidence, do not give an answer "
                "anyway -- explain what is missing and offer to escalate."),
            "parameters": {
                "type": "object",
                "properties": {
                    "decision_type": {
                        "type": "string",
                        "enum": ["cancellation", "service_credit", "sla"],
                    },
                    "order_id": {"type": "string",
                                 "description": "Required for cancellation and service_credit."},
                    "ticket_id": {"type": "string",
                                  "description": "Required for sla."},
                },
                "required": ["decision_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": (
                "Prepare a state-changing action for human confirmation. This does NOT "
                "execute anything: it returns a proposal with a preview that the user must "
                "explicitly approve in the interface before it takes effect. You have no "
                "tool that executes directly, by design. Use this when the request needs "
                "human judgment, an unsupported exception, an action outside your "
                "capability, a breached SLA, a P1 incident, or when the policy engine "
                "reports low confidence or missing data."),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": ["create_escalation", "update_ticket",
                                 "create_followup_task", "issue_service_credit"],
                    },
                    "ticket_id": {"type": "string"},
                    "order_id": {"type": "string"},
                    "account_id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
                    "reason": {"type": "string",
                               "description": "Why this action is needed, citing sources."},
                    "details": {"type": "string",
                                "description": "Task title, ticket update, or credit rationale."},
                    "amount_inr": {"type": "number",
                                   "description": "Only for issue_service_credit. Must come "
                                                  "from evaluate_policy_decision."},
                },
                "required": ["action_type", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_proactive_outreach",
            "description": (
                "Internal staff only. Returns customers who should be contacted BEFORE "
                "they complain -- uncollected shipments with a credit already computed, "
                "and accounts caught in a known-issue cluster who have not raised a "
                "ticket. Also returns who was deliberately NOT contacted and why "
                "(already raised it, outside their contractual support hours, entitlement "
                "not provable). Nothing is sent: each draft still needs human approval."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_operational_signals",
            "description": (
                "Internal staff only. Returns the current operations picture across all "
                "accounts: SLA breaches and at-risk tickets, clusters of related "
                "complaints, issues affecting multiple customers, and anomalies in order "
                "activity. Use for questions like 'what needs attention', 'what is "
                "trending', or 'which tickets are breaching'."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _accept_null_on_optionals(schemas: list[dict]) -> list[dict]:
    """Let optional parameters be sent explicitly as null.

    Groq validates every tool call against the schema before it reaches us and
    rejects a mismatch as a 400 -- which is a permanent error, so the turn dies
    outright. gpt-oss habitually fills in the parameters it was NOT given with
    `null` rather than omitting the key, and a bare `"type": "string"` makes that
    a hard failure on questions the agent could otherwise answer.

    Widening the type is the honest fix: null genuinely IS acceptable here, and
    `call_tool` drops it so the Python function sees an omitted argument exactly
    as before. Required parameters are left strict on purpose -- a null there is
    a real error we want surfaced, not absorbed.
    """
    for schema in schemas:
        params = schema["function"]["parameters"]
        required = set(params.get("required", []))
        for name, prop in params.get("properties", {}).items():
            if name in required or not isinstance(prop.get("type"), str):
                continue
            prop["type"] = [prop["type"], "null"]
            # A value must satisfy `enum` as well as `type`, so an optional enum
            # needs null listed there too or the widening above achieves nothing.
            if "enum" in prop and None not in prop["enum"]:
                prop["enum"] = [*prop["enum"], None]
    return schemas


TOOL_SCHEMAS = _accept_null_on_optionals(TOOL_SCHEMAS)


CATEGORY = {
    "get_proactive_outreach": "Operations intelligence",
    "search_policy_documents": "Document retrieval",
    "lookup_account": "Structured data",
    "lookup_order": "Structured data",
    "lookup_tickets": "Structured data",
    "evaluate_policy_decision": "Deterministic policy engine",
    "propose_action": "State-changing action (needs confirmation)",
    "get_operational_signals": "Operations intelligence",
}


# --------------------------------------------------------------------------
# Implementations
# --------------------------------------------------------------------------

def _trim(text: str, n: int = 900) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + " ..."


def t_search_policy_documents(p: Principal, query: str,
                              account_id: str | None = None) -> dict:
    if p.is_customer:
        account_id = p.account_id      # a customer's scope is never negotiable
    r = GovernedRetriever(p).search(query, account_context=account_id)
    out = r.to_dict()
    for item in out["results"]:
        item["text"] = _trim(item["text"])
    return out


def t_lookup_account(p: Principal, account_ref: str | None = None) -> dict:
    repo = Repository(p)
    if not account_ref:
        if p.is_customer:
            account_ref = p.account_id
        else:
            return {"accounts": [a.view(p) for a in repo.list_accounts()],
                    "note": "No account specified; listing all accounts in scope."}
    acct = repo.resolve_account(account_ref)
    data = acct.view(p)
    terms = terms_for(acct.account_id)
    data["signed_agreement"] = terms.summary() if terms else None
    if not terms:
        data["agreement_note"] = (
            "No signed agreement for this account in the supplied pack, so the "
            "standard Support Policy v3 and the current SOP apply in full.")
    return data


def t_lookup_order(p: Principal, order_id: str) -> dict:
    repo = Repository(p)
    order = repo.get_order(order_id)
    acct = repo.get_account(order.account_id)
    return {"order": order.view(p),
            "account": {"account_id": acct.account_id, "name": acct.account_name,
                        "plan": acct.plan.value,
                        "has_signed_agreement": acct.has_agreement},
            "reference_time": clock.fmt(clock.now()),
            "note": ("All time calculations use the dataset snapshot as 'now'. "
                     "Use evaluate_policy_decision for any fee or credit outcome.")}


def t_lookup_tickets(p: Principal, ticket_id: str | None = None,
                     query: str | None = None, account_id: str | None = None,
                     only_open: bool = False) -> dict:
    repo = Repository(p)
    if account_id:
        # A named account is a CLAIM about whose data this is, so it gets
        # checked rather than quietly narrowed to the caller's own account.
        # Silently returning ACCT-002's tickets to someone who asked about
        # ACCT-001 leaks nothing, but it invites the model to present them under
        # the wrong customer's name -- which reads exactly like a leak.
        p.assert_account_access(account_id)

    if ticket_id:
        tickets = [repo.get_ticket(ticket_id)]
    elif query:
        tickets = repo.search_tickets(query)
    else:
        tickets = repo.list_tickets(account_id=account_id, only_open=only_open)[:12]

    items = []
    for t in tickets:
        v = t.view(p)
        tr = triage(t)
        v["triage"] = {"severity": tr.severity.value, "rationale": tr.rationale,
                       "policy_source": tr.policy_source,
                       "confidence": tr.confidence, "ambiguous": tr.ambiguous}
        items.append(v)

    # State the scope that was actually applied. Without it the model has to
    # infer whose tickets these are, and a misattributed list is a support
    # incident even when the access control underneath it worked perfectly.
    if p.is_customer:
        scope = (f"Restricted to account {p.account_id}. Every ticket below belongs "
                 f"to that account. If the question named a different company, these "
                 f"results do NOT answer it -- say so rather than describing these "
                 f"tickets as theirs.")
    elif account_id:
        scope = f"Restricted to account {account_id}."
    else:
        scope = "All accounts."
    return {"count": len(items), "scope": scope, "tickets": items,
            "reference_time": clock.fmt(clock.now())}


# Topic phrasing used to run conflict detection on the DECISION path.
_DECISION_PROBE = {
    "cancellation": "cancellation fee waiver for a booked shipment before pickup",
    "service_credit": "failed pickup service credit eligibility threshold and amount",
    "sla": "first response target severity escalation",
}


def _decision_context(p: Principal, decision_type: str,
                      account_id: str) -> dict:
    """Run governed retrieval alongside a deterministic decision.

    Conflict detection and citation collection used to live only in the
    retrieval tool, which meant they fired only if the model happened to search
    before deciding. On a well-posed question it often goes straight to the
    engine -- and then the single most important trust signal in the system
    (a past resolution contradicting the contract) silently never ran.

    A trust guarantee must not depend on the model's routing choices, so the
    decision path carries its own.
    """
    try:
        res = GovernedRetriever(p).search(_DECISION_PROBE.get(decision_type, decision_type),
                                          top_k=4, account_context=account_id)
    except Exception:                                             # noqa: BLE001
        return {}
    out: dict = {}
    if res.conflicts:
        out["conflicts_detected"] = [c.__dict__ for c in res.conflicts]
        out["conflict_instruction"] = (
            "MANDATORY: a source conflict was found. Your answer MUST include a "
            "sentence naming the overruled source and stating that it is incorrect "
            "or superseded — for example 'note that ticket TKT-450 previously told "
            "this customer otherwise; that answer was incorrect'. An agent about to "
            "repeat a past answer is exactly who needs this warning. Do not omit it "
            "and do not quietly pick a side.")
    if res.results:
        out["supporting_sources"] = [
            {"citation": c.citation, "authority_tier": c.authority_tier,
             "status": c.status, "text": _trim(c.text, 400)} for c in res.results]
    if res.excluded:
        out["excluded_sources"] = [
            {"citation": e.citation, "reason": e.reason} for e in res.excluded]
    return out


def t_evaluate_policy_decision(p: Principal, decision_type: str,
                               order_id: str | None = None,
                               ticket_id: str | None = None) -> dict:
    repo = Repository(p)
    if decision_type in ("cancellation", "service_credit"):
        if not order_id:
            return {"error": f"{decision_type} requires an order_id."}
        order = repo.get_order(order_id)
        acct = repo.get_account(order.account_id)
        fn = (engine.decide_cancellation if decision_type == "cancellation"
              else engine.decide_service_credit)
        d = fn(order, acct)
        return {"decision": d.model_dump(mode="json"),
                **_decision_context(p, decision_type, acct.account_id),
                "instruction": ("Report `headline` as the answer. Explain using "
                                "`rule_chain` and name any `overrides_applied`. State "
                                "every caveat. Do not restate any amount not present here.")}
    if decision_type == "sla":
        if not ticket_id:
            return {"error": "sla requires a ticket_id."}
        t = repo.get_ticket(ticket_id)
        acct = repo.get_account(t.account_id)
        tr = triage(t)
        s = engine.resolve_sla(acct, tr.severity, t.created_at, subject=t.ticket_id)
        return {"triage": {"severity": tr.severity.value, "rationale": tr.rationale,
                           "ambiguous": tr.ambiguous},
                "sla": s.model_dump(mode="json"),
                **_decision_context(p, "sla", acct.account_id),
                "instruction": ("If `breached` is true, say so plainly and recommend "
                                "escalation -- Support Policy v3 §4 requires it. If "
                                "`clock_running` is false, explain that the business "
                                "clock has not started yet.")}
    return {"error": f"Unknown decision_type: {decision_type}"}


def t_propose_action(p: Principal, action_type: str, reason: str,
                     ticket_id: str | None = None, order_id: str | None = None,
                     account_id: str | None = None, severity: str | None = None,
                     details: str | None = None,
                     amount_inr: float | None = None) -> dict:
    from app.agent import actions as A
    repo = Repository(p)
    at = A.ActionType(action_type)

    # Resolve the owning account from the referenced record so a caller cannot
    # attach an action to an account they do not have access to.
    resolved_account = account_id
    subject_desc = ""
    if ticket_id:
        t = repo.get_ticket(ticket_id)
        resolved_account, subject_desc = t.account_id, f"{t.ticket_id}: {t.subject}"
    elif order_id:
        o = repo.get_order(order_id)
        resolved_account, subject_desc = o.account_id, f"{o.order_id} ({o.status.value})"
    elif account_id:
        resolved_account = repo.get_account(account_id).account_id
    elif p.is_customer:
        resolved_account = p.account_id

    warnings: list[str] = []
    if at is A.ActionType.ISSUE_SERVICE_CREDIT and amount_inr is not None:
        from app.policy.rules import defaults
        line = float(defaults().manager_approval_above_inr.value)
        if amount_inr > line:
            warnings.append(
                f"INR {amount_inr:,.0f} exceeds the INR {line:,.0f} manager-approval "
                f"threshold in the SOP; a manager must approve this.")

    preview = {k: v for k, v in {
        "action": at.value, "ticket_id": ticket_id, "order_id": order_id,
        "account_id": resolved_account, "subject": subject_desc,
        "severity": severity, "details": details,
        "amount_inr": amount_inr, "reason": reason,
        "raised_by": p.display_name,
        "prepared_at": clock.fmt(clock.now()),
    }.items() if v is not None}

    label = at.value.replace("_", " ").title()
    summary = f"{label} — {subject_desc or resolved_account or 'general'}"

    proposal = A.propose(at, preview, p, summary=summary, preview=preview,
                         warnings=warnings, account_id=resolved_account)
    return {
        "proposal_id": proposal.proposal_id,
        "status": "PREPARED - NOT YET EXECUTED",
        "summary": proposal.summary,
        "preview": proposal.preview,
        "warnings": proposal.warnings,
        "expires_at": proposal.expires_at,
        "instruction": (
            "Nothing has been created. Show the user exactly what will happen and ask "
            "them to confirm using the Confirm button on the proposal card. Do not claim "
            "the action is done, and do not attempt to execute it yourself -- you have no "
            "tool that can."),
    }


def t_get_proactive_outreach(p: Principal) -> dict:
    p.require(Perm.VIEW_SIGNALS, "view proactive outreach")
    from app.outreach.engine import build_outreach
    plan = build_outreach(p)
    return {
        "stats": plan.stats,
        "drafts": [{"candidate_id": d.candidate_id, "kind": d.kind_label,
                    "account": d.account_name, "subject": d.subject,
                    "credit_inr": d.entitlement_inr, "facts": d.facts,
                    "citations": d.citations} for d in plan.drafts],
        "suppressed": [s_.model_dump(mode="json") for s_ in plan.suppressed],
        "instruction": ("Summarise who should be contacted and why, and mention the "
                        "suppressed cases -- deciding NOT to contact someone is a "
                        "deliberate decision worth reporting. Nothing sends without "
                        "approval on the Outreach tab."),
    }


def t_get_operational_signals(p: Principal) -> dict:
    p.require(Perm.VIEW_SIGNALS, "view the operations signal board")
    from app.signals.detectors import build_signals
    return build_signals(p).to_dict()


DISPATCH: dict[str, Callable[..., dict]] = {
    "get_proactive_outreach": t_get_proactive_outreach,
    "search_policy_documents": t_search_policy_documents,
    "lookup_account": t_lookup_account,
    "lookup_order": t_lookup_order,
    "lookup_tickets": t_lookup_tickets,
    "evaluate_policy_decision": t_evaluate_policy_decision,
    "propose_action": t_propose_action,
    "get_operational_signals": t_get_operational_signals,
}


def call_tool(name: str, args: dict, principal: Principal) -> dict:
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    # An explicit null and an omitted argument mean the same thing to every tool
    # here, but only the omission preserves the Python default. See
    # `_accept_null_on_optionals` for why the model sends nulls at all.
    args = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        result = fn(principal, **args)
        audit.record("tool.call", principal, tool=name, args=args, ok=True)
        return result
    except AccessDenied as e:
        audit.record("tool.call.denied", principal, tool=name, args=args,
                     internal_reason=e.internal_reason)
        return {"access_denied": True, "message": e.message,
                "instruction": ("Tell the user plainly that this is outside what they can "
                                "access, without speculating about the underlying record. "
                                "Offer to escalate to a ParcelPilot support agent.")}
    except Exception as e:      # noqa: BLE001 - surface, never crash the turn
        audit.record("tool.call.error", principal, tool=name, args=args, error=str(e))
        return {"error": f"{type(e).__name__}: {e}",
                "instruction": "Tell the user the lookup failed and offer to escalate."}
