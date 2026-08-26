"""The tool layer: what the agent may call, and what it may not.

No API key needed. These test the tools themselves — the model's job is to
choose between them, and choosing is the only part that needs a model.
"""
from __future__ import annotations

import pytest

from app import access, actions, engine, store, text, tools, verify

pytestmark = pytest.mark.usefixtures("loaded")


@pytest.fixture(scope="session")
def loaded():
    store.load_workbook()


AGENT = access.get("agent_rohit")
MANAGER = access.get("mgr_priya")
LUMEN = access.get("cust_lumenworks")


# ==========================================================================
# The brief requires at least three distinct tools the agent chooses between.
# ==========================================================================

def test_the_agent_is_offered_four_distinct_categories():
    cats = {tools.CATEGORY[s["function"]["name"]] for s in tools.available(AGENT)}
    assert {"Document retrieval", "Structured data", "Deterministic calculation",
            "State-changing action (needs confirmation)"} <= cats


def test_figures_come_from_the_engine_not_the_model():
    """`calculate` is the only tool that returns money or deadlines."""
    r = tools.call("calculate", {"kind": "cancellation", "order_id": "ORD-1001"}, AGENT)
    d = r["decision"]
    assert d["amount_inr"] == 0.0
    assert d["rule_chain"], "a figure with no chain cannot be defended"
    assert "Northstar" in d["authority_used"]


def test_calculate_refuses_without_the_record_it_needs():
    """The clarify behaviour is enforced by the tool, not by prompt discipline.

    A credit question with no order cannot be answered: 3 hours is over the SOP
    threshold and under the LumenWorks one.
    """
    assert "error" in tools.call("calculate", {"kind": "service_credit"}, AGENT)


# ==========================================================================
# Access control lives in the tool layer
# ==========================================================================

def test_a_customer_cannot_calculate_on_another_tenants_order():
    r = tools.call("calculate", {"kind": "cancellation", "order_id": "ORD-1001"}, LUMEN)
    assert r.get("access_denied")
    assert "decision" not in r


def test_a_customer_cannot_look_up_another_tenants_order():
    assert tools.call("lookup_order", {"order_id": "ORD-1001"}, LUMEN).get("not_found")
    assert tools.call("lookup_order", {"order_id": "ORD-2001"}, LUMEN).get("order")


def test_another_customers_agreement_is_never_retrievable():
    r = tools.call("search_policies", {"query": "cancellation fee waiver"}, LUMEN)
    assert all("Northstar" not in p["citation"] for p in r["passages"])
    assert any("another customer" in e["reason"] for e in r["excluded"])


def test_a_historical_resolution_is_returned_with_a_warning():
    """TKT-450's recorded resolution is wrong; the tool says so on the way out."""
    r = tools.call("lookup_ticket", {"ticket_id": "TKT-450"}, AGENT)
    assert r["ticket"]["historical_resolution"]
    assert "CONTEXT ONLY" in r["warning"]


# ==========================================================================
# Confirmation before actions
# ==========================================================================

def test_preparing_an_action_executes_nothing():
    r = tools.call("propose_action", {"action_type": "create_escalation",
                                      "reason": "P1 SLA breached",
                                      "ticket_id": "TKT-505"}, AGENT)
    p = actions.get(r["proposal_id"])
    assert r["status"].startswith("PREPARED")
    assert p.status == "pending" and p.reference is None


def test_only_a_human_confirmation_commits():
    r = tools.call("propose_action", {"action_type": "create_escalation",
                                      "reason": "P1 breach", "ticket_id": "TKT-501"},
                   AGENT)
    p = actions.commit(r["proposal_id"], AGENT)
    assert p.status == "committed" and p.reference
    with pytest.raises(actions.Refused):
        actions.commit(r["proposal_id"], AGENT)   # not twice


def test_a_large_credit_needs_a_manager():
    """SOP v4 §3: above INR 1,000 a manager must approve.

    Checked in the action layer rather than trusted from the model, because this
    is the number that decides who may sign it off.
    """
    r = tools.call("propose_action", {
        "action_type": "issue_service_credit", "reason": "goodwill",
        "order_id": "ORD-2002", "amount_inr": 5000}, AGENT)
    assert actions.get(r["proposal_id"]).requires_manager
    with pytest.raises(actions.Refused):
        actions.commit(r["proposal_id"], AGENT)
    assert actions.commit(r["proposal_id"], MANAGER).status == "committed"


def test_a_customer_cannot_prepare_an_internal_action():
    r = tools.call("propose_action", {"action_type": "issue_service_credit",
                                      "reason": "I want one",
                                      "order_id": "ORD-2002",
                                      "amount_inr": 300}, LUMEN)
    assert r.get("refused")


def test_the_model_has_no_tool_that_executes():
    """The gate is the absence of a capability, not an instruction."""
    assert "commit" not in tools.DISPATCH
    assert not any("commit" in s["function"]["name"] or
                   "execute" in s["function"]["name"]
                   for s in tools.SCHEMAS)


# ==========================================================================
# The guard on the model's prose
# ==========================================================================

def test_a_figure_the_tools_produced_is_accepted():
    used = [{"tool": "calculate",
             "result": tools.call("calculate", {"kind": "service_credit",
                                                "order_id": "ORD-2002"}, AGENT)}]
    assert verify.check("A credit of INR 300 is due.", used) == []


def test_a_figure_the_tools_never_produced_is_reported():
    used = [{"tool": "calculate",
             "result": tools.call("calculate", {"kind": "service_credit",
                                                "order_id": "ORD-2002"}, AGENT)}]
    assert verify.check(
        "A credit of INR 4,750 is due.", used) == ["INR 4,750"]


def test_a_fabricated_record_id_is_caught_like_a_fabricated_figure():
    """An invented ticket number sends an agent chasing a record that never was."""
    used = [{"tool": "lookup_ticket",
             "result": tools.call("lookup_ticket", {"ticket_id": "TKT-505"}, AGENT)}]
    assert verify.check("See TKT-505 for detail.", used) == []
    assert verify.check("See TCK-5678 and ORD-3456.", used) == ["TCK-5678", "ORD-3456"]


def test_an_id_the_user_supplied_is_quoted_not_invented():
    assert verify.check("ORD-9999 is the one you mean.", [],
                        "what about ORD-9999?") == []


def test_customer_phrasing_keeps_the_substance_and_drops_the_code():
    warn = ("KI-211: SwiftShip pickup confirmations can arrive up to 20 minutes "
            "late. (Product Operations Guide — KI-211)")
    plain = text.plainly(warn)
    assert "20 minutes" in plain
    assert "KI-211" not in plain and "Product Operations Guide" not in plain


def test_a_figure_the_engine_stated_in_its_rule_chain_is_not_called_invented():
    """The check reported a fabrication on almost every correct answer.

    `30-minute` in a rule chain and `"overdue_minutes": 120` in a facts dict are
    both figures a tool produced, but neither has a unit word following the
    number, so neither registered as allowed. The answers quoting them were then
    flagged in the interface as figures no tool produced — which is the one
    warning that has to mean something when it appears.
    """
    used = [{"tool": "calculate",
             "result": tools.call("calculate", {"kind": "sla",
                                                "ticket_id": "TKT-505"}, AGENT)}]
    assert verify.check(
        "The 30 minute target was missed; it is 120 minutes overdue.", used) == []
    # The narrow no-break space the model writes figures with.
    assert verify.check("Overdue by 120 minutes.", used) == []
    # 2.0 h is how the engine phrases the same overrun in its headline.
    assert verify.check("It is 2.0 h past the target.", used) == []
    # And a figure that really is invented still gets caught.
    assert verify.check("It is 47 minutes overdue.", used) == ["47 minutes"]


def test_a_search_returns_the_passage_that_answers_the_question():
    """Authority was being applied as a global sort, so every contract clause in
    the pack outranked the product documentation on every query. An internal
    question with no account named returned five agreement clauses about service
    credits and never the section that answers it — and the model, given nothing
    useful, searched again until the step budget ran out.
    """
    r = tools.call("search_policies", {"query": "bulk upload limit"}, AGENT)
    cites = [p["citation"] for p in r["passages"]]
    assert any("Product Operations Guide" in c for c in cites), cites
    assert not any("Agreement" in c for c in cites), \
        "a contract clause that is not about the question must not crowd it out"


def test_authority_still_wins_between_passages_that_both_answer():
    """The rule Support Policy v3 §1 actually states, and the one worth keeping:
    where a signed agreement and the SOP both speak to the question, the
    agreement is cited first."""
    r = tools.call("search_policies", {"query": "cancellation fee window"}, LUMEN)
    cites = [p["citation"] for p in r["passages"]]
    assert "LumenWorks" in cites[0], cites
    assert any("SOP" in c for c in cites), "the SOP is still offered, just second"
