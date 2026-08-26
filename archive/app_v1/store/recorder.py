"""Persist a turn as it streams.

Wraps the agent's event stream and writes it to the store on the way past, so
the browser still gets tokens the moment they arrive and nothing is buffered
for the sake of the database.

What gets recorded is chosen so a support agent can VERIFY an answer later
rather than merely re-read it: every tool call with its arguments and its raw
result, the citations, the derived confidence, and whether the answer was
withheld. An audit that only stores the prose can tell you what was said and
never whether it was justified.
"""
from __future__ import annotations

from typing import Iterator

from app.store import db, vectors


def _title_from(message: str) -> str:
    t = " ".join(message.split())
    return t[:90] + ("…" if len(t) > 90 else "")


def record_turn(cid: str, principal, user_message: str,
                events: Iterator[dict]) -> Iterator[dict]:
    """Pass events through untouched, writing them to the store as they go."""
    if not cid:
        yield from events
        return

    turn = db.next_seq(cid)
    db.add_message(cid, turn, "user", user_message)
    db.touch_conversation(cid, title=_title_from(user_message), bump_turn=True)

    answer: str | None = None
    trust: dict | None = None
    escalated = False
    # Only `tool_start` carries the arguments; `tool_end` carries the result.
    # Recording the result without the arguments would tell a reviewer WHAT came
    # back but not what was asked for -- which is half the audit and the half
    # that matters when the question is "did it look up the right order?".
    pending_args: dict[str, dict] = {}

    try:
        for ev in events:
            kind = ev.get("type")

            if kind == "tool_start":
                pending_args[ev.get("call_id", "")] = ev.get("args") or {}

            elif kind == "tool_end":
                db.add_tool_call(cid, turn, ev.get("tool", ""), ev.get("category"),
                                 pending_args.pop(ev.get("call_id", ""), None),
                                 ev.get("raw"), ev.get("result"))

            elif kind == "answer":
                answer = ev.get("text", "")

            elif kind == "trust":
                # Arrives immediately after `answer`; hold both and write once,
                # so an assistant row never exists without its assessment.
                trust = {k: ev.get(k) for k in
                         ("confidence", "band", "reasons", "citations",
                          "conflicts", "withheld")}

            elif kind == "proposals":
                for p in ev.get("items", []):
                    if _record_proposal(cid, principal, p):
                        escalated = True

            yield ev
    finally:
        if answer is not None:
            db.add_message(cid, turn + 1, "assistant", answer, trust)
        if escalated:
            db.touch_conversation(cid, escalated=True)
        # Re-embed on every turn: a thread's subject drifts as it goes on, and
        # an index built only from the opening question ages badly.
        try:
            vectors.index_conversation(cid)
        except Exception as e:                                    # noqa: BLE001
            print(f"[recorder] could not index {cid}: {e}")


def _record_proposal(cid: str, principal, proposal: dict) -> bool:
    """Record an escalation proposal. Returns True if it WAS an escalation."""
    params = proposal.get("params") or proposal.get("preview") or {}
    action = (proposal.get("action_type") or params.get("action")
              or params.get("action_type") or "")
    if action != "create_escalation":
        return False
    db.record_escalation(
        proposal_id=proposal.get("proposal_id", ""),
        conversation_id=cid,
        account_id=params.get("account_id") or principal.account_id,
        account_name=params.get("account_name"),
        ticket_id=params.get("ticket_id"),
        severity=params.get("severity"),
        reason=params.get("reason") or proposal.get("reason"),
        details=params.get("details") or params.get("subject"),
        raised_by=principal.user_id,
        raised_by_role=principal.role.value,
        status="proposed")
    return True
