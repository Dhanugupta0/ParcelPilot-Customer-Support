"""The agent loop, with the model stubbed.

The loop's job is mechanical: offer the tools, run what the model picks, feed
the results back, stop. None of that needs a live model, and testing it against
one would make the suite slow, flaky and dependent on a quota.

What is NOT tested here is whether the model chooses well — that is a judgement
about a model, not about this code, and it is covered by the tool-layer tests
proving that whatever it chooses, the figures come from the engine.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from app import access, agent, store

pytestmark = pytest.mark.usefixtures("loaded")


@pytest.fixture(scope="session")
def loaded():
    store.load_workbook()


AGENT = access.get("agent_rohit")
LUMEN = access.get("cust_lumenworks")


# --- a fake model that plays a fixed script ------------------------------

@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _Call:
    id: str
    function: _Fn


@dataclass
class _Msg:
    content: str | None = None
    tool_calls: list | None = None


@dataclass
class _Resp:
    choices: list


class _Script:
    """Returns the queued responses in order, recording what it was sent."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.seen: list[dict] = []

    def create(self, **kw):
        self.seen.append(kw)
        return _Resp([type("C", (), {"message": self.queue.pop(0)})()])

    # shape the SDK exposes
    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self


def _run(monkeypatch, user, question, *responses):
    script = _Script(*responses)
    monkeypatch.setattr(agent, "client", lambda: script)
    events = list(agent.run(user, question))
    return events, script


def _tool_call(name, **args):
    return _Msg(tool_calls=[_Call(id=f"c-{name}", function=_Fn(
        name=name, arguments=json.dumps(args)))])


# ==========================================================================

def test_the_loop_runs_the_tool_and_feeds_the_result_back(monkeypatch):
    events, script = _run(
        monkeypatch, AGENT, "Can Northstar cancel ORD-1001?",
        _tool_call("calculate", kind="cancellation", order_id="ORD-1001"),
        _Msg(content="No fee — the agreement waives it."))

    kinds = [e["type"] for e in events]
    assert kinds == ["tool_start", "tool_end", "answer", "done"]

    end = next(e for e in events if e["type"] == "tool_end")
    assert end["result"]["decision"]["amount_inr"] == 0.0

    # The second request must carry the tool's result, or the model is answering
    # from memory of its own question.
    second = script.seen[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert "ALLOWED_NO_FEE" in second[-1]["content"]


def test_the_answer_is_whatever_the_model_finally_says(monkeypatch):
    events, _ = _run(monkeypatch, AGENT, "hello",
                     _Msg(content="Hello — what can I look up for you?"))
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["text"] == "Hello — what can I look up for you?"


def test_a_customer_is_never_offered_the_internal_tool(monkeypatch):
    _, script = _run(monkeypatch, LUMEN, "what is happening?",
                     _Msg(content="ok"))
    offered = {t["function"]["name"] for t in script.seen[0]["tools"]}
    assert "list_tickets" not in offered
    assert "calculate" in offered, "a customer still gets real figures"


def test_the_loop_stops_at_the_step_limit(monkeypatch):
    """A model that keeps calling tools must not loop forever."""
    events, _ = _run(monkeypatch, AGENT, "go",
                     *[_tool_call("lookup_order", order_id="ORD-1001")
                       for _ in range(agent.MAX_STEPS)])
    done = next(e for e in events if e["type"] == "done")
    assert done["truncated"] is True
    assert done["steps"] == agent.MAX_STEPS


def test_an_outage_still_reports_what_was_already_computed(monkeypatch):
    """The payoff for keeping arithmetic out of the model.

    The calculation succeeded; only the sentence-writing failed. The figure is
    still correct, so it is shown rather than replaced with an apology.
    """
    class _Dies:
        def __init__(self):
            self.n = 0
        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                return _Resp([type("C", (), {"message": _tool_call(
                    "calculate", kind="service_credit", order_id="ORD-2002")})()])
            raise RuntimeError("upstream is down")
        chat = property(lambda self: self)
        completions = property(lambda self: self)

    monkeypatch.setattr(agent, "client", _Dies)
    monkeypatch.setattr(agent, "daily_quota_spent", lambda e: True)  # no retries
    events = list(agent.run(AGENT, "Is a credit due on ORD-2002?"))
    answer = next(e for e in events if e["type"] == "answer")
    assert "INR 300" in answer["text"]


def test_a_customer_outage_message_carries_no_internal_codes(monkeypatch):
    class _Dies:
        def __init__(self):
            self.n = 0
        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                return _Resp([type("C", (), {"message": _tool_call(
                    "calculate", kind="cancellation", order_id="ORD-2001")})()])
            raise RuntimeError("down")
        chat = property(lambda self: self)
        completions = property(lambda self: self)

    monkeypatch.setattr(agent, "client", _Dies)
    monkeypatch.setattr(agent, "daily_quota_spent", lambda e: True)
    events = list(agent.run(LUMEN, "Can I cancel ORD-2001?"))
    text = next(e for e in events if e["type"] == "answer")["text"]
    assert "KI-" not in text
    assert "Product Operations Guide" not in text


def test_the_picked_subject_is_passed_as_a_default_not_a_fact(monkeypatch):
    """A UI click is a routing hint, not a verified record.

    The note tells the agent what the conversation is about AND that it must
    still look the record up — because a subject that arrived from a button is
    not evidence about the order.
    """
    _, plain = _run(monkeypatch, LUMEN, "why is this happening?", _Msg(content="ok"))
    assert "[The user opened this conversation" not in \
        plain.seen[0]["messages"][-1]["content"]

    script = _Script(_Msg(content="ok"))
    monkeypatch.setattr(agent, "client", lambda: script)
    list(agent.run(LUMEN, "why is this happening?", subject={"ref": "TKT-502"}))
    sent = script.seen[0]["messages"][-1]["content"]
    assert "TKT-502" in sent
    assert "Look it up before stating anything" in sent


# ==========================================================================
# The loop must not spend its budget repeating itself
# ==========================================================================

def test_a_repeated_tool_call_is_served_from_the_first_result(monkeypatch):
    """The failure this guards: six identical `search_policies` calls, the whole
    step budget spent, and the turn ending with no answer at all."""
    from app import tools as tools_mod

    calls: list[str] = []
    real = tools_mod.call

    def counting(name, args, user):
        calls.append(name)
        return real(name, args, user)

    monkeypatch.setattr(tools_mod, "call", counting)

    events, _ = _run(
        monkeypatch, AGENT, "what is the bulk upload limit?",
        _tool_call("search_policies", query="bulk upload limit"),
        _tool_call("search_policies", query="bulk upload limit"),
        _Msg(content="5,000 rows per CSV."))

    assert calls == ["search_policies"], "the second call must not re-run the tool"

    ends = [e for e in events if e["type"] == "tool_end"]
    assert len(ends) == 2, "the repeat is still shown in the trace"
    assert "repeated_call" not in ends[0]["result"]
    assert "repeated_call" in ends[1]["result"], \
        "the model has to be told it is repeating itself in order to stop"


def test_the_same_tool_with_different_arguments_is_not_a_repeat(monkeypatch):
    from app import tools as tools_mod

    calls: list[dict] = []
    real = tools_mod.call

    def counting(name, args, user):
        calls.append(args)
        return real(name, args, user)

    monkeypatch.setattr(tools_mod, "call", counting)

    _run(monkeypatch, AGENT, "compare them",
         _tool_call("lookup_order", order_id="ORD-1001"),
         _tool_call("lookup_order", order_id="ORD-1002"),
         _Msg(content="Here they are."))

    assert len(calls) == 2, "a different order id is a different question"


def test_the_final_step_is_spent_writing_rather_than_searching(monkeypatch):
    """Leaving the turn with no answer is worse than answering from what is
    already gathered, so the last step is not offered the tools."""
    events, script = _run(
        monkeypatch, AGENT, "go",
        *[_tool_call("lookup_order", order_id=f"ORD-100{i}")
          for i in range(agent.MAX_STEPS - 1)],
        _Msg(content="Here is what I found."))

    choices = [kw.get("tool_choice") for kw in script.seen]
    assert choices[:-1] == ["auto"] * (agent.MAX_STEPS - 1)
    assert choices[-1] == "none"

    answer = next(e for e in events if e["type"] == "answer")
    assert answer["text"] == "Here is what I found."
    assert not next(e for e in events if e["type"] == "done").get("truncated")
