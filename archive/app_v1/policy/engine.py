"""Deterministic decision engine.

This is the single most important design decision in the system: the language
model does not decide anything that has a number or an entitlement attached to
it. It routes, retrieves and explains. Every cancellation outcome, credit
amount and SLA verdict is computed here, in ordinary Python, from parsed rules.

The engine returns a Decision carrying a full `rule_chain` -- each step names
the clause it applied and quotes it -- so an answer can be audited line by line
and reproduced exactly. An LLM cannot invent "INR 250" because it is never
asked to produce a number.

Precedence, per Support Policy v3 s1:  contract clause > current SOP/policy.
Every time a contract clause displaces a default, it is recorded in
`overrides_applied` and surfaced to the user, because "your agreement overrides
the standard policy here" is usually the most useful sentence in the answer.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.core import clock
from app.core.models import Account, Order, OrderStatus, Severity
from app.ingest.contract_terms import ContractTerms, terms_for
from app.ingest.corpus import all_chunks
from app.policy.rules import defaults


class Outcome(str, Enum):
    ALLOWED_NO_FEE = "ALLOWED_NO_FEE"
    ALLOWED_WITH_FEE = "ALLOWED_WITH_FEE"
    NOT_ALLOWED = "NOT_ALLOWED"
    USE_ALTERNATIVE_WORKFLOW = "USE_ALTERNATIVE_WORKFLOW"
    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Confidence(str, Enum):
    HIGH = "HIGH"       # deterministic rule + unambiguous data
    MEDIUM = "MEDIUM"   # deterministic rule, but a caveat applies
    LOW = "LOW"         # missing/conflicting data -> do not act, escalate


class RuleStep(BaseModel):
    step: str
    source: str
    quote: str = ""
    result: str


class Caveat(BaseModel):
    """A reason to hedge or verify before acting.

    Distinct from a decision: the rule outcome may be perfectly clear while the
    underlying data is not yet trustworthy. ORD-1001 is the motivating case --
    the contract answer is unambiguous, but the order is a SwiftShip shipment
    inside its pickup window and KI-211 makes 'BOOKED' potentially stale.
    """
    code: str
    message: str
    source: str = ""
    blocks_action: bool = False


class Decision(BaseModel):
    decision_type: str
    subject: str
    outcome: Outcome
    headline: str
    amount_inr: float | None = None
    currency: str = "INR"
    rule_chain: list[RuleStep] = Field(default_factory=list)
    authority_used: str = "policy_default"      # contract | policy_default
    overrides_applied: list[str] = Field(default_factory=list)
    caveats: list[Caveat] = Field(default_factory=list)
    requires_manager_approval: bool = False
    missing_data: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.HIGH
    recommended_action: str | None = None
    computed_at: str = Field(default_factory=lambda: clock.fmt(clock.now()))

    def finalise(self) -> "Decision":
        if self.missing_data:
            self.confidence = Confidence.LOW
        elif any(c.blocks_action for c in self.caveats):
            self.confidence = Confidence.MEDIUM
        elif self.caveats:
            self.confidence = Confidence.MEDIUM
        return self


# --------------------------------------------------------------------------
# Carrier data-freshness caveats, derived from the known-issues documentation
# --------------------------------------------------------------------------

def _pickup_status_caveat(order: Order) -> Caveat | None:
    """Is this order's BOOKED status trustworthy right now?

    Scans the CURRENT known issues for one that (a) names this carrier and
    (b) describes a pickup-confirmation delay, then checks whether we are still
    inside that delay window. Data-driven, so a new carrier webhook issue added
    to the guide is picked up without a code change.
    """
    if order.status is not OrderStatus.BOOKED or not order.pickup_window_start:
        return None
    for ch in all_chunks():
        if not ch.section.startswith("Known issue") or ch.status == "RESOLVED":
            continue
        low = ch.text.lower()
        if order.carrier.lower() not in low:
            continue
        if not re.search(r"pickup.*(webhook|confirmation)|webhook.*pickup", low):
            continue
        m = re.search(r"up to\s+(\d+)\s*minutes", low)
        window = float(m.group(1)) if m else 20.0
        # The status is only suspect once pickup could plausibly have happened.
        since_window_open = (clock.now() - order.pickup_window_start).total_seconds() / 60.0
        if since_window_open < -window:
            return None
        return Caveat(
            code="STALE_PICKUP_STATUS",
            message=(
                f"{order.carrier} pickup confirmations can arrive up to "
                f"{window:.0f} minutes late, so this order showing BOOKED does not "
                f"prove the parcel has not been collected. Verify carrier status "
                f"before taking an action that depends on it."),
            source=ch.citation,
            blocks_action=True,
        )
    return None


# --------------------------------------------------------------------------
# 1. Cancellation
# --------------------------------------------------------------------------

def decide_cancellation(order: Order, account: Account,
                        at: datetime | None = None) -> Decision:
    d = defaults()
    terms: ContractTerms | None = terms_for(account.account_id)
    dec = Decision(decision_type="cancellation", subject=order.order_id,
                   outcome=Outcome.INSUFFICIENT_DATA, headline="")

    dec.rule_chain.append(RuleStep(
        step="Identify order status",
        source="orders sheet (ParcelPilot operational data)",
        result=f"{order.order_id} is {order.status.value} for {account.account_name} "
               f"({account.account_id}, {account.plan.value} plan)"))

    # --- status gates (SOP v4 s1) -----------------------------------------
    if order.status is OrderStatus.DELIVERED:
        dec.outcome = Outcome.NOT_ALLOWED
        dec.headline = "This shipment has already been delivered and cannot be cancelled."
        dec.rule_chain.append(RuleStep(
            step="Apply cancellation rule for DELIVERED",
            source="Cancellation & Service Credit SOP v4 §1 (Order cancellation)",
            quote="DELIVERED: Cannot be cancelled.",
            result="Cancellation not possible"))
        return dec.finalise()

    if order.status is OrderStatus.PICKED_UP:
        dec.outcome = Outcome.USE_ALTERNATIVE_WORKFLOW
        dec.headline = ("This shipment has already been picked up, so it cannot be "
                        "cancelled. The return-to-origin workflow applies instead.")
        dec.rule_chain.append(RuleStep(
            step="Apply cancellation rule for PICKED_UP",
            source="Cancellation & Service Credit SOP v4 §1 (Order cancellation)",
            quote="PICKED_UP: Do not cancel. Use the return-to-origin workflow if the "
                  "customer wants the parcel returned.",
            result="Route to return-to-origin"))
        if terms:
            dec.rule_chain.append(RuleStep(
                step="Check agreement for a conflicting term",
                source=terms.document,
                result="Agreement is consistent with the SOP once a shipment is picked up"))
        dec.recommended_action = "offer_return_to_origin"
        return dec.finalise()

    if order.status is OrderStatus.CANCELLED:
        dec.outcome = Outcome.NOT_ALLOWED
        dec.headline = "This shipment is already cancelled."
        return dec.finalise()

    if order.status is OrderStatus.DRAFT:
        dec.outcome, dec.amount_inr = Outcome.ALLOWED_NO_FEE, 0.0
        dec.headline = "This shipment is still a draft and can be cancelled with no fee."
        dec.rule_chain.append(RuleStep(
            step="Apply cancellation rule for DRAFT",
            source="Cancellation & Service Credit SOP v4 §1 (Order cancellation)",
            quote="DRAFT: May be cancelled with no fee.",
            result="No fee"))
        return dec.finalise()

    # --- BOOKED, not yet picked up ----------------------------------------
    ref = at or order.cancellation_requested_at or clock.now()
    ref_label = ("the recorded cancellation request"
                 if order.cancellation_requested_at and at is None
                 else "the current snapshot time")
    if not order.booked_at:
        dec.missing_data.append("booked_at is empty, so elapsed time cannot be computed")
        dec.headline = "I cannot confirm the cancellation fee because this order has no booking timestamp."
        return dec.finalise()

    elapsed = (ref - order.booked_at).total_seconds() / 60.0
    window = float(d.free_cancellation_window_minutes.value)
    dec.rule_chain.append(RuleStep(
        step="Measure time between booking and cancellation request",
        source="orders sheet",
        result=f"Booked {clock.fmt(order.booked_at)}; measured against {ref_label} "
               f"{clock.fmt(ref)} = {elapsed:.0f} minutes"))

    waiver = terms.cancellation_fee_waived if terms else None

    if waiver is not None and waiver.value is True:
        dec.outcome, dec.amount_inr = Outcome.ALLOWED_NO_FEE, 0.0
        dec.authority_used = "contract"
        dec.overrides_applied.append(
            f"{terms.document} overrides the SOP's INR "
            f"{d.cancellation_fee_inr.value:.0f} fee and the "
            f"{window:.0f}-minute free window entirely.")
        dec.rule_chain.append(RuleStep(
            step="Check for a contract cancellation-fee waiver (highest authority)",
            source=waiver.citation, quote=waiver.quote,
            result="Agreement waives the cancellation fee for BOOKED shipments before "
                   "pickup, regardless of elapsed time"))
        dec.rule_chain.append(RuleStep(
            step="Apply source precedence",
            source="Support Policy v3 §1 (Scope and source precedence)",
            quote="When sources conflict, use the signed customer agreement first, then "
                  "the current support policy, then current product documentation.",
            result="Contract clause wins over the SOP default"))
        dec.headline = (f"{order.order_id} can be cancelled with no cancellation fee, "
                        f"because {account.account_name}'s agreement waives it.")
    else:
        if waiver is not None:
            dec.rule_chain.append(RuleStep(
                step="Check for a contract cancellation-fee waiver",
                source=waiver.citation, quote=waiver.quote,
                result="Agreement explicitly does NOT waive the fee; SOP default applies"))
        elif terms:
            dec.rule_chain.append(RuleStep(
                step="Check for a contract cancellation-fee waiver",
                source=terms.document,
                result="No cancellation-fee waiver in this agreement; SOP default applies"))
        else:
            dec.rule_chain.append(RuleStep(
                step="Check for a signed agreement",
                source="accounts sheet",
                result=f"No customer agreement on file for {account.account_id}; "
                       "standard policy applies"))

        if elapsed <= window:
            dec.outcome, dec.amount_inr = Outcome.ALLOWED_NO_FEE, 0.0
            dec.rule_chain.append(RuleStep(
                step="Apply the SOP free-cancellation window",
                source=d.free_cancellation_window_minutes.citation,
                quote=d.free_cancellation_window_minutes.quote,
                result=f"{elapsed:.0f} min is within the {window:.0f}-minute window → no fee"))
            dec.headline = (f"{order.order_id} can be cancelled with no fee — the request "
                            f"is within {window:.0f} minutes of booking.")
        else:
            fee = float(d.cancellation_fee_inr.value)
            dec.outcome, dec.amount_inr = Outcome.ALLOWED_WITH_FEE, fee
            dec.rule_chain.append(RuleStep(
                step="Apply the SOP cancellation fee",
                source=d.cancellation_fee_inr.citation,
                quote=d.cancellation_fee_inr.quote,
                result=f"{elapsed:.0f} min exceeds the {window:.0f}-minute window → INR {fee:.0f}"))
            dec.headline = (f"{order.order_id} can be cancelled, but an INR {fee:.0f} "
                            f"cancellation fee applies because the request came "
                            f"{elapsed:.0f} minutes after booking.")

    caveat = _pickup_status_caveat(order)
    if caveat:
        dec.caveats.append(caveat)
        dec.recommended_action = "verify_carrier_pickup_status"
    return dec.finalise()


# --------------------------------------------------------------------------
# 2. Failed-pickup service credit
# --------------------------------------------------------------------------

def decide_service_credit(order: Order, account: Account) -> Decision:
    d = defaults()
    terms = terms_for(account.account_id)
    dec = Decision(decision_type="service_credit", subject=order.order_id,
                   outcome=Outcome.INSUFFICIENT_DATA, headline="")

    if not order.pickup_window_end:
        dec.missing_data.append("pickup_window_end is empty")
        dec.headline = "I cannot assess a service credit without the scheduled pickup window."
        return dec.finalise()

    delay_min = order.pickup_delay_minutes() or 0.0
    delay_h = delay_min / 60.0
    measured_to = ("actual pickup" if order.pickup_actual_at
                   else "the dataset snapshot (pickup still has not happened)")
    dec.rule_chain.append(RuleStep(
        step="Measure pickup delay against the scheduled window",
        source="orders sheet",
        result=f"Window ended {clock.fmt(order.pickup_window_end)}; measured to "
               f"{measured_to} → {delay_h:.1f} hours late"))

    # --- threshold: contract may replace the default ----------------------
    thr_clause = terms.credit_threshold_hours if terms else None
    if thr_clause:
        threshold = float(thr_clause.value)
        dec.authority_used = "contract"
        dec.overrides_applied.append(
            f"{terms.document} replaces the SOP's "
            f"{float(d.credit_threshold_hours.value):.0f}-hour threshold with "
            f"{threshold:.0f} hours.")
        dec.rule_chain.append(RuleStep(
            step="Determine the qualifying delay threshold (contract first)",
            source=thr_clause.citation, quote=thr_clause.quote,
            result=f"Contract threshold: more than {threshold:.0f} hours"))
    else:
        threshold = float(d.credit_threshold_hours.value)
        dec.rule_chain.append(RuleStep(
            step="Determine the qualifying delay threshold",
            source=d.credit_threshold_hours.citation,
            quote=d.credit_threshold_hours.quote,
            result=f"Default SOP threshold: more than {threshold:.0f} hours"
                   + (f" (no credit-timing clause in {terms.document})" if terms else "")))

    # --- fault conditions -------------------------------------------------
    # SOP v4 s3: "Do not promise a credit when carrier fault, pickup timing, or
    # customer fault is unknown." Absent fault evidence is NOT the same as
    # carrier fault, so we refuse rather than assume.
    if order.customer_fault:
        dec.outcome = Outcome.NOT_ELIGIBLE
        dec.rule_chain.append(RuleStep(
            step="Check customer-fault condition", source="orders sheet",
            result="customer_fault = True → not eligible"))
        dec.headline = "No service credit applies: the delay is recorded as customer-caused."
        return dec.finalise()

    if not order.carrier_fault:
        dec.outcome = Outcome.INSUFFICIENT_DATA
        dec.missing_data.append(
            "carrier_fault is not recorded as True for this order, and the SOP "
            "forbids promising a credit while fault is unknown")
        dec.rule_chain.append(RuleStep(
            step="Check carrier-fault condition",
            source=d.credit_cap_inr.citation,
            quote="Do not promise a credit when carrier fault, pickup timing, or "
                  "customer fault is unknown.",
            result="carrier_fault is not confirmed → cannot promise a credit"))
        dec.headline = ("I cannot confirm a service credit for this shipment because "
                        "carrier fault has not been established.")
        dec.recommended_action = "escalate_for_fault_verification"
        return dec.finalise()

    dec.rule_chain.append(RuleStep(
        step="Check fault conditions", source="orders sheet",
        result="carrier_fault = True, customer_fault = False → conditions met"))

    if delay_h <= threshold:
        dec.outcome, dec.amount_inr = Outcome.NOT_ELIGIBLE, 0.0
        dec.rule_chain.append(RuleStep(
            step="Compare delay against threshold", source="policy engine",
            result=f"{delay_h:.1f}h does not exceed the {threshold:.0f}h threshold → not eligible"))
        dec.headline = (f"No service credit applies: the pickup was {delay_h:.1f} hours "
                        f"late, which does not exceed the {threshold:.0f}-hour threshold "
                        f"that applies to {account.account_name}.")
        return dec.finalise()

    # --- amount: contract fixed amount, else the SOP formula --------------
    amt_clause = terms.credit_fixed_amount_inr if terms else None
    if amt_clause:
        amount = float(amt_clause.value)
        dec.authority_used = "contract"
        dec.overrides_applied.append(
            f"{terms.document} replaces the SOP credit formula with a fixed "
            f"INR {amount:.0f}.")
        dec.rule_chain.append(RuleStep(
            step="Determine credit amount (contract first)",
            source=amt_clause.citation, quote=amt_clause.quote,
            result=f"Fixed contractual credit: INR {amount:.0f}"))
    else:
        cap = float(d.credit_cap_inr.value)
        pct = float(d.credit_percent_of_fee.value) / 100.0
        pct_amount = pct * float(order.shipment_fee_inr or 0)
        amount = min(cap, pct_amount)
        dec.rule_chain.append(RuleStep(
            step="Compute credit from the SOP formula",
            source=d.credit_cap_inr.citation,
            quote=d.credit_cap_inr.quote,
            result=f"lower of INR {cap:.0f} or {pct*100:.0f}% of INR "
                   f"{order.shipment_fee_inr:.0f} (= INR {pct_amount:.0f}) → INR {amount:.0f}"))

    dec.outcome, dec.amount_inr = Outcome.ELIGIBLE, amount
    dec.headline = (f"{account.account_name} is eligible for an INR {amount:.0f} service "
                    f"credit on {order.order_id} — the pickup was {delay_h:.1f} hours past "
                    f"the scheduled window with carrier fault confirmed.")

    # --- approval + monthly cap ------------------------------------------
    approval_line = float(d.manager_approval_above_inr.value)
    if amount > approval_line:
        dec.requires_manager_approval = True
        dec.rule_chain.append(RuleStep(
            step="Check approval threshold",
            source=d.manager_approval_above_inr.citation,
            quote=d.manager_approval_above_inr.quote,
            result=f"INR {amount:.0f} exceeds INR {approval_line:.0f} → manager approval required"))

    cap_clause = terms.credit_monthly_cap_inr if terms else None
    if cap_clause:
        dec.rule_chain.append(RuleStep(
            step="Check contractual monthly credit cap",
            source=cap_clause.citation, quote=cap_clause.quote,
            result=f"Monthly aggregate cap INR {float(cap_clause.value):.0f} — "
                   f"confirm month-to-date credits before issuing"))
        dec.caveats.append(Caveat(
            code="MONTHLY_CAP_UNVERIFIED",
            message=(f"This agreement caps monthly aggregate service credits at INR "
                     f"{float(cap_clause.value):.0f}. The supplied dataset has no "
                     f"credit ledger, so month-to-date usage cannot be verified here."),
            source=cap_clause.citation))
    return dec.finalise()


# --------------------------------------------------------------------------
# 3. SLA resolution
# --------------------------------------------------------------------------

class SLAStatus(BaseModel):
    subject: str
    account_id: str
    account_name: str
    plan: str
    severity: str
    target: str
    clock_type: str
    authority_used: str
    source: str
    created_at: str | None
    deadline: str | None
    elapsed_minutes: float
    target_minutes: float
    breached: bool
    minutes_over: float = 0.0
    minutes_remaining: float = 0.0
    clock_running: bool = True
    note: str = ""
    rule_chain: list[RuleStep] = Field(default_factory=list)


def resolve_sla(account: Account, severity: Severity | str,
                created_at: datetime, subject: str = "") -> SLAStatus:
    """Resolve the applicable first-response target and measure it correctly.

    Two things routinely go wrong here and both are exercised by this dataset:
    the contract target must displace the policy table, and the target must be
    measured on the right clock. The snapshot is a Sunday, so '24x7' targets
    have been running while 'business hours' targets have not started.
    """
    d = defaults()
    sev = severity.value if isinstance(severity, Severity) else str(severity).upper()
    terms = terms_for(account.account_id)
    chain: list[RuleStep] = []

    contract_hit = terms.sla_target(sev) if terms else None
    if contract_hit:
        target, clause = contract_hit
        authority, source = "contract", clause.citation
        chain.append(RuleStep(
            step="Resolve first-response target (contract first)",
            source=clause.citation, quote=clause.quote,
            result=f"{sev} target from the signed agreement: {target.raw}"))
        default_hit = d.sla_target(account.plan.value, sev)
        if default_hit and default_hit[0].raw != target.raw:
            chain.append(RuleStep(
                step="Note the displaced policy default",
                source=d.sla_citation,
                result=f"Support Policy v3 default for {account.plan.value} {sev} is "
                       f"{default_hit[0].raw}; the agreement replaces it"))
    else:
        default_hit = d.sla_target(account.plan.value, sev)
        if not default_hit:
            raise ValueError(f"No SLA target for plan={account.plan.value} severity={sev}")
        target, source = default_hit[0], default_hit[1]
        authority = "policy_default"
        chain.append(RuleStep(
            step="Resolve first-response target",
            source=source,
            result=f"No agreement clause for {sev}; Support Policy v3 default for "
                   f"{account.plan.value} is {target.raw}"))

    note = ""
    # A contractual coverage exclusion forces the business clock even if the
    # wording of the target itself looks continuous.
    if terms and terms.coverage_exclusion and target.clock is clock.ClockType.CONTINUOUS:
        target = clock.SLATarget(raw=target.raw, minutes=target.minutes,
                                 clock=clock.ClockType.BUSINESS)
        note = "Contract excludes weekend and after-hours coverage, so the clock is business-hours only."
        chain.append(RuleStep(
            step="Apply contractual coverage exclusion",
            source=terms.coverage_exclusion.citation,
            quote=terms.coverage_exclusion.quote,
            result="Target measured on the business clock only"))

    elapsed = target.elapsed_since(created_at)
    deadline = target.deadline_from(created_at)
    breached = elapsed > target.minutes
    running = (target.clock is clock.ClockType.CONTINUOUS
               or clock.is_within_business_hours(clock.now()))
    if not running and not note:
        note = (f"The business clock is not running at the snapshot time "
                f"({clock.now():%A %H:%M}), so this target has not started counting.")

    chain.append(RuleStep(
        step="Measure elapsed time on the correct clock",
        source="policy engine",
        result=f"{target.describe()}; elapsed {clock.humanise_minutes(elapsed)}; "
               f"due {clock.fmt(deadline)}"))

    return SLAStatus(
        subject=subject, account_id=account.account_id, account_name=account.account_name,
        plan=account.plan.value, severity=sev, target=target.raw,
        clock_type=target.clock.value, authority_used=authority, source=source,
        created_at=clock.fmt(created_at), deadline=clock.fmt(deadline),
        elapsed_minutes=round(elapsed, 1), target_minutes=target.minutes,
        breached=breached,
        minutes_over=round(max(0.0, elapsed - target.minutes), 1),
        minutes_remaining=round(max(0.0, target.minutes - elapsed), 1),
        clock_running=running, note=note, rule_chain=chain)
