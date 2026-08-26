"""Domain models for the supplied workbook, plus field-level redaction.

Two ideas worth calling out:

* `Ticket.historical_resolution` is modelled as an explicitly UNRELIABLE field.
  The workbook README warns that past resolutions may be wrong, and the pack
  contains two that demonstrably are (TKT-450 contradicts the Northstar
  agreement; TKT-451 contradicts the Product Operations Guide). Anything that
  reads this field gets it wrapped in a reliability warning, and customers
  never see it at all.

* Redaction happens on the way out of the repository, keyed off the Principal's
  role. It is not a prompt instruction the model could be argued out of.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.core import clock
from app.core.principal import Perm, Principal


class Plan(str, Enum):
    ENTERPRISE = "Enterprise"
    GROWTH = "Growth"
    STANDARD = "Standard"


class OrderStatus(str, Enum):
    DRAFT = "DRAFT"
    BOOKED = "BOOKED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class Severity(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


REDACTED = "[redacted - not available in a customer session]"


class Account(BaseModel):
    account_id: str
    account_name: str
    plan: Plan
    status: str
    csm: str | None = None
    contract_file: str | None = None
    premium_support: bool = False
    notes: str | None = None

    @property
    def has_agreement(self) -> bool:
        return bool(self.contract_file)

    def view(self, p: Principal) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        if not p.can(Perm.READ_INTERNAL_FIELDS):
            d["notes"] = REDACTED
        return d


class Order(BaseModel):
    order_id: str
    account_id: str
    carrier: str
    status: OrderStatus
    booked_at: datetime | None = None
    pickup_window_start: datetime | None = None
    pickup_window_end: datetime | None = None
    pickup_actual_at: datetime | None = None
    shipment_fee_inr: float = 0.0
    carrier_fault: bool = False
    customer_fault: bool = False
    cancellation_requested_at: datetime | None = None
    notes: str | None = None

    # --- derived facts the policy engine relies on -------------------------
    @property
    def minutes_since_booking_at_cancel_request(self) -> float | None:
        if not self.booked_at or not self.cancellation_requested_at:
            return None
        return (self.cancellation_requested_at - self.booked_at).total_seconds() / 60.0

    def pickup_delay_minutes(self, ref: datetime | None = None) -> float | None:
        """Minutes past the END of the scheduled pickup window.

        If the pickup already happened, measured to the actual pickup. If it
        still has not happened, measured to the dataset snapshot -- an open
        failure keeps accruing delay, which is what ORD-2002 exercises.
        """
        if not self.pickup_window_end:
            return None
        end = self.pickup_actual_at or (ref or clock.now())
        return max(0.0, (end - self.pickup_window_end).total_seconds() / 60.0)

    @property
    def pickup_occurred(self) -> bool:
        return self.pickup_actual_at is not None or self.status in {
            OrderStatus.PICKED_UP, OrderStatus.DELIVERED
        }

    def view(self, p: Principal) -> dict[str, Any]:
        p.assert_account_access(self.account_id)
        d = self.model_dump(mode="json")
        d["_derived"] = {
            "minutes_since_booking_at_cancel_request":
                self.minutes_since_booking_at_cancel_request,
            "pickup_delay_minutes_vs_window_end": self.pickup_delay_minutes(),
            "pickup_occurred": self.pickup_occurred,
        }
        return d


class Ticket(BaseModel):
    ticket_id: str
    account_id: str
    created_at: datetime | None = None
    status: str = "open"
    subject: str = ""
    description: str = ""
    channel: str = ""
    assigned_to: str | None = None
    last_customer_message_at: datetime | None = None
    historical_resolution: str | None = None
    # Set for records we generate to give the Signal Board a realistic history.
    synthetic: bool = False

    @property
    def is_open(self) -> bool:
        return str(self.status).lower() in {"open", "pending", "in_progress"}

    def view(self, p: Principal) -> dict[str, Any]:
        p.assert_account_access(self.account_id)
        d = self.model_dump(mode="json")
        if not p.can(Perm.READ_INTERNAL_FIELDS):
            d["assigned_to"] = REDACTED
            d["historical_resolution"] = REDACTED
        elif self.historical_resolution:
            # Internal users DO see it, but never without the health warning.
            d["historical_resolution"] = {
                "text": self.historical_resolution,
                "reliability": "UNRELIABLE",
                "warning": (
                    "Historical ticket resolutions are context only. The dataset "
                    "README states some are incorrect. Never cite this as policy "
                    "authority; verify against the current SOP, policy, or the "
                    "customer's signed agreement."
                ),
            }
        return d
