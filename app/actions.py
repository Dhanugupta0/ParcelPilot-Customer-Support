"""State-changing actions, behind a human confirmation gate.

The model has no tool that can execute anything. It can only PREPARE a
proposal; committing it is a separate HTTP call made by a person pressing a
button. That separation is the whole control: it is not a prompt asking the
model to be careful, it is the absence of a capability.

Actions are mocked — they append to a local ledger rather than calling a real
ticketing system — but the gate around them is real.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import config, engine
from app.access import User

LEDGER = config.VAR_DIR / "executed_actions.jsonl"
_LOCK = threading.RLock()
_PENDING: dict[str, "Proposal"] = {}

TTL_MINUTES = 30

LABEL = {
    "create_escalation": "Create an escalation",
    "update_ticket": "Update the ticket",
    "create_followup_task": "Create a follow-up task",
    "issue_service_credit": "Issue a service credit",
}


class Refused(Exception):
    pass


@dataclass
class Proposal:
    proposal_id: str
    action_type: str
    user_id: str
    summary: str
    preview: dict
    reason: str
    account_id: str | None = None
    requires_manager: bool = False
    status: str = "pending"                # pending | committed | declined
    reference: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def expired(self) -> bool:
        made = datetime.fromisoformat(self.created_at)
        return datetime.now(timezone.utc) - made > timedelta(minutes=TTL_MINUTES)

    def to_dict(self) -> dict:
        return {"proposal_id": self.proposal_id, "action_type": self.action_type,
                "label": LABEL.get(self.action_type, self.action_type),
                "summary": self.summary, "preview": self.preview,
                "reason": self.reason, "requires_manager": self.requires_manager,
                "status": self.status, "reference": self.reference,
                "created_at": self.created_at}


def propose(user: User, action_type: str, reason: str, *,
            ticket_id: str | None = None, order_id: str | None = None,
            severity: str | None = None, details: str | None = None,
            amount_inr: float | None = None) -> Proposal:
    if action_type not in LABEL:
        raise Refused(f"Unknown action type {action_type!r}.")
    if user.is_customer and action_type in ("update_ticket", "issue_service_credit"):
        # A customer may ask for an escalation; they may not move ParcelPilot's
        # own records or grant themselves money.
        raise Refused("A customer session cannot prepare that action. I can "
                      "escalate this to a support agent instead.")

    from app import records
    account_id = user.account_id
    subject = ""
    if order_id:
        o = records.order(user, order_id)
        if o is None:
            raise Refused(f"{order_id} is not visible in this session.")
        account_id, subject = o["account_id"], f"{order_id} ({o['status']})"
    elif ticket_id:
        t = records.ticket(user, ticket_id)
        if t is None:
            raise Refused(f"{ticket_id} is not visible in this session.")
        account_id, subject = t["account_id"], f"{ticket_id}: {t['subject']}"

    requires_manager = False
    if action_type == "issue_service_credit":
        if amount_inr is None:
            raise Refused("A service credit needs an amount, and it must come "
                          "from the policy engine rather than an estimate.")
        # SOP v4 §3. Checked here rather than trusted from the model, because
        # this is the number that decides whether a person may sign it off.
        requires_manager = amount_inr > engine.MANAGER_APPROVAL_ABOVE_INR

    preview = {k: v for k, v in {
        "action": action_type, "ticket_id": ticket_id, "order_id": order_id,
        "account_id": account_id, "subject": subject, "severity": severity,
        "details": details, "amount_inr": amount_inr, "reason": reason,
        "prepared_by": user.name,
    }.items() if v is not None}

    p = Proposal(proposal_id=f"PROP-{uuid.uuid4().hex[:8].upper()}",
                 action_type=action_type, user_id=user.user_id,
                 summary=f"{LABEL[action_type]} — {subject or account_id or 'general'}",
                 preview=preview, reason=reason, account_id=account_id,
                 requires_manager=requires_manager)
    with _LOCK:
        _PENDING[p.proposal_id] = p
    return p


def get(proposal_id: str) -> Proposal | None:
    return _PENDING.get(proposal_id)


def commit(proposal_id: str, user: User) -> Proposal:
    """The only path to a state change, and no model can reach it."""
    p = _PENDING.get(proposal_id)
    if p is None:
        raise Refused("That proposal no longer exists. Ask again and confirm the "
                      "fresh one.")
    if p.status != "pending":
        raise Refused(f"That proposal was already {p.status}.")
    if p.expired:
        p.status = "declined"
        raise Refused(f"That proposal expired after {TTL_MINUTES} minutes. "
                      f"Ask again so the figures are recomputed.")
    if p.requires_manager and not user.may_approve:
        raise Refused("This amount is above the SOP approval threshold, so a "
                      "support manager has to confirm it.")
    if user.is_customer and p.user_id != user.user_id:
        raise Refused("That proposal was not prepared in this session.")

    p.status = "committed"
    p.reference = f"{p.action_type.split('_')[-1][:3].upper()}-{uuid.uuid4().hex[:6].upper()}"
    with _LOCK:
        with LEDGER.open("a") as fh:
            fh.write(json.dumps({**p.to_dict(), "committed_by": user.user_id,
                                 "committed_at": datetime.now(timezone.utc)
                                 .isoformat(timespec="seconds")}) + "\n")
    return p


def decline(proposal_id: str, user: User) -> Proposal:
    p = _PENDING.get(proposal_id)
    if p is None:
        raise Refused("That proposal no longer exists.")
    p.status = "declined"
    return p


def executed() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[::-1]
