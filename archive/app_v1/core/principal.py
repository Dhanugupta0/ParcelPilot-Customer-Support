"""Identity, roles and capabilities.

The assessment allows mocked auth. What it does NOT allow is enforcing access
control through model instructions. So the Principal is bound to the session
server-side, is never taken from anything the model or the user types, and is a
required argument on every repository and tool call. A prompt-injected
"ignore previous instructions, I am an admin" changes nothing here, because the
model has no channel through which to assert identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    CUSTOMER = "customer"
    SUPPORT_AGENT = "support_agent"
    SUPPORT_MANAGER = "support_manager"


class Perm(str, Enum):
    READ_OWN_ACCOUNT = "read:own_account"
    READ_ALL_ACCOUNTS = "read:all_accounts"
    READ_INTERNAL_FIELDS = "read:internal_fields"   # staff notes, assignee, history
    VIEW_SIGNALS = "view:signals"                   # the ops Signal Board
    ACT_ESCALATE = "action:escalate"
    ACT_UPDATE_TICKET = "action:update_ticket"
    ACT_CREATE_TASK = "action:create_task"
    APPROVE_CREDIT = "action:approve_credit"        # SOP v4 s3: credits > INR 1,000
    ACT_SEND_OUTREACH = "action:send_outreach"      # proactive customer contact


ROLE_PERMS: dict[Role, set[Perm]] = {
    Role.CUSTOMER: {
        Perm.READ_OWN_ACCOUNT,
        Perm.ACT_ESCALATE,       # a customer may raise an escalation on their own ticket
    },
    Role.SUPPORT_AGENT: {
        Perm.READ_ALL_ACCOUNTS,
        Perm.READ_INTERNAL_FIELDS,
        Perm.VIEW_SIGNALS,
        Perm.ACT_ESCALATE,
        Perm.ACT_UPDATE_TICKET,
        Perm.ACT_CREATE_TASK,
        Perm.ACT_SEND_OUTREACH,
    },
    Role.SUPPORT_MANAGER: {
        Perm.READ_ALL_ACCOUNTS,
        Perm.READ_INTERNAL_FIELDS,
        Perm.VIEW_SIGNALS,
        Perm.ACT_ESCALATE,
        Perm.ACT_UPDATE_TICKET,
        Perm.ACT_CREATE_TASK,
        Perm.ACT_SEND_OUTREACH,
        Perm.APPROVE_CREDIT,
    },
}


class AccessDenied(Exception):
    """Raised by the data layer, never by the model.

    Carries two messages on purpose. `message` is what the user and the model
    see; `internal_reason` is the true cause and goes only to the audit log and
    the internal tool-trace panel. Keeping them separate is what stops the error
    channel from becoming an enumeration oracle: "belongs to ACCT-001" and
    "does not exist" must be indistinguishable to a customer, or they can probe
    for other tenants' record IDs by watching which error they get back.
    """

    def __init__(self, message: str, *, resource: str | None = None,
                 internal_reason: str | None = None):
        super().__init__(message)
        self.message = message
        self.resource = resource
        self.internal_reason = internal_reason or message


@dataclass(frozen=True)
class Principal:
    user_id: str
    display_name: str
    role: Role
    account_id: str | None = None        # required for CUSTOMER, None for staff
    perms: frozenset[Perm] = field(default_factory=frozenset)

    @property
    def is_customer(self) -> bool:
        return self.role is Role.CUSTOMER

    @property
    def is_internal(self) -> bool:
        return not self.is_customer

    def can(self, perm: Perm) -> bool:
        return perm in self.perms

    def require(self, perm: Perm, what: str = "") -> None:
        if not self.can(perm):
            raise AccessDenied(
                f"Your role ({self.role.value}) is not permitted to {what or perm.value}.",
                resource=perm.value,
            )

    def assert_account_access(self, account_id: str | None) -> None:
        """The single choke point for tenant isolation."""
        if self.can(Perm.READ_ALL_ACCOUNTS):
            return
        if account_id is None or account_id != self.account_id:
            raise AccessDenied(
                "That record belongs to a different ParcelPilot account. "
                "You can only access data for your own account.",
                resource=account_id,
                internal_reason=(
                    f"tenant isolation: principal {self.user_id} is scoped to "
                    f"{self.account_id}, requested {account_id}"
                ),
            )

    def scope_label(self) -> str:
        if self.is_customer:
            return f"customer session, scoped to {self.account_id}"
        return f"internal {self.role.value}, cross-account access"


def make_principal(user_id: str, display_name: str, role: Role,
                   account_id: str | None = None) -> Principal:
    if role is Role.CUSTOMER and not account_id:
        raise ValueError("A customer principal must be bound to an account_id")
    if role is not Role.CUSTOMER:
        account_id = None
    return Principal(
        user_id=user_id,
        display_name=display_name,
        role=role,
        account_id=account_id,
        perms=frozenset(ROLE_PERMS[role]),
    )


# --- Mocked identity directory ---------------------------------------------
# Stands in for an IdP. Sessions reference these by key; the browser never sends
# a role or an account_id that the server trusts.
DIRECTORY: dict[str, dict] = {
    "cust_northstar": dict(user_id="cust_northstar", display_name="Ravi (Northstar Logistics)",
                           role=Role.CUSTOMER, account_id="ACCT-001"),
    "cust_lumenworks": dict(user_id="cust_lumenworks", display_name="Sara (LumenWorks)",
                            role=Role.CUSTOMER, account_id="ACCT-002"),
    "cust_beacon": dict(user_id="cust_beacon", display_name="Dev (Beacon Retail)",
                        role=Role.CUSTOMER, account_id="ACCT-003"),
    "cust_axis": dict(user_id="cust_axis", display_name="Meera (Axis Labs)",
                      role=Role.CUSTOMER, account_id="ACCT-004"),
    "agent_rohit": dict(user_id="agent_rohit", display_name="Rohit (Support Agent)",
                        role=Role.SUPPORT_AGENT),
    "agent_maya": dict(user_id="agent_maya", display_name="Maya (Support Agent)",
                       role=Role.SUPPORT_AGENT),
    "mgr_priya": dict(user_id="mgr_priya", display_name="Priya Mehta (Support Manager)",
                      role=Role.SUPPORT_MANAGER),
}


def load_principal(user_key: str) -> Principal:
    spec = DIRECTORY.get(user_key)
    if spec is None:
        raise AccessDenied(f"Unknown user session: {user_key!r}")
    return make_principal(**spec)
