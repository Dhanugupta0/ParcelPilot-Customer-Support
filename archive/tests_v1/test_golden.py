"""Golden test suite.

Each case below corresponds to a trap deliberately planted in the data pack:
a contract that overrides a policy, a deprecated document retained to be
mis-retrieved, a past resolution that is wrong, a question that is unanswerable
without knowing the account, a topic the pack simply does not cover, and a
snapshot that falls on a Sunday.

These run without an API key, because everything they assert is decided by
deterministic code rather than by the model. That is the point of the
architecture: the parts that must be right are testable.
"""
from __future__ import annotations

import pytest

from app.core import clock
from app.core.models import Severity
from app.core.principal import AccessDenied, load_principal
from app.data.repository import Repository
from app.policy.engine import (Confidence, Outcome, decide_cancellation,
                               decide_service_credit, resolve_sla)
from app.policy.rules import defaults
from app.policy.triage import triage
from app.retrieval.governed import GovernedRetriever


def _order_and_account(repo, order_id):
    o = repo.get_order(order_id)
    return o, repo.get_account(o.account_id)


# ==========================================================================
# 1. Reference time
# ==========================================================================

def test_snapshot_comes_from_workbook_readme():
    assert clock.now().strftime("%Y-%m-%d %H:%M") == "2026-08-16 11:00"


def test_snapshot_is_a_sunday():
    """Load-bearing: business-hours SLAs are not running at the snapshot."""
    assert clock.now().strftime("%A") == "Sunday"
    assert not clock.is_within_business_hours(clock.now())


# ==========================================================================
# 2. Cancellation — contract vs SOP
# ==========================================================================

def test_northstar_cancellation_waived_by_contract(repo):
    """ORD-1001: 120 minutes after booking, but the agreement waives the fee.

    The SOP alone would charge INR 250. The trap is that a closed ticket
    (TKT-450) says exactly that, and it is wrong for this customer.
    """
    o, a = _order_and_account(repo, "ORD-1001")
    d = decide_cancellation(o, a)
    assert d.outcome is Outcome.ALLOWED_NO_FEE
    assert d.amount_inr == 0.0
    assert d.authority_used == "contract"
    assert any("overrides" in x.lower() for x in d.overrides_applied)


def test_lumenworks_cancellation_charges_fee(repo):
    """ORD-2001: 75 minutes, and the agreement explicitly declines a waiver."""
    o, a = _order_and_account(repo, "ORD-2001")
    d = decide_cancellation(o, a)
    assert d.outcome is Outcome.ALLOWED_WITH_FEE
    assert d.amount_inr == 250.0
    assert d.authority_used == "policy_default"


def test_beacon_cancellation_within_free_window(repo):
    """ORD-3001: 15 minutes, no agreement -> the SOP's free window applies."""
    o, a = _order_and_account(repo, "ORD-3001")
    d = decide_cancellation(o, a)
    assert d.outcome is Outcome.ALLOWED_NO_FEE
    assert d.amount_inr == 0.0


def test_picked_up_order_routes_to_return_to_origin(repo):
    o, a = _order_and_account(repo, "ORD-1002")
    d = decide_cancellation(o, a)
    assert d.outcome is Outcome.USE_ALTERNATIVE_WORKFLOW
    assert "return-to-origin" in d.headline.lower()


def test_delivered_order_cannot_be_cancelled(repo):
    o, a = _order_and_account(repo, "ORD-4001")
    assert decide_cancellation(o, a).outcome is Outcome.NOT_ALLOWED


def test_stale_pickup_status_caveat_on_swiftship(repo):
    """KI-211: a BOOKED SwiftShip order inside its window may already be collected."""
    o, a = _order_and_account(repo, "ORD-1001")
    d = decide_cancellation(o, a)
    codes = {c.code for c in d.caveats}
    assert "STALE_PICKUP_STATUS" in codes
    assert d.confidence is Confidence.MEDIUM


# ==========================================================================
# 3. Service credits — contract replaces threshold AND amount
# ==========================================================================

def test_lumenworks_credit_uses_contract_threshold_and_amount(repo):
    """ORD-2002: 4.5h late. Default SOP would give INR 240 at a 2h threshold;
    the agreement replaces both with 'more than 4 hours' and a fixed INR 300."""
    o, a = _order_and_account(repo, "ORD-2002")
    d = decide_service_credit(o, a)
    assert d.outcome is Outcome.ELIGIBLE
    assert d.amount_inr == 300.0
    assert d.authority_used == "contract"
    assert len(d.overrides_applied) == 2
    assert not d.requires_manager_approval          # 300 < the 1,000 approval line


def test_credit_refused_when_carrier_fault_unknown(repo):
    """SOP v4 s3 forbids promising a credit while fault is unknown."""
    o, a = _order_and_account(repo, "ORD-1001")
    d = decide_service_credit(o, a)
    assert d.outcome is Outcome.INSUFFICIENT_DATA
    assert d.confidence is Confidence.LOW
    assert d.missing_data


def test_three_hour_delay_is_account_dependent(repo):
    """The brief's own example question cannot be answered without the account.

    A three-hour carrier-fault delay qualifies under the default SOP (>2h) but
    NOT for LumenWorks, whose agreement sets the threshold at >4h. Any system
    that answers this generically is wrong for one of the two customers.
    """
    from app.core.models import Order, OrderStatus
    from app.core.clock import parse_dt

    def probe(account_id: str):
        a = repo.get_account(account_id)
        o = Order(order_id="PROBE", account_id=account_id, carrier="SwiftShip",
                  status=OrderStatus.BOOKED,
                  booked_at=parse_dt("2026-08-16 04:00"),
                  pickup_window_start=parse_dt("2026-08-16 06:00"),
                  pickup_window_end=parse_dt("2026-08-16 08:00"),
                  pickup_actual_at=parse_dt("2026-08-16 11:00"),   # 3h late
                  shipment_fee_inr=4000.0, carrier_fault=True, customer_fault=False)
        return decide_service_credit(o, a)

    northstar = probe("ACCT-001")     # no credit clause -> SOP default, >2h
    lumenworks = probe("ACCT-002")    # contract threshold >4h
    assert northstar.outcome is Outcome.ELIGIBLE
    assert northstar.amount_inr == 400.0          # min(500, 10% of 4000)
    assert lumenworks.outcome is Outcome.NOT_ELIGIBLE


# ==========================================================================
# 4. SLA — three-layer precedence on the correct clock
# ==========================================================================

def test_contract_sla_beats_current_policy(repo):
    """Northstar P1 = 15 min (contract), not 30 min (policy v3), not 1 h (v2)."""
    a = repo.get_account("ACCT-001")
    s = resolve_sla(a, Severity.P1, clock.parse_dt("2026-08-16 10:30"))
    assert s.target.startswith("15 minutes")
    assert s.authority_used == "contract"


def test_policy_default_used_when_no_agreement(repo):
    """Axis Labs is Enterprise with no agreement -> Support Policy v3's 30 min."""
    a = repo.get_account("ACCT-004")
    s = resolve_sla(a, Severity.P1, clock.parse_dt("2026-08-16 08:30"))
    assert s.target.startswith("30 minutes")
    assert s.authority_used == "policy_default"


def test_deprecated_policy_values_never_used():
    """Support Policy v2 says Enterprise P1 = 1 hour. It must be unreachable."""
    d = defaults()
    for plan, row in d.sla_matrix.items():
        assert row["P1"] != "1 hour", "deprecated v2 value leaked into the matrix"
    assert d.sla_matrix["Enterprise"]["P1"].startswith("30 minutes")


def test_p1_breaches_are_detected_on_continuous_clock(repo):
    a = repo.get_account("ACCT-004")
    s = resolve_sla(a, Severity.P1, clock.parse_dt("2026-08-16 08:30"))
    assert s.clock_type == "24x7"
    assert s.breached
    assert s.minutes_over == pytest.approx(120.0, abs=1)


def test_business_hours_sla_not_running_on_sunday(repo):
    """LumenWorks P2 target has not started: the snapshot is a Sunday and their
    agreement excludes weekend cover."""
    a = repo.get_account("ACCT-002")
    s = resolve_sla(a, Severity.P2, clock.parse_dt("2026-08-16 09:45"))
    assert s.clock_type == "business"
    assert s.elapsed_minutes == 0.0
    assert not s.breached
    assert s.deadline.startswith("2026-08-17")     # rolls to Monday


# ==========================================================================
# 5. Severity triage
# ==========================================================================

@pytest.mark.parametrize("ticket_id,expected", [
    ("TKT-501", Severity.P1),   # every user, HTTP 500, no real workaround
    ("TKT-505", Severity.P1),   # suspected credential exposure
    ("TKT-502", Severity.P2),   # bulk upload degraded, workaround exists
    ("TKT-503", Severity.P3),   # how-to / configuration
])
def test_triage(repo, ticket_id, expected):
    assert triage(repo.get_ticket(ticket_id)).severity is expected


def test_viewing_is_not_a_workaround_for_creating(repo):
    """TKT-501 says existing shipments 'can still be viewed'. That is not a
    workaround for being unable to create, and must not demote the P1."""
    t = repo.get_ticket("TKT-501")
    assert triage(t).severity is Severity.P1


# ==========================================================================
# 6. Access control (enforced in the data layer)
# ==========================================================================

def test_customer_cannot_read_another_accounts_order():
    r = Repository(load_principal("cust_lumenworks"))
    with pytest.raises(AccessDenied):
        r.get_order("ORD-1001")


def test_denial_and_not_found_are_indistinguishable():
    """Otherwise the error channel becomes an ID-enumeration oracle."""
    r = Repository(load_principal("cust_lumenworks"))
    msgs = []
    for oid in ("ORD-1001", "ORD-9999"):
        try:
            r.get_order(oid)
        except AccessDenied as e:
            msgs.append(e.message.replace(oid, "X"))
    assert len(msgs) == 2 and msgs[0] == msgs[1]


def test_customer_never_sees_historical_resolution():
    r = Repository(load_principal("cust_northstar"))
    view = r.get_ticket("TKT-450").view(r.p)
    assert view["historical_resolution"] == "[redacted - not available in a customer session]"


def test_staff_see_historical_resolution_but_flagged(repo):
    view = repo.get_ticket("TKT-450").view(repo.p)
    assert view["historical_resolution"]["reliability"] == "UNRELIABLE"


def test_customer_cannot_retrieve_another_customers_contract():
    g = GovernedRetriever(load_principal("cust_lumenworks"))
    res = g.search("Northstar cancellation terms enterprise agreement")
    for c in res.results:
        assert c.scoped_account_id in (None, "ACCT-002")
    assert not any("Northstar" in c.citation for c in res.results)


def test_synthetic_history_hidden_from_customers():
    cust = Repository(load_principal("cust_northstar"))
    assert all(not t.synthetic for t in cust.list_tickets())


# ==========================================================================
# 7. Governed retrieval and conflict detection
# ==========================================================================

def test_deprecated_policy_excluded_and_reported(staff):
    res = GovernedRetriever(staff).search("Enterprise P1 first response target")
    assert not any("v2" in c.citation for c in res.results)
    assert any("v2" in e.citation and "Superseded" in e.reason for e in res.excluded)


def test_wrong_historical_resolution_flagged_as_conflict(staff):
    """TKT-450 told Northstar a INR 250 fee applied. Their contract waives it."""
    res = GovernedRetriever(staff).search(
        "Can Northstar cancel a booked shipment without a cancellation fee?",
        account_context="ACCT-001")
    conflicts = [c for c in res.conflicts if "TKT-450" in c.conflicting]
    assert conflicts, "the incorrect past resolution was not detected"
    assert "Northstar" in conflicts[0].authoritative


def test_wrong_bulk_upload_answer_flagged_as_conflict(staff):
    """TKT-451 said Growth caps at 3,000 rows. The guide says 5,000."""
    res = GovernedRetriever(staff).search(
        "bulk upload row limit Growth plan", account_context="ACCT-002")
    assert any("TKT-451" in c.conflicting for c in res.conflicts)


def test_resolved_known_issue_excluded_from_answers(staff):
    """KI-176 is resolved; the guide forbids using it to explain new incidents."""
    res = GovernedRetriever(staff).search("address validation problem")
    assert not any("KI-176" in c.citation for c in res.results)


def test_live_known_issue_is_retrievable(staff):
    """KI-211 is only Monitoring, not resolved, and TKT-504 depends on it."""
    res = GovernedRetriever(staff).search("SwiftShip order still shows BOOKED after pickup")
    assert any("KI-211" in c.citation for c in res.results)


# ==========================================================================
# 8. Rules are parsed from source, not hard-coded
# ==========================================================================

def test_all_policy_rules_parsed_with_citations():
    d = defaults()
    for name in ("free_cancellation_window_minutes", "cancellation_fee_inr",
                 "credit_threshold_hours", "credit_cap_inr",
                 "credit_percent_of_fee", "manager_approval_above_inr"):
        rule = getattr(d, name)
        assert rule is not None, f"{name} not parsed"
        assert rule.citation and rule.quote


def test_contract_terms_extracted_with_quotes():
    from app.ingest.contract_terms import terms_for
    n = terms_for("ACCT-001")
    assert n.cancellation_fee_waived.value is True
    assert "no cancellation fee" in n.cancellation_fee_waived.quote.lower()

    lw = terms_for("ACCT-002")
    assert lw.cancellation_fee_waived.value is False      # explicit refusal
    assert lw.credit_threshold_hours.value == 4.0
    assert lw.credit_fixed_amount_inr.value == 300.0


def test_no_agreement_means_standard_policy():
    from app.ingest.contract_terms import terms_for
    assert terms_for("ACCT-003") is None
    assert terms_for("ACCT-004") is None
