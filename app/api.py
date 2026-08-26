"""HTTP layer. Thin on purpose — the product lives in engine.py and pipeline.py.

Every endpoint takes a session, resolves it to a `User`, and passes that user
into the data layer. There is no route that reads a record without a scope.
"""
from __future__ import annotations

import json
import uuid

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app import access, actions, agent, engine, knowledge, records, store, verify
from app.access import Denied, User

app = FastAPI(title="ParcelPilot Support", version="2.0")

SESSIONS: dict[str, dict] = {}


@app.on_event("startup")
def _boot() -> None:
    """Load the pack and fail loudly if any of it is wrong.

    Doing this at boot rather than lazily means a broken dataset is discovered
    by whoever started the process, not by the first customer to ask a question.
    """
    counts = store.load_workbook()
    idx = knowledge.index()
    snap = store.snapshot()
    print(f"[boot] snapshot={snap:%Y-%m-%d %H:%M %Z} records={counts} "
          f"policy_chunks={len(idx.chunks)}")


def _session(sid: str) -> tuple[User, dict]:
    s = SESSIONS.get(sid or "")
    if s is None:
        raise HTTPException(404, "Session not found. Reload the page.")
    return s["user"], s


def _internal(user: User) -> None:
    if not user.is_internal:
        raise HTTPException(403, "Not available in a customer session.")


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

@app.get("/api/users")
def users() -> dict:
    return {"users": [{"key": k, "name": u.name, "role": u.role,
                       "account_id": u.account_id}
                      for k, u in access.DIRECTORY.items()]}


@app.post("/api/session")
def create_session(payload: dict = Body(...)) -> dict:
    try:
        user = access.get(payload.get("user_key", ""))
    except Denied as e:
        raise HTTPException(403, e.message) from None
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = {"user": user, "chat_id": None, "subject": None}
    acct = records.my_account(user)
    return {"session_id": sid, "user_id": user.user_id, "name": user.name,
            "role": user.role, "account_id": user.account_id,
            "account_name": acct["account_name"] if acct else None,
            "snapshot": f"{store.snapshot():%Y-%m-%d %H:%M} IST"}


# --------------------------------------------------------------------------
# Ask
# --------------------------------------------------------------------------

@app.post("/api/ask")
def ask(payload: dict = Body(...)) -> StreamingResponse:
    """One turn, streamed as server-sent events.

    Streaming exists so the interface can show WHICH TOOL is running while it
    runs. A spinner that says nothing is indistinguishable from a hang, and the
    tool trace is the part that makes the answer auditable.
    """
    user, sess = _session(payload.get("session_id", ""))
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "Empty question.")

    subject = payload.get("subject") or sess.get("subject")
    if sess.get("chat_id") is None:
        sess["chat_id"] = store.start_conversation(
            user, subject_ref=(subject or {}).get("ref") if subject else None)
        sess["subject"] = subject

    history = sess.setdefault("history", [])

    def gen():
        answer, used, proposals = "", [], []
        try:
            for ev in agent.run(user, question, history, sess.get("subject")):
                if ev["type"] == "answer":
                    answer = ev["text"]
                elif ev["type"] == "done":
                    used = ev.get("tools", used)
                elif ev["type"] == "proposals":
                    proposals = ev["items"]

                # A customer sees the friendly label and never the raw result;
                # the trace is an agent's instrument.
                if user.is_customer and ev["type"] in ("tool_start", "tool_end"):
                    ev = {"type": ev["type"], "call_id": ev.get("call_id"),
                          "label": ev.get("friendly", "Working")}
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as e:                                    # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:300]})}\n\n"
        finally:
            # Whatever the model said, check that every figure in it came from a
            # tool result. This should never fire -- `calculate` hands the model
            # every number -- but a wrong figure in a customer reply is the
            # failure the whole design is arranged to prevent.
            unverified = verify.check(answer, used, question)
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer})
            record = {"answer": answer, "tools": used, "proposals": proposals,
                      "unverified": unverified,
                      "decisions": [u["result"]["decision"] for u in used
                                    if u["tool"] == "calculate"
                                    and "decision" in u.get("result", {})]}
            store.add_turn(sess["chat_id"], question, answer, record)
            tail = {"type": "verified", "unverified": unverified,
                    "chat_id": sess["chat_id"]}
            yield f"data: {json.dumps(tail)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# The confirmation gate — the only path to a state change, and no model can
# reach it. It is called by a human pressing Confirm.
# --------------------------------------------------------------------------

@app.get("/api/proposals/{proposal_id}")
def get_proposal(proposal_id: str, session_id: str) -> dict:
    _session(session_id)
    p = actions.get(proposal_id)
    if p is None:
        raise HTTPException(404, "No such proposal.")
    return p.to_dict()


@app.post("/api/proposals/{proposal_id}/confirm")
def confirm_proposal(proposal_id: str, payload: dict = Body(...)) -> dict:
    user, sess = _session(payload.get("session_id", ""))
    try:
        p = actions.commit(proposal_id, user)
    except actions.Refused as e:
        raise HTTPException(403, str(e)) from None
    sess.setdefault("history", []).append({
        "role": "user",
        "content": (f"[SYSTEM] The human confirmed {p.proposal_id}; it is now "
                    f"{p.reference}. Acknowledge briefly and state the reference.")})
    return {"status": "committed", "reference": p.reference,
            "proposal": p.to_dict()}


@app.post("/api/proposals/{proposal_id}/decline")
def decline_proposal(proposal_id: str, payload: dict = Body(...)) -> dict:
    user, sess = _session(payload.get("session_id", ""))
    try:
        p = actions.decline(proposal_id, user)
    except actions.Refused as e:
        raise HTTPException(404, str(e)) from None
    sess.setdefault("history", []).append({
        "role": "user",
        "content": (f"[SYSTEM] The human declined {p.proposal_id}. Nothing was "
                    f"executed. Acknowledge briefly and offer an alternative.")})
    return {"status": "declined", "proposal": p.to_dict()}


@app.get("/api/actions")
def executed_actions(session_id: str) -> dict:
    user, _ = _session(session_id)
    _internal(user)
    return {"actions": actions.executed()}


@app.post("/api/new-chat")
def new_chat(payload: dict = Body(...)) -> dict:
    _, sess = _session(payload.get("session_id", ""))
    sess["chat_id"] = None
    sess["subject"] = payload.get("subject")
    sess["history"] = []
    return {"ok": True, "subject": sess["subject"]}


# --------------------------------------------------------------------------
# What the customer needs help with
# --------------------------------------------------------------------------

@app.get("/api/my-issues")
def my_issues(session_id: str) -> dict:
    user, _ = _session(session_id)
    out_t, out_o = [], []
    for t in records.tickets(user):
        sev, _why = engine.triage(t["subject"] or "", t["description"] or "")
        out_t.append({"kind": "ticket", "ref": t["ticket_id"], "label": t["subject"],
                      "status": t["status"], "severity": sev,
                      "open": (t["status"] or "").lower() == "open",
                      "created_at": t["created_at"]})
    for o in records.orders(user):
        out_o.append({"kind": "order", "ref": o["order_id"],
                      "label": f"{o['carrier']} · {o['status']}",
                      "status": o["status"],
                      "fee_inr": o["shipment_fee_inr"],
                      "open": o["status"] in ("BOOKED", "DRAFT"),
                      "created_at": o["booked_at"]})
    out_t.sort(key=lambda x: (not x["open"], x["created_at"] or ""), reverse=True)
    out_o.sort(key=lambda x: (not x["open"], x["created_at"] or ""), reverse=True)
    return {"tickets": out_t, "orders": out_o}


# --------------------------------------------------------------------------
# Employee dashboard: what is breaching, right now
# --------------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard(session_id: str) -> dict:
    user, _ = _session(session_id)
    _internal(user)

    breached, at_risk, ok = [], [], []
    for t in records.tickets(user, only_open=True):
        d = engine.sla(t["ticket_id"])
        acct = store.one("SELECT account_name FROM accounts WHERE account_id=?",
                         (t["account_id"],))
        row = {**d.facts, "account_name": acct["account_name"] if acct else None,
               "headline": d.headline, "authority": d.authority_used,
               "rule_chain": [s.to_dict() for s in d.rule_chain],
               "caveats": d.caveats}
        (breached if row["breached"] else ok).append(row)

    # Orders that are accruing a credit right now, with the amount already
    # computed -- an agent should not have to ask the assistant for a number
    # the engine can produce for every order at once.
    credits = []
    for o in records.orders(user):
        d = engine.service_credit(o["order_id"])
        if d.outcome in (engine.Outcome.ELIGIBLE, engine.Outcome.INSUFFICIENT_DATA):
            acct = store.one("SELECT account_name FROM accounts WHERE account_id=?",
                             (o["account_id"],))
            credits.append({"order_id": o["order_id"],
                            "account_name": acct["account_name"] if acct else None,
                            "outcome": d.outcome.value, "amount_inr": d.amount_inr,
                            "headline": d.headline, "authority": d.authority_used,
                            "needs_manager": d.needs_manager,
                            "caveats": d.caveats,
                            "rule_chain": [s.to_dict() for s in d.rule_chain]})

    breached.sort(key=lambda r: -r["overdue_minutes"])
    return {"breached": breached, "within_target": ok, "credits": credits,
            "stats": {"open_tickets": len(breached) + len(ok),
                      "breached": len(breached),
                      "credits_due": sum(1 for c in credits
                                         if c["outcome"] == "ELIGIBLE"),
                      "accounts": len(records.accounts(user))},
            "snapshot": f"{store.snapshot():%Y-%m-%d %H:%M} IST"}


# --------------------------------------------------------------------------
# Conversation history and review
# --------------------------------------------------------------------------

@app.get("/api/chats")
def chats(session_id: str, scope: str = "mine") -> dict:
    user, _ = _session(session_id)
    if user.is_customer or scope == "mine":
        rows = store.list_conversations(user_id=user.user_id)
    else:
        _internal(user)
        rows = store.list_conversations(role="customer" if scope == "customers" else None)
    return {"chats": rows, "scope": scope}


@app.get("/api/chats/{chat_id}")
def chat_detail(chat_id: str, session_id: str) -> dict:
    user, _ = _session(session_id)
    c = store.conversation(chat_id)
    if c is None:
        raise HTTPException(404, "No such conversation.")
    if user.is_customer and c["user_id"] != user.user_id:
        raise HTTPException(403, "That conversation is not yours.")
    turns = store.conversation_turns(chat_id)
    if user.is_customer:
        # The transcript, without the machinery behind it.
        turns = [{"seq": t["seq"], "question": t["question"],
                  "answer": t["answer"], "created_at": t["created_at"]}
                 for t in turns]
    return {"chat": c, "turns": turns}


@app.post("/api/chats/{chat_id}/resume")
def resume(chat_id: str, payload: dict = Body(...)) -> dict:
    user, sess = _session(payload.get("session_id", ""))
    c = store.conversation(chat_id)
    if c is None:
        raise HTTPException(404, "No such conversation.")
    if user.is_customer and c["user_id"] != user.user_id:
        raise HTTPException(403, "That conversation is not yours.")
    sess["chat_id"] = chat_id
    sess["subject"] = ({"ref": c["subject_ref"]} if c["subject_ref"] else None)
    turns = store.conversation_turns(chat_id)
    return {"ok": True, "chat": c,
            "turns": [{"question": t["question"], "answer": t["answer"]}
                      for t in turns]}


# --------------------------------------------------------------------------
# Policy search — the vector store, exposed so it can be inspected
# --------------------------------------------------------------------------

@app.get("/api/policy-search")
def policy_search(session_id: str, q: str) -> dict:
    user, _ = _session(session_id)
    passages, excluded = knowledge.index().search(
        q, account_id=user.scope(), limit=8)
    return {"query": q,
            "passages": [p.to_dict() for p in passages],
            "excluded": excluded if user.is_internal else []}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "snapshot": f"{store.snapshot():%Y-%m-%d %H:%M} IST",
            "records": {t: store.one(f"SELECT COUNT(*) n FROM {t}")["n"]
                        for t in ("accounts", "orders", "tickets")},
            "policy_chunks": len(knowledge.index().chunks),
            "model": config.GROQ_MODEL}


STATIC = config.ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC / "index.html")
