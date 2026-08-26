"""The conversation store and the vector index.

These run against a temporary database rather than `var/parcelpilot.db`, so the
suite never depends on -- or damages -- whatever demo state is on the machine.
"""
from __future__ import annotations

import importlib

import pytest

from app.core.principal import load_principal


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A fresh database per test.

    The module caches a connection in a global, so the path is patched and the
    module reloaded; otherwise every test would share the developer's own file.
    """
    from app import config
    monkeypatch.setattr(config, "VAR_DIR", tmp_path)
    import app.store.db as db
    importlib.reload(db)
    db.DB_PATH = tmp_path / "test.db"
    db._conn = None
    db.conn()
    yield db
    db._conn = None


def test_conversation_records_the_whole_turn(store, staff):
    cid = store.create_conversation(staff, context_kind="ticket", context_ref="TKT-505")
    store.add_message(cid, 1, "user", "Is TKT-505 breached?")
    store.add_tool_call(cid, 1, "evaluate_policy_decision", "Deterministic policy engine",
                        {"decision_type": "sla", "ticket_id": "TKT-505"},
                        {"sla": {"breached": True}}, "BREACHED")
    store.add_message(cid, 2, "assistant", "Yes, by 2 hours.",
                      {"confidence": 0.8, "band": "HIGH",
                       "reasons": ["engine"], "citations": ["Support Policy v3 §3"],
                       "conflicts": [], "withheld": False})

    t = store.transcript(cid)
    assert [m["role"] for m in t["messages"]] == ["user", "assistant"]
    a = t["messages"][1]
    assert a["band"] == "HIGH" and a["confidence"] == 0.8
    assert a["citations"] == ["Support Policy v3 §3"]

    # The ARGUMENTS matter as much as the result: "did it look up the right
    # ticket" is unanswerable without them.
    call = t["tool_calls"][0]
    assert call["args"] == {"decision_type": "sla", "ticket_id": "TKT-505"}
    assert call["result"]["sla"]["breached"] is True


def test_withheld_answers_are_recorded_as_withheld(store, staff):
    cid = store.create_conversation(staff)
    store.add_message(cid, 1, "user", "How many rows?")
    store.add_message(cid, 2, "assistant", "I cannot give you a reliable answer.",
                      {"confidence": 0.3, "band": "LOW", "withheld": True})
    a = store.transcript(cid)["messages"][1]
    assert a["withheld"] is True, "a reviewer must be able to see what was withheld"


def test_empty_conversations_are_hidden_from_the_review_queue(store, staff):
    empty = store.create_conversation(staff)
    used = store.create_conversation(staff)
    store.add_message(used, 1, "user", "hello")
    store.touch_conversation(used, title="hello", bump_turn=True)

    ids = [c["id"] for c in store.list_conversations()]
    assert used in ids
    assert empty not in ids, "an abandoned session is not a conversation to review"
    assert empty in [c["id"] for c in store.list_conversations(include_empty=True)]


def test_escalation_lifecycle(store, staff):
    cid = store.create_conversation(staff)
    store.record_escalation(proposal_id="PROP-1", conversation_id=cid,
                            account_id="ACCT-004", account_name="Axis Labs",
                            ticket_id="TKT-505", severity="P1", reason="P1 breach",
                            details="escalate", raised_by=staff.user_id,
                            raised_by_role=staff.role.value)
    assert store.list_escalations()[0]["status"] == "proposed"

    # Only a human confirmation makes it real, and the dashboard must reflect
    # that rather than treating a proposal as an escalation.
    store.set_escalation_status("PROP-1", "committed", "ESC-ABC123")
    e = store.list_escalations()[0]
    assert e["status"] == "committed" and e["reference"] == "ESC-ABC123"
    assert store.escalations_for(cid)[0]["id"] == "PROP-1"


def test_recording_an_escalation_twice_does_not_lose_its_reference(store, staff):
    cid = store.create_conversation(staff)
    kw = dict(proposal_id="PROP-2", conversation_id=cid, account_id="ACCT-001",
              account_name="Northstar", ticket_id="TKT-501", severity="P1",
              reason="r", details="d", raised_by=staff.user_id,
              raised_by_role=staff.role.value)
    store.record_escalation(**kw)
    store.set_escalation_status("PROP-2", "committed", "ESC-KEEP")
    store.record_escalation(**kw)          # a re-proposal must not wipe history
    assert store.list_escalations()[0]["reference"] == "ESC-KEEP"


# --------------------------------------------------------------------------
# Vectors
# --------------------------------------------------------------------------

def test_semantic_search_finds_a_paraphrase(store, monkeypatch, staff):
    """The whole justification for the vector index, as an assertion.

    A lexical index cannot connect "compensation for a missed collection" to a
    thread that says "pickup never happened" -- there is not one shared word.
    """
    import app.store.vectors as vectors
    importlib.reload(vectors)
    monkeypatch.setattr(vectors, "db", store)
    if not vectors.available():
        pytest.skip("embedding model unavailable")

    a = store.create_conversation(staff)
    store.add_message(a, 1, "user", "The pickup never happened and I want money back")
    store.touch_conversation(a, title="pickup never happened", bump_turn=True)

    b = store.create_conversation(staff)
    store.add_message(b, 1, "user", "How do I add a new user to my account?")
    store.touch_conversation(b, title="adding a teammate", bump_turn=True)

    assert vectors.index_conversation(a) and vectors.index_conversation(b)
    hits = vectors.search("compensation for a missed collection", limit=5)
    assert hits, "semantic search returned nothing"
    assert hits[0]["id"] == a, f"expected the pickup thread first, got {hits[0]['title']}"
    assert hits[0]["score"] > 0.3


def test_search_can_be_scoped_to_one_tenant(store, monkeypatch):
    import app.store.vectors as vectors
    importlib.reload(vectors)
    monkeypatch.setattr(vectors, "db", store)
    if not vectors.available():
        pytest.skip("embedding model unavailable")

    one = load_principal("cust_northstar")
    two = load_principal("cust_lumenworks")
    for p in (one, two):
        cid = store.create_conversation(p)
        store.add_message(cid, 1, "user", "bulk upload keeps failing")
        store.touch_conversation(cid, title="bulk upload keeps failing", bump_turn=True)
        vectors.index_conversation(cid)

    # The vector table has no access control of its own; isolation is the
    # caller's job and this is the assertion that it is actually being done.
    hits = vectors.search("csv import broken", account_id=one.account_id)
    assert hits, "expected a hit for the scoped account"
    assert {h["account_id"] for h in hits} == {one.account_id}
