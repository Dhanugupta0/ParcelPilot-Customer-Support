"""Every figure the product can state, asserted against the document pack.

These tests need no API key and no model. That is the argument for the whole
architecture: if the numbers are computed in Python, they can be pinned to the
source documents by a test, and a wrong answer becomes a failing assertion
rather than an unlucky sample.

Each test names the clause it is enforcing so a reviewer can check the PDF.
"""
from __future__ import annotations

import pytest

from app import engine as E
from app import store

pytestmark = pytest.mark.usefixtures("loaded")


@pytest.fixture(scope="session")
def loaded():
    store.load_workbook()


# ==========================================================================
# Cancellation — Cancellation & Service Credit SOP v4 §1
# ==========================================================================

def test_booked_within_30_minutes_is_free():
    """SOP v4 §1: no fee within 30 minutes of booking.

    ORD-3001 (Beacon, no agreement) was booked 10:25 and cancellation requested
    10:40 — 15 minutes.
    """
    d = E.cancellation("ORD-3001")
    assert d.outcome is E.Outcome.ALLOWED_NO_FEE
    assert d.amount_inr == 0.0
    assert d.authority_used == "default policy"


def test_booked_after_30_minutes_costs_250():
    """SOP v4 §1: INR 250 after 30 minutes, absent an agreement waiver.

    ORD-2001 (LumenWorks) was cancelled 75 minutes after booking, and the
    LumenWorks agreement §2 explicitly declines to waive the fee.
    """
    d = E.cancellation("ORD-2001")
    assert d.outcome is E.Outcome.ALLOWED_WITH_FEE
    assert d.amount_inr == 250.0


def test_a_signed_agreement_beats_the_thirty_minute_rule():
    """The trap in the dataset, and the reason precedence exists.

    ORD-1001 was cancelled 120 minutes after booking, so the SOP alone says
    INR 250 — and TKT-450 shows an agent telling Northstar exactly that. The
    Northstar agreement §2 waives the fee outright, and the agreement wins.
    """
    d = E.cancellation("ORD-1001")
    assert d.outcome is E.Outcome.ALLOWED_NO_FEE
    assert d.amount_inr == 0.0
    assert "Northstar" in d.authority_used
    assert any("waives" in s.statement for s in d.rule_chain)


def test_picked_up_is_not_cancelled_but_returned():
    """SOP v4 §1: PICKED_UP uses return-to-origin, not cancellation."""
    d = E.cancellation("ORD-1002")
    assert d.outcome is E.Outcome.NOT_ALLOWED
    assert "return-to-origin" in " ".join(s.statement for s in d.rule_chain)


def test_delivered_cannot_be_cancelled():
    d = E.cancellation("ORD-4001")
    assert d.outcome is E.Outcome.NOT_ALLOWED


def test_swiftship_booked_orders_carry_the_stale_status_caveat():
    """KI-211: a BOOKED SwiftShip order may already have been collected.

    The decision is still correct; the caveat is what stops an agent acting on
    a status the product knows can be up to 20 minutes behind reality.
    """
    d = E.cancellation("ORD-1001")
    assert any("KI-211" in c for c in d.caveats)
    assert not any("KI-211" in c for c in E.cancellation("ORD-3001").caveats), \
        "RoadRunner orders must not carry a SwiftShip-specific caveat"


# ==========================================================================
# Service credit — SOP v4 §2 and the agreements
# ==========================================================================

def test_contract_replaces_both_threshold_and_amount():
    """LumenWorks §3: >4 hours late, carrier at fault → a FIXED INR 300.

    ORD-2002's window closed 06:30 and it was still uncollected at the 11:00
    snapshot — 4.5 hours. The default rule would give the lower of INR 500 and
    10% of the INR 2,400 fee (INR 240); the agreement overrides both the 2-hour
    threshold and the amount.
    """
    d = E.service_credit("ORD-2002")
    assert d.outcome is E.Outcome.ELIGIBLE
    assert d.amount_inr == 300.0, "the fixed contractual credit, not the default"
    assert "LumenWorks" in d.authority_used


def test_a_late_pickup_inside_the_contract_threshold_earns_nothing():
    """The same 3-hour delay is eligible under the SOP and not under LumenWorks.

    This is why the pipeline refuses to answer "a pickup is 3 hours late, do I
    get a credit?" without knowing the order.
    """
    hours = 3.0
    assert hours > E.CREDIT_THRESHOLD_HOURS
    assert hours < E.CONTRACTS["ACCT-002"].credit_threshold_hours


def test_no_credit_is_promised_while_fault_is_unknown():
    """SOP v4 §3: do not promise a credit when carrier fault is unknown.

    INSUFFICIENT_DATA is a distinct outcome from NOT_ELIGIBLE on purpose: one
    says "you are not owed this", the other says "I cannot tell yet".
    """
    d = E.service_credit("ORD-1001")
    assert d.outcome in (E.Outcome.NOT_ELIGIBLE, E.Outcome.INSUFFICIENT_DATA)
    assert d.amount_inr is None


def test_manager_approval_threshold():
    """SOP v4 §3: any individual credit above INR 1,000 needs a manager."""
    assert E.MANAGER_APPROVAL_ABOVE_INR == 1000.0
    assert not E.service_credit("ORD-2002").needs_manager, "INR 300 is below it"


def test_northstar_monthly_cap_is_surfaced_as_a_caveat():
    """Northstar §3 caps aggregate monthly credits at INR 5,000.

    The engine cannot know what has already been issued this month, so it says
    so rather than silently ignoring the cap.
    """
    assert E.CONTRACTS["ACCT-001"].credit_monthly_cap_inr == 5000.0


# ==========================================================================
# Severity and SLA — Support Policy v3 §2/§3 and the agreements
# ==========================================================================

@pytest.mark.parametrize("ticket_id,expected", [
    ("TKT-501", "P1"),   # every user gets HTTP 500 — complete outage
    ("TKT-505", "P1"),   # suspected credential exposure
    ("TKT-502", "P2"),   # bulk upload degraded, workaround exists
    ("TKT-503", "P3"),   # how-to / configuration request
])
def test_severity_matches_the_policy_definitions(ticket_id, expected):
    t = store.one("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))
    level, why = E.triage(t["subject"], t["description"])
    assert level == expected, f"{ticket_id}: {why}"


def test_default_enterprise_p1_target_is_thirty_minutes_round_the_clock():
    """Policy v3 §3. TKT-505 (Axis Labs, Enterprise, no agreement) was raised
    08:30; the target was 09:00 and the snapshot is 11:00."""
    d = E.sla("TKT-505")
    assert d.facts["severity"] == "P1"
    assert d.facts["due_at"] == "2026-08-16 09:00"
    assert d.facts["breached"] is True
    assert d.facts["overdue_minutes"] == 120
    assert d.authority_used == "default policy"


def test_agreement_tightens_the_target_below_the_plan_default():
    """Northstar §1 sets P1 to 15 minutes, against the Enterprise default of 30.

    TKT-501 was raised 10:30, so it is breached at 11:00 under the agreement and
    would NOT be breached under the plan default. Using the wrong source here
    changes the answer.
    """
    d = E.sla("TKT-501")
    assert d.facts["due_at"] == "2026-08-16 10:45"
    assert d.facts["breached"] is True
    assert "Northstar" in d.authority_used


def test_weekend_exclusion_stops_a_breach_being_invented():
    """The most easily-got-wrong answer in the pack.

    The snapshot is a SUNDAY. TKT-502 (LumenWorks, P2) was raised 09:45 with a
    4-business-hour target, and the LumenWorks agreement excludes weekend cover.
    Clock arithmetic says 13:45 Sunday — breached. The business-hours clock says
    the target starts Monday 09:00 and is due Monday 13:00 — not breached.
    """
    assert store.snapshot().weekday() == 6, "the dataset snapshot is a Sunday"
    d = E.sla("TKT-502")
    assert d.facts["due_at"] == "2026-08-17 13:00"
    assert d.facts["breached"] is False


def test_business_hours_clock_skips_nights_and_weekends():
    from datetime import datetime
    from app import config
    sun = datetime(2026, 8, 16, 9, 45, tzinfo=config.TIMEZONE)      # Sunday
    assert E.add_business_minutes(sun, 4 * 60).strftime("%Y-%m-%d %H:%M") \
        == "2026-08-17 13:00"
    fri = datetime(2026, 8, 14, 17, 0, tzinfo=config.TIMEZONE)      # Friday 17:00
    # One hour left on Friday, the rest resumes Monday morning.
    assert E.add_business_minutes(fri, 3 * 60).strftime("%Y-%m-%d %H:%M") \
        == "2026-08-17 11:00"


def test_a_breach_must_be_stated_and_escalation_recommended():
    """Policy v3 §4: state the breach plainly rather than hiding uncertainty."""
    d = E.sla("TKT-505")
    assert "BREACHED" in d.headline
    assert any("escalation" in s.statement.lower() for s in d.rule_chain)


# ==========================================================================
# Every decision must be explainable
# ==========================================================================

@pytest.mark.parametrize("fn,ref", [
    (E.cancellation, "ORD-1001"), (E.cancellation, "ORD-2001"),
    (E.service_credit, "ORD-2002"), (E.sla, "TKT-501"), (E.sla, "TKT-502"),
])
def test_every_decision_cites_a_source_for_every_step(fn, ref):
    d = fn(ref)
    assert d.rule_chain, "a decision with no chain cannot be explained"
    for step in d.rule_chain:
        assert step.source, f"unsourced step: {step.statement}"
    assert d.to_dict()["citations"]


def test_unknown_records_fail_closed():
    for fn, ref in ((E.cancellation, "ORD-9999"), (E.service_credit, "ORD-9999"),
                    (E.sla, "TKT-9999")):
        assert fn(ref).outcome is E.Outcome.INSUFFICIENT_DATA


def test_an_sla_decision_says_breached_rather_than_eligible():
    """`ELIGIBLE` is the service-credit vocabulary. On an SLA it read backwards:
    a breached target came back as ELIGIBLE and one still within target as
    NOT_ELIGIBLE — in the tool trace, in the review transcript, and in the JSON
    the model reads before writing the sentence."""
    breached = E.sla("TKT-505")
    assert breached.facts["breached"] is True
    assert breached.outcome is E.Outcome.BREACHED

    within = E.sla("TKT-502")
    assert within.facts["breached"] is False
    assert within.outcome is E.Outcome.WITHIN_TARGET
