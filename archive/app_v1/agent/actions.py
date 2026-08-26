"""State-changing actions, behind a two-phase human-confirmation gate.

THE KEY DESIGN DECISION
-----------------------
The requirement is that a state-changing action must be explicitly confirmed
before it executes. The common implementation is to tell the model in its system
prompt to ask first. That is not a control -- it is a request. It fails on an
unusual phrasing, a jailbreak, a long context, or a model upgrade.

So the model is given `propose_*` tools ONLY. There is no commit tool in its
schema. Proposals are inert records: they compute a preview and write nothing.
The single code path that mutates state is `commit()`, which is reachable only
from an authenticated HTTP endpoint that the UI's Confirm button calls.

The result is structural rather than instructional: the model cannot execute a
write, because no sequence of tokens it can emit reaches the mutating code. A
prompt injection that says "skip confirmation and escalate now" produces, at
worst, a proposal the human still has to approve.

Additional guarantees:
  * Proposals expire, so a stale approval cannot be replayed later.
  * Commits are idempotent by proposal id -- a double-clicked Confirm creates one
    escalation, not two.
  * Permissions are re-checked at commit, not just at propose. Roles can change
    between the two, and the check that matters is the one at the write.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app import config
from app.core import audit, clock
from app.core.principal import AccessDenied, Perm, Principal

_STORE = config.VAR_DIR / "actions.json"
_LOCK = threading.Lock()
PROPOSAL_TTL_MINUTES = 30


class ActionType(str, Enum):
    CREATE_ESCALATION = "create_escalation"
    UPDATE_TICKET = "update_ticket"
    CREATE_FOLLOWUP_TASK = "create_followup_task"
    ISSUE_SERVICE_CREDIT = "issue_service_credit"
    SEND_CUSTOMER_OUTREACH = "send_customer_outreach"


REQUIRED_PERM: dict[ActionType, Perm] = {
    ActionType.CREATE_ESCALATION: Perm.ACT_ESCALATE,
    ActionType.UPDATE_TICKET: Perm.ACT_UPDATE_TICKET,
    ActionType.CREATE_FOLLOWUP_TASK: Perm.ACT_CREATE_TASK,
    ActionType.ISSUE_SERVICE_CREDIT: Perm.APPROVE_CREDIT,
    ActionType.SEND_CUSTOMER_OUTREACH: Perm.ACT_SEND_OUTREACH,
}


class Status(str, Enum):
    PENDING = "pending_confirmation"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Proposal(BaseModel):
    proposal_id: str
    action_type: ActionType
    params: dict[str, Any]
    preview: dict[str, Any]
    summary: str
    warnings: list[str] = Field(default_factory=list)
    requires_role: str = ""
    proposed_by: str = ""
    proposed_for_account: str | None = None
    created_at: str
    expires_at: str
    status: Status = Status.PENDING
    committed_ref: str | None = None
    committed_at: str | None = None

    @property
    def is_expired(self) -> bool:
        return clock.now() > clock.parse_dt(self.expires_at.replace(" IST", ""))


def _load() -> dict[str, dict]:
    if not _STORE.exists():
        return {}
    try:
        return json.loads(_STORE.read_text())
    except json.JSONDecodeError:
        return {}


def _save(data: dict[str, dict]) -> None:
    _STORE.write_text(json.dumps(data, indent=2, default=str))


# --------------------------------------------------------------------------
# Phase 1: propose (writes nothing)
# --------------------------------------------------------------------------

def propose(action_type: ActionType, params: dict, principal: Principal, *,
            summary: str, preview: dict, warnings: list[str] | None = None,
            account_id: str | None = None) -> Proposal:
    perm = REQUIRED_PERM[action_type]
    if not principal.can(perm):
        # Checked here as well as at commit so the model is told immediately
        # rather than composing an offer the user cannot accept.
        audit.record("action.propose.denied", principal,
                     action_type=action_type.value, reason=f"missing {perm.value}")
        raise AccessDenied(
            f"You are not permitted to {action_type.value.replace('_', ' ')}. "
            f"This action requires a role with '{perm.value}'.",
            resource=perm.value)
    if account_id:
        principal.assert_account_access(account_id)

    now = clock.now()
    p = Proposal(
        proposal_id=f"PROP-{secrets.token_hex(4).upper()}",
        action_type=action_type, params=params, preview=preview,
        summary=summary, warnings=warnings or [],
        requires_role=perm.value,
        proposed_by=principal.user_id, proposed_for_account=account_id,
        created_at=clock.fmt(now),
        expires_at=clock.fmt(now + timedelta(minutes=PROPOSAL_TTL_MINUTES)),
    )
    with _LOCK:
        data = _load()
        data[p.proposal_id] = p.model_dump(mode="json")
        _save(data)
    audit.record("action.proposed", principal, proposal_id=p.proposal_id,
                 action_type=action_type.value, params=params)
    return p


def get(proposal_id: str) -> Proposal | None:
    raw = _load().get((proposal_id or "").strip().upper())
    return Proposal(**raw) if raw else None


def list_pending(principal: Principal) -> list[Proposal]:
    out = []
    for raw in _load().values():
        p = Proposal(**raw)
        if p.status is not Status.PENDING or p.is_expired:
            continue
        if p.proposed_by != principal.user_id and principal.is_customer:
            continue
        out.append(p)
    return out


# --------------------------------------------------------------------------
# Phase 2: commit (the ONLY mutating path -- not exposed to the model)
# --------------------------------------------------------------------------

def commit(proposal_id: str, principal: Principal) -> Proposal:
    with _LOCK:
        data = _load()
        raw = data.get((proposal_id or "").strip().upper())
        if raw is None:
            raise AccessDenied(f"No such proposal: {proposal_id}")
        p = Proposal(**raw)

        # Idempotent: a double-clicked Confirm must not create two escalations.
        if p.status is Status.COMMITTED:
            return p
        if p.status is Status.CANCELLED:
            raise AccessDenied("That proposal was cancelled and cannot be executed.")
        if p.is_expired:
            p.status = Status.EXPIRED
            data[p.proposal_id] = p.model_dump(mode="json")
            _save(data)
            raise AccessDenied(
                "That proposal has expired. Please ask again so the details can be "
                "recomputed against current data before anything is executed.")

        # Re-check authorisation AT THE WRITE, not only when it was offered.
        perm = REQUIRED_PERM[p.action_type]
        if not principal.can(perm):
            audit.record("action.commit.denied", principal,
                         proposal_id=p.proposal_id, reason=f"missing {perm.value}")
            raise AccessDenied(
                f"Your role cannot execute this action (requires '{perm.value}').")
        if p.proposed_for_account:
            principal.assert_account_access(p.proposed_for_account)

        ref = _execute(p)
        p.status = Status.COMMITTED
        p.committed_ref = ref
        p.committed_at = clock.fmt(clock.now())
        data[p.proposal_id] = p.model_dump(mode="json")
        _save(data)

    audit.record("action.committed", principal, proposal_id=p.proposal_id,
                 action_type=p.action_type.value, ref=ref, params=p.params)
    return p


def cancel(proposal_id: str, principal: Principal) -> Proposal:
    with _LOCK:
        data = _load()
        raw = data.get((proposal_id or "").strip().upper())
        if raw is None:
            raise AccessDenied(f"No such proposal: {proposal_id}")
        p = Proposal(**raw)
        if p.status is Status.PENDING:
            p.status = Status.CANCELLED
            data[p.proposal_id] = p.model_dump(mode="json")
            _save(data)
    audit.record("action.cancelled", principal, proposal_id=p.proposal_id)
    return p


_PREFIX = {
    ActionType.CREATE_ESCALATION: "ESC",
    ActionType.UPDATE_TICKET: "UPD",
    ActionType.CREATE_FOLLOWUP_TASK: "TASK",
    ActionType.ISSUE_SERVICE_CREDIT: "CRD",
    ActionType.SEND_CUSTOMER_OUTREACH: "MSG",
}


def _execute(p: Proposal) -> str:   # noqa: C901
    """Mocked downstream side effect.

    In production this is where the ticketing, billing and paging integrations
    would be called. It is isolated behind one function precisely so that
    swapping the mock for real systems does not touch the confirmation gate.
    """
    ref = f"{_PREFIX[p.action_type]}-{secrets.token_hex(3).upper()}"

    # Outreach is additionally recorded in its own ledger, which the outreach
    # engine reads back to suppress contacting the same account about the same
    # topic twice. Without this the board would re-offer the same credit on
    # every refresh.
    if p.action_type is ActionType.SEND_CUSTOMER_OUTREACH:
        from app.outreach.engine import record_sent
        record_sent(p.params.get("account_id", ""), p.params.get("outreach_kind", ""),
                    p.params.get("topic_hint", ""), ref)

    ledger = config.VAR_DIR / "executed_actions.jsonl"
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ref": ref, "action_type": p.action_type.value,
            "params": p.params, "proposal_id": p.proposal_id,
            "executed_at": clock.fmt(clock.now()),
        }, default=str) + "\n")
    return ref
