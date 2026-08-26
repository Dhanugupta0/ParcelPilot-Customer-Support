"""The agent loop.

Written directly against the OpenAI-compatible tool-calling API rather than through a
framework. For a system whose whole value proposition is that its control flow
is inspectable -- who may call what, what gets filtered, what needs confirmation
-- an abstraction layer that hides the loop is working against the goal. It is
also about eighty lines.

Each turn emits a stream of events so the interface can show the tools being
chosen as they happen, streams the answer token by token, and finishes with a
trust assessment of that answer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from openai import (APIConnectionError, APITimeoutError, InternalServerError,
                    RateLimitError)

from app import config
from app.agent import trust
from app.agent.prompts import system_prompt
from app.agent.tools import CATEGORY, TOOL_SCHEMAS, call_tool
from app.core import audit
from app.core.llm import client, reasoning_params, strip_reasoning
from app.core.principal import Principal


@dataclass
class Session:
    session_id: str
    principal: Principal
    messages: list[dict] = field(default_factory=list)
    # The durable conversation this session is writing into. A session is a
    # browser tab; a conversation outlives it and can be resumed from another.
    conversation_id: str | None = None
    # What the customer said they need help with, chosen before the first
    # message. Folded into the system prompt so they do not have to re-type
    # "about my order ORD-1001" in every question.
    context: dict | None = None

    def ensure_system(self) -> None:
        if not self.messages or self.messages[0].get("role") != "system":
            self.messages.insert(0, {"role": "system",
                                     "content": system_prompt(self.principal,
                                                              self.context)})


# Transient upstream failures a hosted deployment WILL hit: rate limits under
# load, cold-start timeouts, and occasional 5xx. Failing the whole turn on the
# first one turns a two-second blip into a broken conversation, so they are
# retried with backoff. Everything else (bad request, auth) fails immediately,
# because retrying it just wastes time and money.
_TRANSIENT = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)
_MAX_RETRIES = 3

# A 429 means two very different things. "You are going too fast" is transient
# and worth backing off for. "You have no credits" is a 429 with the same class
# and will never succeed, so retrying it just adds 4.5 seconds of backoff to a
# failure the operator has to fix in a billing console.
_PERMANENT_CODES = ("insufficient_quota", "credit_balance_exhausted",
                    "billing_hard_limit_reached", "account_deactivated")

# Groq's free tier adds a third case the codes above do not cover: a DAILY token
# allowance. It arrives as a 429 like an ordinary rate limit, but the window is
# hours away, not seconds -- retrying it three times just adds minutes of delay
# to a failure only a quota reset or a plan upgrade will fix. Groq marks it
# explicitly with `x-should-retry: false`, which is the most reliable signal.
_DAILY_QUOTA_MARKERS = ("tokens per day", "requests per day", "(tpd)", "(rpd)")


def _daily_quota_exhausted(e: Exception) -> bool:
    body = str(getattr(e, "body", "") or "")
    if any(m in f"{body} {e}".lower() for m in _DAILY_QUOTA_MARKERS):
        return True
    headers = getattr(getattr(e, "response", None), "headers", None)
    return bool(headers) and str(headers.get("x-should-retry", "")).lower() == "false"


def _is_permanent(e: Exception) -> bool:
    code = str(getattr(e, "code", "") or "")
    body = str(getattr(e, "body", "") or "")
    blob = f"{code} {body} {e}".lower()
    if any(c in blob for c in _PERMANENT_CODES):
        return True
    return _daily_quota_exhausted(e)


_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?$")
# A per-minute quota needs a wait measured in tens of seconds, but a turn that
# blocks for minutes is worse than one that fails honestly, so the wait is capped.
_MAX_BACKOFF_S = 60.0


def _parse_duration(raw: str) -> float | None:
    """Seconds from either a plain number or Groq's "2h38m24s" / "577ms" form."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    m = _DURATION.match(raw)
    if not m or not any(m.groups()):
        return None
    h, mins, secs, ms = m.groups()
    return (float(h or 0) * 3600 + float(mins or 0) * 60
            + float(secs or 0) + float(ms or 0) / 1000)


def _retry_delay(e: Exception, attempt: int) -> float:
    """How long to wait before retrying, preferring what the provider tells us.

    Groq's free tier meters TOKENS PER MINUTE, so a 429 there is not the
    millisecond-scale blip that exponential backoff is designed for -- the
    window can be most of a minute away, and 1.5s then 3s simply burns both
    retries and fails a turn that would have succeeded. The response says
    exactly when the quota resets; guessing when we have been told is careless.
    """
    headers = getattr(getattr(e, "response", None), "headers", None)
    if headers:
        for name in ("retry-after", "x-ratelimit-reset-tokens",
                     "x-ratelimit-reset-requests"):
            secs = _parse_duration(str(headers.get(name) or ""))
            if secs is not None and secs > 0:
                # +0.25s so we land just after the window opens, not on its edge.
                return min(secs + 0.25, _MAX_BACKOFF_S)
    return min(1.5 * (2 ** attempt), _MAX_BACKOFF_S)


def _failure_message(e: Exception) -> str:
    """What the person in front of the screen should be told.

    An operator whose deployment has run out of credits needs to know that, not
    a generic "try again" that will never work.
    """
    if _daily_quota_exhausted(e):
        # Distinct from "no credit": the allowance comes back on its own, so the
        # operator needs to know it is a wait-or-upgrade, not a broken account.
        when = ""
        headers = getattr(getattr(e, "response", None), "headers", None)
        secs = _parse_duration(str((headers or {}).get("retry-after") or ""))
        if secs:
            when = f" It resets in about {round(secs / 60)} minute(s)."
        return ("This deployment has used up its model provider's daily token "
                f"allowance, so I cannot answer anything further today.{when} "
                "Retrying will not help until then. Please alert whoever operates "
                "this deployment — the allowance resets on its own, or the plan "
                "can be upgraded.")
    if _is_permanent(e):
        return ("This deployment's model provider has rejected the request — the "
                "API account has no remaining credit or has been deactivated. "
                "This is a configuration problem on ParcelPilot's side, not "
                "something retrying will fix. Please alert whoever operates this "
                "deployment.")
    return ("I could not reach the reasoning service just now, so I have not "
            "given you an answer rather than guess at one. Please try again — "
            "and if it keeps happening, raise this with ParcelPilot support.")


def _stream_step(session: "Session", tools: list[dict]) -> Iterator[tuple[str, Any]]:
    """Run one model step with token streaming.

    Yields ("delta", text) for each content token as it arrives, then exactly one
    ("message", assistant_message_dict) once the step is complete. The message is
    assembled here rather than taken from the SDK because a streamed response
    arrives as fragments: content in pieces, and tool calls as partial JSON that
    has to be concatenated per index before it can be parsed.

    A transient failure is only retried while nothing has been shown to the user.
    Once tokens are on screen, restarting would replay half an answer, so the
    error surfaces instead.
    """
    import time
    last: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        content_parts: list[str] = []
        refusal_parts: list[str] = []
        calls: dict[int, dict] = {}
        emitted = False
        try:
            stream = client().chat.completions.create(
                model=config.GROQ_MODEL,
                messages=session.messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,           # routing and citation, not prose style
                stream=True,
                # Keeps the model's chain of thought server-side. Without it the
                # reasoning arrives as its own delta field, and a model that runs
                # out of budget mid-thought can spill it into `content` -- which
                # is exactly the text this loop streams to the browser.
                extra_body=reasoning_params(),
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if getattr(delta, "refusal", None):
                    refusal_parts.append(delta.refusal)
                if delta.content:
                    content_parts.append(delta.content)
                    emitted = True
                    yield ("delta", delta.content)
                for tc in (delta.tool_calls or []):
                    slot = calls.setdefault(tc.index, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["function"]["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["function"]["arguments"] += tc.function.arguments
        except _TRANSIENT as e:
            last = e
            if _is_permanent(e) or emitted or attempt == _MAX_RETRIES - 1:
                break
            wait = _retry_delay(e, attempt)
            audit.record("chat.retry", session.principal, attempt=attempt + 1,
                         wait_s=wait, error=type(e).__name__)
            time.sleep(wait)
            continue

        msg: dict = {"role": "assistant"}
        if content_parts:
            msg["content"] = "".join(content_parts)
        if refusal_parts:
            msg["refusal"] = "".join(refusal_parts)
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        yield ("message", msg)
        return
    raise last                                                  # type: ignore[misc]


def _withheld_text(principal, ungrounded: list[str]) -> str:
    """What to say when the trust layer refuses to let an answer through.

    The message has to change with who is reading it. Telling a support agent
    to "escalate this to a ParcelPilot support agent" is telling them to contact
    themselves -- it reads as a bug, and it hides the one piece of information
    they can actually act on: WHICH figure could not be traced. A customer wants
    a route to a human; an agent wants the failure.
    """
    figures = ", ".join(ungrounded[:4])
    if principal.is_customer:
        return ("I am not able to give you a reliable answer here. My draft "
                "response contained figures I could not trace back to a "
                "verified source, so I would rather not state them.\n\n"
                "I would recommend escalating this to a ParcelPilot support "
                "agent, who can confirm the details directly.")
    return (
        "I have withheld my answer. The draft I produced asserted "
        + (f"a figure ({figures}) " if figures else "figures ")
        + "that does not appear in any tool result from this turn — most often "
          "that means I derived or rounded it rather than reading it, which is "
          "exactly the kind of number that should not reach a customer.\n\n"
        "You have the tool trace beside this answer: check the raw results "
        "directly, or ask me again for the specific field you need and I will "
        "quote it rather than compute it.")


def _no_answer_text(principal) -> str:
    if principal.is_customer:
        return ("I was not able to produce an answer for that. Could you rephrase "
                "it, or would you like me to escalate this to a ParcelPilot "
                "support agent?")
    return ("I did not produce an answer for that, and I would rather say so than "
            "invent one. Try rephrasing the question, or name the specific ticket, "
            "order or clause you want me to look at.")


def _step_limit_text(principal) -> str:
    if principal.is_customer:
        return ("This request needed more investigation steps than I am allowed to "
                "take in one turn. Let me hand this to a ParcelPilot support agent "
                "rather than give you a partial answer.")
    return (f"I hit my {config.MAX_AGENT_STEPS}-step limit before reaching a "
            "conclusion, so anything I said would be a partial answer. The tool "
            "trace shows how far I got — narrowing the question to one account, "
            "ticket or order will usually get there inside the limit.")


def _event(kind: str, **data) -> dict:
    return {"type": kind, **data}


def _unverified(reason: str) -> trust.TrustReport:
    """A trust report for the paths that never reached an assessment.

    Every `answer` event must be followed by a `trust` event. The two bail-out
    paths below -- an upstream failure and the step limit -- used to emit an
    answer with no assessment at all, which renders in the interface as an
    answer with no confidence strip: visually indistinguishable from one that
    passed. An unverified answer must look unverified.
    """
    return trust.TrustReport(confidence=0.0, band="LOW", reasons=[reason])


def _tools_for(p: Principal) -> list[dict]:
    """Tool availability is itself an access-control surface.

    A customer session never even sees the operations-signals tool. Removing a
    capability from the schema is stronger than declining it at call time: the
    model cannot be talked into invoking something it was never offered.
    """
    from app.core.principal import Perm
    out = []
    for schema in TOOL_SCHEMAS:
        name = schema["function"]["name"]
        if (name in ("get_operational_signals", "get_proactive_outreach")
                and not p.can(Perm.VIEW_SIGNALS)):
            continue
        out.append(schema)
    return out


def run_turn(session: Session, user_message: str) -> Iterator[dict]:
    session.ensure_system()
    session.messages.append({"role": "user", "content": user_message})
    audit.record("chat.turn", session.principal, session_id=session.session_id,
                 message=user_message[:500])

    tool_results: list[dict[str, Any]] = []
    tool_names: list[str] = []
    proposals: list[dict] = []
    tools = _tools_for(session.principal)

    for step in range(config.MAX_AGENT_STEPS):
        msg: dict = {"role": "assistant"}
        try:
            # Content tokens are forwarded to the browser the moment they arrive.
            # They are a DRAFT: the trust assessment below has not run yet, and
            # the interface marks the text unverified until it does.
            for kind, payload in _stream_step(session, tools):
                if kind == "delta":
                    yield _event("answer_delta", text=payload)
                else:
                    msg = payload
        except Exception as e:                                    # noqa: BLE001
            audit.record("chat.error", session.principal, error=str(e))
            yield _event("error", message=f"Model call failed: {e}")
            # Emit an answer as well, so a transient upstream failure surfaces as
            # a readable reply rather than an empty bubble. Silence is the worst
            # possible outcome here: the user cannot tell a refusal from a crash.
            yield _event("answer", text=_failure_message(e))
            yield _event("trust", **_unverified(
                "The reasoning service could not be reached, so nothing here has "
                "been checked against a source.").to_dict())
            yield _event("done", steps=step + 1, failed=True)
            return

        session.messages.append(msg)

        if not msg.get("tool_calls"):
            # Belt and braces: `reasoning_format=hidden` should mean the chain
            # of thought never reaches `content`, but a model that exhausts its
            # budget mid-thought can still spill an unterminated block into it.
            answer = strip_reasoning(msg.get("content") or "")
            if not answer:
                # No content, no tool calls, no refusal text. Rare, but an empty
                # chat bubble is indistinguishable from a hung request, so say
                # something honest instead of nothing.
                answer = (msg.get("refusal") or _no_answer_text(session.principal))
            # The record the user picked from the issue list counts as something
            # THEY supplied: referring to TKT-504 in a thread they opened from
            # TKT-504 is quoting, not inventing.
            stated = user_message
            if session.context and session.context.get("ref"):
                stated = f"{user_message} {session.context['ref']}"
            report = trust.assess(answer, tool_results, tool_names, stated)

            if report.should_withhold:
                # The answer asserted figures no tool produced. Withholding is
                # the correct outcome: a confidently wrong number is the exact
                # failure mode this system exists to avoid.
                answer = _withheld_text(session.principal, report.ungrounded_claims)
                audit.record("answer.withheld", session.principal,
                             ungrounded=report.ungrounded_claims)

            # Always rewrite the stored turn to the text the human was actually
            # shown. A withheld answer must not survive in the history the model
            # reads next, and a streamed step can otherwise leave a message with
            # no content at all, which the API rejects on the following call.
            session.messages[-1] = {"role": "assistant", "content": answer}

            # The final, verified text. The interface replaces whatever it
            # streamed with this, so a withheld answer is never left on screen.
            yield _event("answer", text=answer)
            yield _event("trust", **report.to_dict())
            if proposals:
                yield _event("proposals", items=proposals)
            yield _event("done", steps=step + 1)
            return

        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            yield _event("tool_start", tool=name,
                         category=CATEGORY.get(name, "Tool"), args=args,
                         call_id=tc["id"])

            result = call_tool(name, args, session.principal)
            tool_results.append(result)
            tool_names.append(name)
            if name == "propose_action" and result.get("proposal_id"):
                proposals.append(result)

            yield _event("tool_end", tool=name,
                         category=CATEGORY.get(name, "Tool"),
                         call_id=tc["id"], result=_summarise(name, result),
                         raw=result)

            session.messages.append({
                "role": "tool", "tool_call_id": tc["id"], "name": name,
                "content": json.dumps(result, default=str)[:12000],
            })

    yield _event("answer", text=_step_limit_text(session.principal))
    yield _event("trust", **_unverified(
        f"The investigation hit the {config.MAX_AGENT_STEPS}-step limit before "
        f"reaching a conclusion, so this is not a verified answer.").to_dict())
    yield _event("done", steps=config.MAX_AGENT_STEPS, truncated=True)


def _summarise(name: str, result: dict) -> str:
    """One line per tool call for the live trace panel."""
    if not isinstance(result, dict):
        return "done"
    if result.get("access_denied"):
        return "access denied by the data layer"
    if result.get("error"):
        return f"error: {str(result['error'])[:90]}"

    if name == "search_policy_documents":
        n, ex = len(result.get("results", [])), len(result.get("excluded_sources", []))
        cf = len(result.get("conflicts_detected", []))
        parts = [f"{n} passage(s)"]
        if ex:
            parts.append(f"{ex} source(s) excluded")
        if cf:
            parts.append(f"{cf} CONFLICT(S) detected")
        return ", ".join(parts)
    if name == "lookup_account":
        if result.get("accounts"):
            return f"{len(result['accounts'])} account(s) in scope"
        return (f"{result.get('account_name')} · {result.get('plan')} · "
                f"{'signed agreement found' if result.get('signed_agreement') else 'no agreement'}")
    if name == "lookup_order":
        o = result.get("order", {})
        return f"{o.get('order_id')} · {o.get('status')} · {o.get('carrier')}"
    if name == "lookup_tickets":
        return f"{result.get('count', 0)} ticket(s)"
    if name == "evaluate_policy_decision":
        d = result.get("decision")
        if d:
            amt = d.get("amount_inr")
            return (f"{d.get('outcome')}" + (f" · INR {amt:,.0f}" if amt is not None else "")
                    + f" · via {d.get('authority_used')} · {d.get('confidence')} confidence")
        s = result.get("sla", {})
        return (f"{result.get('triage', {}).get('severity')} · target {s.get('target')} · "
                + ("BREACHED" if s.get("breached") else "within target"))
    if name == "propose_action":
        return f"{result.get('proposal_id')} prepared — awaiting human confirmation"
    if name == "get_operational_signals":
        st = result.get("stats", {})
        return (f"{st.get('total_signals', 0)} signal(s) · "
                f"{st.get('sla_breached', 0)} SLA breach(es)")
    return "done"
