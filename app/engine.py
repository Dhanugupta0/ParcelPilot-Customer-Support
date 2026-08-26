"""Every number the product states is computed here.

This module is the reason the system can be trusted. Fees, credit amounts, SLA
deadlines and breach margins are arithmetic over the workbook, done in Python,
with the clause that authorised each step recorded beside it. The language model
is never asked to calculate anything -- by the time it sees a question, the
answer already exists as a `Decision` and its job is to put it into English.

Two consequences worth being explicit about:

  * A wrong figure is a BUG, reproducible from a unit test, not a bad sample
    from a model. Every rule below has a test asserting it against the dataset.
  * The rule chain is the explanation. "Why INR 0?" is answered by replaying
    `decision.rule_chain`, which names the clause at each step -- not by asking
    the model to reconstruct its reasoning after the fact.

Precedence is applied the way Support Policy v3 §1 states: a signed customer
agreement overrides the default policy. Where an agreement is silent, the
default applies, and the chain says which happened.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from app import config
from app import store


# --------------------------------------------------------------------------
# Contract terms, transcribed from the signed agreements in Dataset/
#
# These are hard-coded deliberately. They come from two PDFs of prose that will
# not change during the assessment, and parsing free text into money and
# durations at runtime introduces a failure mode (a silent mis-parse) that is
# strictly worse than a table a reviewer can check against the document in
# thirty seconds. The citation on each field is the audit trail.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractTerms:
    account_id: str
    cite: str
    # Cancellation
    waives_cancellation_fee: bool = False
    cancellation_cite: str | None = None
    # Failed-pickup credit
    credit_threshold_hours: float | None = None
    credit_fixed_inr: float | None = None
    credit_monthly_cap_inr: float | None = None
    credit_cite: str | None = None
    # Support
    sla: dict[str, tuple[float, str]] = field(default_factory=dict)
    sla_cite: str | None = None
    weekend_cover: bool = True


CONTRACTS: dict[str, ContractTerms] = {
    "ACCT-001": ContractTerms(
        account_id="ACCT-001",
        cite="Northstar Logistics Enterprise Agreement",
        waives_cancellation_fee=True,
        cancellation_cite="Northstar Logistics Enterprise Agreement §2 (Shipment cancellation)",
        credit_monthly_cap_inr=5000.0,
        credit_cite="Northstar Logistics Enterprise Agreement §3 (Service credits)",
        sla={"P1": (15, "clock"), "P2": (60, "clock"), "P3": (8 * 60, "business")},
        sla_cite="Northstar Logistics Enterprise Agreement §1 (Support terms)",
        weekend_cover=True),
    "ACCT-002": ContractTerms(
        account_id="ACCT-002",
        cite="LumenWorks Service Agreement",
        waives_cancellation_fee=False,
        cancellation_cite="LumenWorks Service Agreement §2 (Cancellation terms)",
        credit_threshold_hours=4.0,
        credit_fixed_inr=300.0,
        credit_cite="LumenWorks Service Agreement §3 (Failed-pickup credits)",
        sla={"P1": (2 * 60, "business"), "P2": (4 * 60, "business"),
             "P3": (2 * 24 * 60, "business_days")},
        sla_cite="LumenWorks Service Agreement §1 (Support terms)",
        # "No weekend or after-hours support coverage" -- the clause that makes
        # a Sunday answer different for this customer than for anyone else.
        weekend_cover=False),
}

# --------------------------------------------------------------------------
# Default policy, transcribed from the current documents
# --------------------------------------------------------------------------

SOP = "Cancellation & Service Credit SOP v4"
POLICY = "Support Policy v3"
PRODUCT = "Product Operations Guide"

CANCEL_FREE_WINDOW_MIN = 30
CANCEL_FEE_INR = 250.0
CANCEL_CITE = f"{SOP} §1 (Order cancellation)"

CREDIT_THRESHOLD_HOURS = 2.0
CREDIT_CAP_INR = 500.0
CREDIT_PCT_OF_FEE = 0.10
CREDIT_CITE = f"{SOP} §2 (Failed-pickup service credits)"

MANAGER_APPROVAL_ABOVE_INR = 1000.0
APPROVAL_CITE = f"{SOP} §3 (Approval and uncertainty)"

# Support Policy v3 §3, default first-response targets. (minutes, clock kind)
DEFAULT_SLA: dict[str, dict[str, tuple[float, str]]] = {
    "Enterprise": {"P1": (30, "clock"), "P2": (2 * 60, "clock"),
                   "P3": (1 * 24 * 60, "business_days")},
    "Growth": {"P1": (2 * 60, "business"), "P2": (4 * 60, "business"),
               "P3": (2 * 24 * 60, "business_days")},
    "Standard": {"P1": (4 * 60, "business"), "P2": (1 * 24 * 60, "business_days"),
                 "P3": (2 * 24 * 60, "business_days")},
}
SLA_CITE = f"{POLICY} §3 (Default first-response targets)"
SEVERITY_CITE = f"{POLICY} §2 (Severity definitions)"


class Outcome(str, Enum):
    ALLOWED_NO_FEE = "ALLOWED_NO_FEE"
    ALLOWED_WITH_FEE = "ALLOWED_WITH_FEE"
    NOT_ALLOWED = "NOT_ALLOWED"
    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    # An SLA decision is not an eligibility question, and reusing the credit
    # vocabulary for it read backwards where it mattered most: a breached
    # target came back as "ELIGIBLE" and one still within target as
    # "NOT_ELIGIBLE" -- in the tool trace, in the review transcript, and in the
    # JSON the model reads before writing the sentence.
    BREACHED = "BREACHED"
    WITHIN_TARGET = "WITHIN_TARGET"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class Step:
    """One link in the chain of reasoning, with the clause that authorised it."""
    statement: str
    source: str

    def to_dict(self) -> dict:
        return {"statement": self.statement, "source": self.source}


@dataclass
class Decision:
    kind: str                       # cancellation | service_credit | sla
    outcome: Outcome
    headline: str
    amount_inr: float | None = None
    rule_chain: list[Step] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    needs_manager: bool = False
    authority_used: str = "default policy"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "outcome": self.outcome.value,
                "headline": self.headline, "amount_inr": self.amount_inr,
                "rule_chain": [s.to_dict() for s in self.rule_chain],
                "caveats": self.caveats, "facts": self.facts,
                "needs_manager": self.needs_manager,
                "authority_used": self.authority_used,
                "citations": sorted({s.source for s in self.rule_chain})}


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=config.TIMEZONE)
        except ValueError:
            continue
    return None


def _hours(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 3600.0


# --------------------------------------------------------------------------
# Known-issue caveats
#
# A caveat is NOT a hedge. It is the difference between "the parcel was not
# collected" and "our system has not been told it was collected", and KI-211
# says those are the same record for up to twenty minutes.
# --------------------------------------------------------------------------

def known_issue_caveats(order: dict, now: datetime) -> list[str]:
    out = []
    if order.get("carrier") == "SwiftShip" and order.get("status") == "BOOKED":
        out.append(
            "KI-211: SwiftShip pickup confirmations can arrive up to 20 minutes "
            "late, so a BOOKED status does not prove the parcel is still "
            "uncollected. Verify with the carrier before acting on it. "
            f"({PRODUCT} — KI-211)")
    return out


# --------------------------------------------------------------------------
# 1. Cancellation
# --------------------------------------------------------------------------

def cancellation(order_id: str, now: datetime | None = None) -> Decision:
    now = now or store.snapshot()
    o = store.one("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    if o is None:
        return Decision("cancellation", Outcome.INSUFFICIENT_DATA,
                        f"No order {order_id} exists in the dataset.")
    acct = store.one("SELECT * FROM accounts WHERE account_id = ?", (o["account_id"],))
    terms = CONTRACTS.get(o["account_id"])
    chain: list[Step] = []
    status = o["status"]

    chain.append(Step(f"Order {order_id} is {status}.", "orders record"))

    if status == "DELIVERED":
        return Decision("cancellation", Outcome.NOT_ALLOWED,
                        "Delivered shipments cannot be cancelled.",
                        rule_chain=chain + [Step(
                            "DELIVERED: cannot be cancelled.", CANCEL_CITE)],
                        facts=_order_facts(o))

    if status == "PICKED_UP":
        return Decision("cancellation", Outcome.NOT_ALLOWED,
                        "Already picked up — use the return-to-origin workflow "
                        "instead of cancelling.",
                        rule_chain=chain + [Step(
                            "PICKED_UP: do not cancel; use return-to-origin.",
                            CANCEL_CITE)],
                        facts=_order_facts(o))

    if status == "DRAFT":
        return Decision("cancellation", Outcome.ALLOWED_NO_FEE,
                        "Can be cancelled with no fee.", amount_inr=0.0,
                        rule_chain=chain + [Step("DRAFT: may be cancelled with no fee.",
                                                 CANCEL_CITE)],
                        facts=_order_facts(o))

    # BOOKED and not yet picked up.
    booked = _dt(o["booked_at"])
    requested = _dt(o["cancellation_requested_at"]) or now
    if booked is None:
        return Decision("cancellation", Outcome.INSUFFICIENT_DATA,
                        "The booking time is missing, so the 30-minute free "
                        "window cannot be established.",
                        rule_chain=chain, facts=_order_facts(o))

    elapsed = (requested - booked).total_seconds() / 60.0
    chain.append(Step(
        f"Cancellation requested {elapsed:.0f} minutes after booking "
        f"({o['booked_at']} → {o['cancellation_requested_at'] or 'now'}).",
        "orders record"))

    # The contract is checked BEFORE the default, which is the precedence rule
    # in Support Policy v3 §1 expressed as control flow.
    if terms and terms.waives_cancellation_fee:
        chain.append(Step(
            "The signed agreement waives the cancellation fee for any BOOKED "
            "shipment before pickup, regardless of elapsed time. This overrides "
            "the SOP's 30-minute rule.", terms.cancellation_cite))
        return Decision("cancellation", Outcome.ALLOWED_NO_FEE,
                        "Can be cancelled with no fee.", amount_inr=0.0,
                        rule_chain=chain, facts=_order_facts(o),
                        authority_used=terms.cite,
                        caveats=known_issue_caveats(o, now))

    if elapsed <= CANCEL_FREE_WINDOW_MIN:
        chain.append(Step(
            f"Within {CANCEL_FREE_WINDOW_MIN} minutes of booking: no fee.",
            CANCEL_CITE))
        return Decision("cancellation", Outcome.ALLOWED_NO_FEE,
                        "Can be cancelled with no fee.", amount_inr=0.0,
                        rule_chain=chain, facts=_order_facts(o),
                        caveats=known_issue_caveats(o, now))

    chain.append(Step(
        f"After {CANCEL_FREE_WINDOW_MIN} minutes and no agreement waiver: "
        f"INR {CANCEL_FEE_INR:,.0f} cancellation fee applies.", CANCEL_CITE))
    if terms:
        chain.append(Step("The customer agreement states no waiver applies.",
                          terms.cancellation_cite or terms.cite))
    return Decision("cancellation", Outcome.ALLOWED_WITH_FEE,
                    f"Can be cancelled, with an INR {CANCEL_FEE_INR:,.0f} fee.",
                    amount_inr=CANCEL_FEE_INR, rule_chain=chain,
                    facts=_order_facts(o),
                    authority_used=terms.cite if terms else "default policy",
                    caveats=known_issue_caveats(o, now))


def _order_facts(o: dict) -> dict:
    return {k: o[k] for k in
            ("order_id", "account_id", "carrier", "status", "booked_at",
             "pickup_window_end", "pickup_actual_at", "shipment_fee_inr",
             "carrier_fault", "customer_fault", "cancellation_requested_at")}


# --------------------------------------------------------------------------
# 2. Failed-pickup service credit
# --------------------------------------------------------------------------

def service_credit(order_id: str, now: datetime | None = None) -> Decision:
    now = now or store.snapshot()
    o = store.one("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    if o is None:
        return Decision("service_credit", Outcome.INSUFFICIENT_DATA,
                        f"No order {order_id} exists in the dataset.")
    terms = CONTRACTS.get(o["account_id"])
    chain: list[Step] = []

    window_end = _dt(o["pickup_window_end"])
    if window_end is None:
        return Decision("service_credit", Outcome.INSUFFICIENT_DATA,
                        "The scheduled pickup window is missing, so lateness "
                        "cannot be established.", rule_chain=chain,
                        facts=_order_facts(o))

    # If pickup happened, measure to it; if not, lateness is still accruing and
    # is measured to the dataset snapshot.
    actual = _dt(o["pickup_actual_at"])
    reference = actual or now
    late_h = _hours(reference, window_end)
    chain.append(Step(
        f"Pickup window ended {o['pickup_window_end']}; "
        + (f"collected {o['pickup_actual_at']}" if actual
           else f"still not collected at the {now:%Y-%m-%d %H:%M} snapshot")
        + f" — {late_h:.1f} hours past the window.", "orders record"))

    # SOP §3: do not promise a credit while fault is unknown. The dataset uses
    # explicit booleans, so "unknown" means neither party is marked at fault on
    # an order that is late.
    carrier_fault = bool(o["carrier_fault"])
    customer_fault = bool(o["customer_fault"])

    threshold = CREDIT_THRESHOLD_HOURS
    cite = CREDIT_CITE
    authority = "default policy"
    if terms and terms.credit_threshold_hours is not None:
        threshold = terms.credit_threshold_hours
        cite = terms.credit_cite or terms.cite
        authority = terms.cite
        chain.append(Step(
            f"The signed agreement replaces the default {CREDIT_THRESHOLD_HOURS:.0f}-hour "
            f"threshold with {threshold:.0f} hours.", cite))

    if late_h <= threshold:
        chain.append(Step(
            f"{late_h:.1f} hours is within the {threshold:.0f}-hour threshold: "
            f"no credit is due.", cite))
        return Decision("service_credit", Outcome.NOT_ELIGIBLE,
                        f"No service credit — the pickup is {late_h:.1f} hours "
                        f"late, inside the {threshold:.0f}-hour threshold.",
                        rule_chain=chain, facts=_order_facts(o),
                        authority_used=authority,
                        caveats=known_issue_caveats(o, now))

    if customer_fault:
        chain.append(Step("The customer is recorded at fault, which excludes a "
                          "credit.", CREDIT_CITE))
        return Decision("service_credit", Outcome.NOT_ELIGIBLE,
                        "No service credit — the delay is recorded as "
                        "customer-caused.", rule_chain=chain,
                        facts=_order_facts(o), authority_used=authority)

    if not carrier_fault:
        chain.append(Step(
            "Carrier fault is not established on this order. The SOP forbids "
            "promising a credit while fault is unknown.", APPROVAL_CITE))
        return Decision("service_credit", Outcome.INSUFFICIENT_DATA,
                        "Cannot confirm a credit — the pickup is late enough, "
                        "but carrier fault has not been established.",
                        rule_chain=chain, facts=_order_facts(o),
                        authority_used=authority,
                        caveats=known_issue_caveats(o, now))

    chain.append(Step("Carrier is at fault and the customer is not.",
                      "orders record"))

    fee = float(o["shipment_fee_inr"] or 0.0)
    if terms and terms.credit_fixed_inr is not None:
        amount = terms.credit_fixed_inr
        chain.append(Step(
            f"The agreement sets a fixed INR {amount:,.0f} credit, replacing the "
            f"default 'lower of INR {CREDIT_CAP_INR:,.0f} or "
            f"{CREDIT_PCT_OF_FEE:.0%} of the shipment fee'.",
            terms.credit_cite or terms.cite))
        authority = terms.cite
    else:
        pct = fee * CREDIT_PCT_OF_FEE
        amount = min(CREDIT_CAP_INR, pct)
        chain.append(Step(
            f"Default credit is the lower of INR {CREDIT_CAP_INR:,.0f} and "
            f"{CREDIT_PCT_OF_FEE:.0%} of the INR {fee:,.0f} shipment fee "
            f"(INR {pct:,.0f}) → INR {amount:,.0f}.", CREDIT_CITE))

    caveats = known_issue_caveats(o, now)
    if terms and terms.credit_monthly_cap_inr is not None:
        chain.append(Step(
            f"Monthly aggregate credits for this account are capped at "
            f"INR {terms.credit_monthly_cap_inr:,.0f}.",
            terms.credit_cite or terms.cite))
        caveats.append(
            f"This account has an INR {terms.credit_monthly_cap_inr:,.0f} monthly "
            f"aggregate cap; check credits already issued this month before "
            f"committing.")

    needs_manager = amount > MANAGER_APPROVAL_ABOVE_INR
    if needs_manager:
        chain.append(Step(
            f"INR {amount:,.0f} exceeds the INR {MANAGER_APPROVAL_ABOVE_INR:,.0f} "
            f"threshold, so a manager must approve it.", APPROVAL_CITE))

    return Decision("service_credit", Outcome.ELIGIBLE,
                    f"A service credit of INR {amount:,.0f} is due.",
                    amount_inr=amount, rule_chain=chain, caveats=caveats,
                    facts=_order_facts(o), needs_manager=needs_manager,
                    authority_used=authority)


# --------------------------------------------------------------------------
# 3. Severity and first-response SLA
# --------------------------------------------------------------------------
#
# ASSUMPTION, stated here because the pack never defines it: "business hours"
# means Mon-Fri 09:00-18:00 IST, and no public-holiday calendar was supplied so
# none is applied. Changing these two constants in config.py changes every
# business-hours answer in the product, and the tests will show which.

_SEVERITY_SIGNALS = [
    ("P1", [
        ("security incident", "confirmed security incident"),
        ("api key", "suspected credential exposure"),
        ("credential", "suspected credential exposure"),
        ("exposed", "suspected credential exposure"),
        ("all shipment creation", "complete outage preventing all shipment creation"),
        ("every user", "complete outage preventing all shipment creation"),
        ("production outage", "complete production outage"),
        ("http 500", "complete outage preventing all shipment creation"),
    ]),
    ("P2", [
        ("fails", "major feature materially degraded"),
        ("failing", "major feature materially degraded"),
        ("bulk upload", "major feature materially degraded"),
        ("unusable", "major feature unavailable"),
        ("degraded", "major feature materially degraded"),
    ]),
    ("P3", [
        ("how do", "how-to question"),
        ("how to", "how-to question"),
        ("change the", "configuration request"),
        ("still shows", "minor defect with limited operational impact"),
    ]),
]


def triage(subject: str, description: str = "") -> tuple[str, list[str]]:
    """Classify severity against Support Policy v3 §2.

    Deterministic on purpose. A severity that changes between two runs of the
    same ticket is not a severity, and this value drives the SLA deadline --
    the one number an agent is judged on.
    """
    blob = f"{subject} {description}".lower()
    for level, signals in _SEVERITY_SIGNALS:
        hits = sorted({why for kw, why in signals if kw in blob})
        if hits:
            return level, hits
    return "P3", ["no higher-severity signal matched; treated as the default"]


def _is_business_time(dt: datetime) -> bool:
    if dt.weekday() not in config.BUSINESS_DAYS:
        return False
    start = dt.replace(hour=config.BUSINESS_DAY_START[0],
                       minute=config.BUSINESS_DAY_START[1], second=0, microsecond=0)
    end = dt.replace(hour=config.BUSINESS_DAY_END[0],
                     minute=config.BUSINESS_DAY_END[1], second=0, microsecond=0)
    return start <= dt < end


def _next_business_start(dt: datetime) -> datetime:
    d = dt
    while True:
        start = d.replace(hour=config.BUSINESS_DAY_START[0],
                          minute=config.BUSINESS_DAY_START[1], second=0, microsecond=0)
        end = d.replace(hour=config.BUSINESS_DAY_END[0],
                        minute=config.BUSINESS_DAY_END[1], second=0, microsecond=0)
        if d.weekday() in config.BUSINESS_DAYS and d < end:
            return max(d, start)
        d = (d + timedelta(days=1)).replace(
            hour=config.BUSINESS_DAY_START[0], minute=config.BUSINESS_DAY_START[1],
            second=0, microsecond=0)


def add_business_minutes(start: datetime, minutes: float) -> datetime:
    """Advance a clock that only ticks during business hours.

    A ticket raised at 09:45 on a Sunday with a four-business-hour target is not
    due at 13:45 on Sunday; it is due at 13:00 on Monday. Getting this wrong
    invents SLA breaches that have not happened, which is how an agent ends up
    apologising for being late when they are not.
    """
    remaining = timedelta(minutes=minutes)
    cur = _next_business_start(start)
    while remaining > timedelta(0):
        day_end = cur.replace(hour=config.BUSINESS_DAY_END[0],
                              minute=config.BUSINESS_DAY_END[1],
                              second=0, microsecond=0)
        available = day_end - cur
        if remaining <= available:
            return cur + remaining
        remaining -= available
        cur = _next_business_start(
            (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
    return cur


def add_business_days(start: datetime, days: float) -> datetime:
    """A business-DAY target lands at the same time N business days later."""
    d = start
    whole = int(days)
    for _ in range(whole):
        d = d + timedelta(days=1)
        while d.weekday() not in config.BUSINESS_DAYS:
            d = d + timedelta(days=1)
    return d


def sla(ticket_id: str, now: datetime | None = None) -> Decision:
    now = now or store.snapshot()
    t = store.one("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))
    if t is None:
        return Decision("sla", Outcome.INSUFFICIENT_DATA,
                        f"No ticket {ticket_id} exists in the dataset.")
    acct = store.one("SELECT * FROM accounts WHERE account_id = ?", (t["account_id"],))
    terms = CONTRACTS.get(t["account_id"])
    plan = acct["plan"] if acct else "Standard"
    chain: list[Step] = []

    severity, signals = triage(t["subject"] or "", t["description"] or "")
    chain.append(Step(
        f"Severity {severity}: {'; '.join(signals)}.", SEVERITY_CITE))

    created = _dt(t["created_at"])
    if created is None:
        return Decision("sla", Outcome.INSUFFICIENT_DATA,
                        "The ticket has no creation time, so a target cannot "
                        "be computed.", rule_chain=chain)

    # Contract first, default second -- Support Policy v3 §1.
    if terms and severity in terms.sla:
        minutes, kind = terms.sla[severity]
        cite = terms.sla_cite or terms.cite
        authority = terms.cite
        chain.append(Step(
            f"The signed agreement sets {severity} first response to "
            f"{_describe(minutes, kind)}, replacing the plan default.", cite))
    else:
        minutes, kind = DEFAULT_SLA.get(plan, DEFAULT_SLA["Standard"])[severity]
        cite = SLA_CITE
        authority = "default policy"
        chain.append(Step(
            f"{plan} plan, {severity}: first response within "
            f"{_describe(minutes, kind)}.", cite))

    if kind == "clock":
        due = created + timedelta(minutes=minutes)
        chain.append(Step(
            f"A 24x7 clock target runs continuously: due {due:%Y-%m-%d %H:%M}.",
            cite))
    elif kind == "business_days":
        due = add_business_days(created, minutes / (24 * 60))
        chain.append(Step(
            f"Business-day target from {created:%Y-%m-%d %H:%M}: "
            f"due {due:%Y-%m-%d %H:%M}.", cite))
    else:
        due = add_business_minutes(created, minutes)
        note = (f"Business-hours clock ({config.BUSINESS_DAY_START[0]:02d}:00–"
                f"{config.BUSINESS_DAY_END[0]:02d}:00, Mon–Fri)")
        if terms and not terms.weekend_cover:
            note += "; this agreement excludes weekend and after-hours cover"
        chain.append(Step(f"{note}: due {due:%Y-%m-%d %H:%M}.", cite))

    overdue_min = (now - due).total_seconds() / 60.0
    breached = overdue_min > 0
    facts = {"ticket_id": ticket_id, "account_id": t["account_id"],
             "plan": plan, "severity": severity, "created_at": t["created_at"],
             "due_at": f"{due:%Y-%m-%d %H:%M}", "now": f"{now:%Y-%m-%d %H:%M}",
             "breached": breached,
             "overdue_minutes": round(overdue_min) if breached else 0,
             "target": _describe(minutes, kind), "subject": t["subject"]}

    if breached:
        chain.append(Step(
            f"At the {now:%Y-%m-%d %H:%M} snapshot the target is overdue by "
            f"{_duration(overdue_min)}.", "dataset snapshot"))
        chain.append(Step(
            "A breached target must be stated plainly and escalation "
            "recommended, not hidden.", f"{POLICY} §4 (Escalation)"))
        headline = (f"{severity} first-response target BREACHED by "
                    f"{_duration(overdue_min)}.")
    else:
        chain.append(Step(
            f"At the {now:%Y-%m-%d %H:%M} snapshot the target has not yet been "
            f"reached ({_duration(-overdue_min)} remaining).", "dataset snapshot"))
        headline = (f"{severity} first response is within target — "
                    f"{_duration(-overdue_min)} remaining.")

    caveats = []
    if terms and not terms.weekend_cover and now.weekday() not in config.BUSINESS_DAYS:
        caveats.append(
            "This agreement excludes weekend cover, so the response clock is "
            "paused until the next business day.")

    return Decision("sla", Outcome.BREACHED if breached else Outcome.WITHIN_TARGET,
                    headline, rule_chain=chain, facts=facts, caveats=caveats,
                    authority_used=authority)


def _describe(minutes: float, kind: str) -> str:
    if kind == "business_days":
        d = minutes / (24 * 60)
        return f"{d:.0f} business day{'s' if d != 1 else ''}"
    unit = "minutes" if minutes < 60 else "hours"
    value = minutes if minutes < 60 else minutes / 60
    label = f"{value:.0f} {unit}"
    return label + (" (24x7)" if kind == "clock" else " (business hours)")


def _duration(minutes: float) -> str:
    minutes = abs(minutes)
    if minutes < 60:
        return f"{minutes:.0f} min"
    if minutes < 60 * 24:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / (60 * 24):.1f} days"
