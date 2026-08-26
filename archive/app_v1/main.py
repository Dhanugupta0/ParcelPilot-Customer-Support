"""FastAPI application: chat API, confirmation endpoints, signal board, UI."""
from __future__ import annotations

import json
import uuid
from typing import Iterator

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.agent import actions as A
from app.agent.loop import Session, run_turn
from app.core import audit, clock
from app.core.principal import DIRECTORY, AccessDenied, load_principal
from app.data.repository import Repository, dataset
from app.store import db, recorder, vectors

app = FastAPI(title="ParcelPilot Support Intelligence", version="1.0")

SESSIONS: dict[str, Session] = {}


@app.on_event("startup")
def _warm() -> None:
    """Fail fast and loudly at boot rather than on the first user question."""
    from app.ingest.contract_terms import terms_by_account
    from app.policy.rules import defaults
    from app.retrieval.index import index
    ds = dataset()
    defaults()                       # asserts every policy rule was parsed
    terms_by_account()
    index()
    print(f"[startup] snapshot={clock.fmt(ds.snapshot)}  accounts={len(ds.accounts)}  "
          f"orders={len(ds.orders)}  tickets={len(ds.tickets)}")


# --------------------------------------------------------------------------
# Session / identity
# --------------------------------------------------------------------------

@app.get("/api/users")
def list_users() -> dict:
    """The mocked identity directory the UI's session switcher offers."""
    return {"users": [
        {"key": k, "display_name": v["display_name"], "role": v["role"].value,
         "account_id": v.get("account_id")} for k, v in DIRECTORY.items()]}


@app.post("/api/session")
def create_session(payload: dict = Body(...)) -> dict:
    """Open a browser session against a durable conversation.

    Pass `conversation_id` to resume an existing thread -- its history is
    replayed into the model's context, so picking a conversation back up
    continues it rather than starting again with amnesia.
    """
    user_key = payload.get("user_key", "")
    try:
        principal = load_principal(user_key)
    except AccessDenied as e:
        raise HTTPException(status_code=403, detail=e.message) from None

    sid = uuid.uuid4().hex[:12]
    context = payload.get("context") or None
    resume_id = payload.get("conversation_id")

    if resume_id:
        convo = db.get_conversation(resume_id)
        if convo is None:
            raise HTTPException(status_code=404, detail="No such conversation.")
        # Resuming someone else's thread would be a cross-tenant read through
        # the back door, so it is checked here and not only in the UI.
        if principal.is_customer and convo["user_id"] != principal.user_id:
            raise HTTPException(status_code=403,
                                detail="That conversation belongs to another user.")
        cid = resume_id
        if convo.get("context_ref"):
            context = {"kind": convo.get("context_kind"),
                       "ref": convo["context_ref"], "label": convo.get("title")}
    else:
        cid = db.create_conversation(
            principal, account_name=_account_name(principal.account_id),
            context_kind=(context or {}).get("kind"),
            context_ref=(context or {}).get("ref"),
            title=(context or {}).get("label"))

    session = Session(session_id=sid, principal=principal,
                      conversation_id=cid, context=context)
    if resume_id:
        session.ensure_system()
        for m in db.transcript(cid)["messages"]:
            session.messages.append({"role": m["role"], "content": m["content"]})
    SESSIONS[sid] = session

    audit.record("session.created", principal, session_id=sid, conversation_id=cid,
                 resumed=bool(resume_id))
    return {"session_id": sid, "user_id": principal.user_id,
            "display_name": principal.display_name,
            "role": principal.role.value, "account_id": principal.account_id,
            "scope": principal.scope_label(), "conversation_id": cid,
            "resumed": bool(resume_id), "context": context,
            "history": db.transcript(cid)["messages"] if resume_id else [],
            "snapshot": clock.fmt(clock.now())}


def _account_name(account_id: str | None) -> str | None:
    if not account_id:
        return None
    for a in dataset().accounts.values():
        if a.account_id == account_id:
            return a.account_name
    return None


def _session(sid: str) -> Session:
    s = SESSIONS.get(sid or "")
    if s is None:
        raise HTTPException(status_code=404, detail="Session not found. Reload the page.")
    return s


# --------------------------------------------------------------------------
# Chat (server-sent events so the tool trace appears as it happens)
# --------------------------------------------------------------------------

@app.post("/api/chat")
def chat(payload: dict = Body(...)) -> StreamingResponse:
    session = _session(payload.get("session_id", ""))
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")

    def gen() -> Iterator[str]:
        try:
            stream = recorder.record_turn(session.conversation_id, session.principal,
                                          message, run_turn(session, message))
            for event in stream:
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except Exception as e:                                    # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# Confirmation gate
#
# This is the only route to a state change, and the model has no way to reach
# it. It is called by the Confirm button in the interface, by a human.
# --------------------------------------------------------------------------

@app.get("/api/proposals/{proposal_id}")
def get_proposal(proposal_id: str, session_id: str) -> dict:
    _session(session_id)
    p = A.get(proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="No such proposal")
    return p.model_dump(mode="json")


@app.post("/api/proposals/{proposal_id}/confirm")
def confirm_proposal(proposal_id: str, payload: dict = Body(...)) -> dict:
    session = _session(payload.get("session_id", ""))
    try:
        p = A.commit(proposal_id, session.principal)
    except AccessDenied as e:
        db.set_escalation_status(proposal_id, "denied")
        raise HTTPException(status_code=403, detail=e.message) from None
    # An escalation that a human actually confirmed is the one the employee
    # dashboard must show as live, so the store learns about it here -- on the
    # only path that can change state.
    db.set_escalation_status(proposal_id, "committed", p.committed_ref)
    # Feed the outcome back into the conversation so the assistant's next reply
    # reflects what actually happened rather than what it proposed.
    session.messages.append({
        "role": "user",
        "content": (f"[SYSTEM] The human confirmed proposal {p.proposal_id}. It has been "
                    f"executed as {p.committed_ref}. Acknowledge this briefly and state "
                    f"the reference. Do not repeat the full details."),
    })
    return {"status": "committed", "reference": p.committed_ref,
            "proposal": p.model_dump(mode="json")}


@app.post("/api/proposals/{proposal_id}/cancel")
def cancel_proposal(proposal_id: str, payload: dict = Body(...)) -> dict:
    session = _session(payload.get("session_id", ""))
    p = A.cancel(proposal_id, session.principal)
    db.set_escalation_status(proposal_id, "declined")
    session.messages.append({
        "role": "user",
        "content": (f"[SYSTEM] The human declined proposal {p.proposal_id}. Nothing was "
                    f"executed. Acknowledge briefly and offer an alternative."),
    })
    return {"status": "cancelled", "proposal": p.model_dump(mode="json")}


# --------------------------------------------------------------------------
# Signal board + audit
# --------------------------------------------------------------------------

@app.get("/api/signals")
def signals(session_id: str) -> dict:
    session = _session(session_id)
    from app.agent.tools import t_get_operational_signals
    try:
        return t_get_operational_signals(session.principal)
    except AccessDenied as e:
        raise HTTPException(status_code=403, detail=e.message) from None


@app.get("/api/outreach")
def outreach(session_id: str) -> dict:
    session = _session(session_id)
    from app.outreach.engine import build_outreach
    try:
        return build_outreach(session.principal).to_dict()
    except AccessDenied as e:
        raise HTTPException(status_code=403, detail=e.message) from None


@app.post("/api/outreach/propose")
def propose_outreach(payload: dict = Body(...)) -> dict:
    """Turn selected drafts into proposals.

    Deliberately routed through the SAME two-phase gate as every other state
    change: this endpoint creates pending proposals and sends nothing. Approving
    a batch still means confirming each proposal, because "approve all" must not
    become a way to bypass the confirmation the rest of the system enforces.
    """
    session = _session(payload.get("session_id", ""))
    wanted = set(payload.get("candidate_ids") or [])
    from app.outreach.engine import build_outreach

    try:
        plan = build_outreach(session.principal)
    except AccessDenied as e:
        raise HTTPException(status_code=403, detail=e.message) from None

    created = []
    for d in plan.drafts:
        if wanted and d.candidate_id not in wanted:
            continue
        params = {
            "outreach_kind": d.kind, "account_id": d.account_id,
            "topic_hint": d.citations[0] if d.citations else d.subject,
            "subject": d.subject, "body": d.body,
            "credit_inr": d.entitlement_inr, "candidate_id": d.candidate_id,
        }
        try:
            prop = A.propose(A.ActionType.SEND_CUSTOMER_OUTREACH, params,
                             session.principal,
                             summary=f"Send to {d.account_name} — {d.subject}",
                             preview={"to": d.account_name, "subject": d.subject,
                                      "credit_inr": d.entitlement_inr,
                                      "body": d.body},
                             warnings=d.warnings, account_id=d.account_id)
        except AccessDenied as e:
            raise HTTPException(status_code=403, detail=e.message) from None
        created.append({"proposal_id": prop.proposal_id,
                        "candidate_id": d.candidate_id,
                        "account": d.account_name, "subject": d.subject,
                        "expires_at": prop.expires_at})
    return {"proposals": created, "count": len(created),
            "note": "Nothing has been sent. Confirm each proposal to execute."}


@app.get("/api/audit")
def audit_tail(session_id: str, limit: int = 40) -> dict:
    session = _session(session_id)
    from app.core.principal import Perm
    if not session.principal.can(Perm.READ_INTERNAL_FIELDS):
        raise HTTPException(status_code=403, detail="Not available in a customer session.")
    return {"entries": audit.tail(limit)}


# --------------------------------------------------------------------------
# Conversations
#
# Two audiences with different rights. A customer may read their OWN threads.
# Internal staff may read every thread, because reviewing what the assistant
# told a customer is the point -- but that is a privilege, so it is checked
# against the principal here rather than assumed from the URL.
# --------------------------------------------------------------------------

def _may_review(principal) -> bool:
    from app.core.principal import Perm
    return principal.can(Perm.READ_INTERNAL_FIELDS)


@app.get("/api/conversations")
def list_conversations(session_id: str, scope: str = "mine",
                       limit: int = 60) -> dict:
    """scope: mine | customers | all"""
    session = _session(session_id)
    p = session.principal

    if p.is_customer or scope == "mine":
        rows = db.list_conversations(user_id=p.user_id, limit=limit)
        # A customer's own list is the only place an unused thread is harmless,
        # but it is still noise -- filtered in db.list_conversations by default.
    elif not _may_review(p):
        raise HTTPException(status_code=403, detail="Not available in this session.")
    elif scope == "customers":
        rows = db.list_conversations(role="customer", limit=limit)
    else:
        rows = db.list_conversations(limit=limit)

    return {"conversations": rows, "scope": scope,
            "current": session.conversation_id}


@app.get("/api/conversations/search")
def search_conversations(session_id: str, q: str, limit: int = 10) -> dict:
    """Semantic search across conversations.

    The tenant filter is passed explicitly for a customer: the vector table has
    no access control of its own, so isolation has to be applied by the caller
    that knows who is asking.
    """
    session = _session(session_id)
    p = session.principal
    if not q.strip():
        return {"results": [], "query": q, "semantic": vectors.available()}
    if p.is_customer:
        results = vectors.search(q, limit=limit, account_id=p.account_id)
        results = [r for r in results if r["user_id"] == p.user_id]
    elif _may_review(p):
        results = vectors.search(q, limit=limit)
    else:
        raise HTTPException(status_code=403, detail="Not available in this session.")
    audit.record("conversations.search", p, query=q[:200], hits=len(results))
    return {"results": results, "query": q, "semantic": vectors.available()}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, session_id: str) -> dict:
    """The full audit view of one conversation.

    Returns the transcript AND the evidence behind it -- every tool call with
    its arguments and raw result, the citations, the derived confidence. A
    reviewer asking "why did it say that" needs the working, not the prose.
    """
    session = _session(session_id)
    p = session.principal
    data = db.transcript(conversation_id)
    if not data:
        raise HTTPException(status_code=404, detail="No such conversation.")
    if p.is_customer and data["conversation"]["user_id"] != p.user_id:
        raise HTTPException(status_code=403, detail="That conversation is not yours.")
    if not p.is_customer and not _may_review(p):
        raise HTTPException(status_code=403, detail="Not available in this session.")

    # A customer reading their own thread gets the transcript, never the
    # internal machinery behind it.
    if p.is_customer:
        data.pop("tool_calls", None)
        data.pop("escalations", None)
        for m in data["messages"]:
            for k in ("reasons", "conflicts", "confidence", "band"):
                m.pop(k, None)
    else:
        audit.record("conversation.reviewed", p, conversation_id=conversation_id)
    return data


# --------------------------------------------------------------------------
# Escalations
# --------------------------------------------------------------------------

@app.get("/api/escalations")
def list_escalations(session_id: str, limit: int = 100) -> dict:
    session = _session(session_id)
    if not _may_review(session.principal):
        raise HTTPException(status_code=403, detail="Not available in this session.")
    rows = db.list_escalations(limit)
    for r in rows:
        if not r.get("account_name"):
            r["account_name"] = _account_name(r.get("account_id"))
    open_n = sum(1 for r in rows if r["status"] == "proposed")
    return {"escalations": rows, "counts": {
        "open": open_n, "committed": sum(1 for r in rows if r["status"] == "committed"),
        "total": len(rows)}}


# --------------------------------------------------------------------------
# What the customer needs help with
# --------------------------------------------------------------------------

@app.get("/api/my-issues")
def my_issues(session_id: str) -> dict:
    """The customer's own open tickets and live orders, for the picker.

    Everything here goes through the same Repository as the agent's tools, so
    the list a customer is offered is scoped by exactly the same access rules
    that would apply if they asked about it in words.
    """
    session = _session(session_id)
    p = session.principal
    repo = Repository(p)

    tickets = []
    for t in repo.list_tickets():
        tickets.append({
            "kind": "ticket", "ref": t.ticket_id, "label": t.subject,
            "status": t.status,
            "created_at": clock.fmt(t.created_at) if t.created_at else None,
            # `is_open` covers pending/in_progress too, not just the literal
            # string "open" -- the model already knows this, so ask it.
            "open": t.is_open})
    tickets.sort(key=lambda x: (not x["open"], x["created_at"] or ""))

    orders = []
    for o in repo.list_orders():
        orders.append({
            "kind": "order", "ref": o.order_id,
            "label": f"{o.status.value} · {o.carrier}"
                     + (f" · pickup {clock.fmt(o.pickup_window_end)}"
                        if o.pickup_window_end else ""),
            "status": o.status.value, "carrier": o.carrier,
            "open": not o.pickup_occurred})
    orders.sort(key=lambda x: not x["open"])

    return {"tickets": tickets[:12], "orders": orders[:12],
            "account_id": p.account_id, "account_name": _account_name(p.account_id)}


@app.get("/api/health")
def health() -> dict:
    ds = dataset()
    return {"ok": True, "snapshot": clock.fmt(ds.snapshot),
            "provider": "groq",
            "store": db.stats(),
            "semantic_search": vectors.available(),
            "model": config.GROQ_MODEL,
            "utility_model": config.GROQ_UTILITY_MODEL,
            "api_key_configured": bool(config.GROQ_API_KEY),
            "counts": {"accounts": len(ds.accounts), "orders": len(ds.orders),
                       "tickets": len(ds.tickets)}}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

STATIC = config.ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC / "index.html")
