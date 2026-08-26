"""Proactive outreach tests.

Outreach is held to a stricter standard than chat, because the customer did not
ask for the message. These tests cover the two things that make it safe: the
entitlement must be provable before it is offered, and the suppression rules
must decide correctly who NOT to contact.
"""
from __future__ import annotations

import json

import pytest

from app.agent import actions as A
from app.agent.trust import _claims_in, _numbers_in
from app.core import clock
from app.core.principal import AccessDenied, load_principal
from app.ingest.corpus import all_chunks
from app.outreach import engine as O


@pytest.fixture
def plan(staff):
    return O.build_outreach(staff)


# ==========================================================================
# Access
# ==========================================================================

def test_customers_cannot_see_outreach():
    with pytest.raises(AccessDenied):
        O.build_outreach(load_principal("cust_northstar"))


def test_customer_session_has_no_outreach_tool():
    from app.agent.loop import _tools_for
    names = {s["function"]["name"] for s in _tools_for(load_principal("cust_axis"))}
    assert "get_proactive_outreach" not in names


# ==========================================================================
# Suppression -- the product judgment
# ==========================================================================

def test_customer_who_already_raised_it_is_not_cold_contacted():
    """LumenWorks has TKT-502 open about bulk upload. Emailing them 'we noticed
    your bulk uploads are failing' reads as though nobody read their ticket."""
    p = O.build_outreach(load_principal("agent_rohit"))
    hits = [s for s in p.suppressed
            if s.account_id == "ACCT-002" and s.kind == O.Kind.KNOWN_ISSUE]
    assert hits, "LumenWorks should have been suppressed for the KI-208 advisory"
    assert "already raised" in hits[0].reason.lower()
    assert "TKT-502" in hits[0].detail
    # And they must not also appear as a draft.
    assert not [d for d in p.drafts
                if d.account_id == "ACCT-002" and d.kind == O.Kind.KNOWN_ISSUE]


def test_contract_support_hours_are_respected():
    """LumenWorks' agreement excludes weekend cover and the snapshot is a Sunday.
    Contacting them about a problem they cannot get help with until Monday
    creates anxiety with no route to resolution."""
    p = O.build_outreach(load_principal("agent_rohit"))
    hits = [s for s in p.suppressed if s.account_id == "ACCT-002"
            and "support hours" in s.reason.lower()]
    assert hits
    assert hits[0].retry_after and hits[0].retry_after.startswith("2026-08-17")


def test_unprovable_entitlement_is_never_offered(staff):
    """An order whose carrier fault is not established must not generate a
    proactive credit offer -- SOP v4 §3 forbids promising a credit while fault
    is unknown, and volunteering one is worse than answering one."""
    from app.core.models import Order, OrderStatus
    from app.data.repository import Repository
    from app.policy import engine as pe
    repo = Repository(staff)
    acct = repo.get_account("ACCT-001")
    o = Order(order_id="PROBE-NF", account_id="ACCT-001", carrier="SwiftShip",
              status=OrderStatus.BOOKED,
              booked_at=clock.parse_dt("2026-08-16 04:00"),
              pickup_window_start=clock.parse_dt("2026-08-16 05:00"),
              pickup_window_end=clock.parse_dt("2026-08-16 06:00"),
              shipment_fee_inr=3000.0, carrier_fault=False, customer_fault=False)
    d = pe.decide_service_credit(o, acct)
    assert d.outcome is not pe.Outcome.ELIGIBLE


def test_repeat_contact_is_suppressed(tmp_path, monkeypatch, staff):
    """Sending once must stop the same offer reappearing on the next refresh."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(O, "_LEDGER", ledger)
    O.record_sent("ACCT-003", O.Kind.KNOWN_ISSUE,
                  "Product Operations Guide – KI-211 (SwiftShip pickup webhook delay)",
                  "MSG-TEST")
    recent = O._recent_topics()
    assert recent
    p = O.build_outreach(staff)
    assert not [d for d in p.drafts
                if d.account_id == "ACCT-003" and d.kind == O.Kind.KNOWN_ISSUE]


# ==========================================================================
# Draft content safety
# ==========================================================================

def test_known_issue_facts_are_accurate_not_inverted():
    """KI-208: failures above ~3,000 rows DESPITE a 5,000 limit.

    An earlier line-based extractor dropped the threshold and produced a
    fragment that led a model to state the opposite -- the same mistake TKT-451
    made. The facts must carry both numbers, in the right relationship.
    """
    ki = next(c for c in all_chunks() if "KI-208" in c.citation)
    facts = O._known_issue_facts(ki.text)
    blob = " ".join(facts)
    assert "3,000" in blob and "5,000" in blob
    assert "above approximately 3,000" in blob
    assert "limit remains 5,000" in blob


def test_internal_agent_directives_are_stripped():
    """KI-211 ends with guidance addressed to a support agent. Mailing it to the
    customer is nonsensical and leaks internal process."""
    ki = next(c for c in all_chunks() if "KI-211" in c.citation)
    facts = O._known_issue_facts(ki.text)
    blob = " ".join(facts).lower()
    assert "telling a customer" not in blob
    assert "verify the carrier status" not in blob
    # The customer-facing symptom survives.
    assert "20 minutes" in blob


def test_drafts_only_contain_grounded_figures(plan):
    """Every number in a draft body must trace back to its own fact list."""
    for d in plan.drafts:
        grounded = _numbers_in(json.dumps(d.facts) + json.dumps(d.entitlement_inr))
        safe = {"1", "2", "3", "4", "5", "0", "24", "30", "12"}
        for value, surface in _claims_in(d.body):
            assert value in grounded or value in safe, (
                f"{d.candidate_id}: '{surface}' is not supported by the draft's facts")


def test_row_counts_are_verified_claims():
    """Regression: bare counts used to bypass grounding entirely, so a
    hallucinated '5,000 rows' passed verification."""
    claims = dict(_claims_in("the supported product limit of 5,000 rows"))
    assert "5000" in claims


def test_comma_grouped_numbers_parse_whole():
    """Regression: the count pattern matched the '000' tail of '3,000'."""
    assert _numbers_in("split into files below 3,000 rows") == {"3000"}


# ==========================================================================
# The confirmation gate still applies
# ==========================================================================

def test_outreach_requires_its_own_permission():
    assert A.REQUIRED_PERM[A.ActionType.SEND_CUSTOMER_OUTREACH].value == "action:send_outreach"


def test_preparing_outreach_sends_nothing(plan, staff):
    if not plan.drafts:
        pytest.skip("no drafts in this dataset state")
    d = plan.drafts[0]
    prop = A.propose(A.ActionType.SEND_CUSTOMER_OUTREACH,
                     {"outreach_kind": d.kind, "account_id": d.account_id,
                      "topic_hint": d.subject, "body": d.body},
                     staff, summary="test", preview={}, account_id=d.account_id)
    assert prop.status is A.Status.PENDING
    assert prop.committed_ref is None
