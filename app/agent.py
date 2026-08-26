"""The agent loop: the model chooses tools, the tools decide the answer.

Roughly ninety lines and written directly against the API rather than through a
framework. For a system whose whole claim is that its control flow is
inspectable — who may call what, what is filtered, what needs confirmation — an
abstraction that hides the loop works against the goal.

The division of labour is the point:

    the MODEL decides   which order, which ticket, which calculation, what to say
    the TOOLS decide    what the answer is

So the model still never computes a fee, a credit or a deadline. It picks
`calculate(kind='cancellation', order_id='ORD-1001')` and `engine.py` returns
INR 0 with the clause that made it zero. Asking the model to route is safe;
asking it to do arithmetic is not.

Each turn emits events so the interface can show the tools as they are chosen.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

from app import config, text, tools
from app.access import User
from app.llm import client, daily_quota_spent, retry_delay

# `.env` may raise or lower this; it was being declared there and ignored.
MAX_STEPS = config.MAX_AGENT_STEPS

INTERNAL_PROMPT = """You are ParcelPilot's support assistant, working with an
authorised {role}. Reference time is {now} — treat it as "now" for every
time-based question. Currency is INR.

SOURCE PRECEDENCE (Support Policy v3 §1)
  1. the customer's signed agreement
  2. the current support policy and SOPs
  3. current product documentation
  4. historical tickets and internal notes — CONTEXT ONLY, and the dataset
     warns some are WRONG. Never present a past resolution as the rule.
A deprecated document is never an authority, whatever it says.

HOW TO WORK
- Any question about a cancellation fee, a service credit, an SLA target or a
  breach MUST go through `calculate`. Reading a clause is not applying it: the
  engine resolves contract-versus-policy precedence, runs the business-hours
  clock and knows the approval threshold. Never do the arithmetic yourself.
- Never state a figure that did not come from a tool result. Do not round,
  divide, convert or take a percentage of one. If you need a number you were
  not given, call a tool for it.
- State only what a tool actually returned. Do not add supporting detail you
  were not given: if no tool told you whether a pickup window is open, whether a
  driver has been assigned, or what a customer was previously told, do not say.
  A correct amount wrapped in an invented explanation is still a wrong answer,
  and it is the explanation the customer will repeat back to you.
- Cite the clause you relied on, in the form the tools give you.
- When a signed agreement overrode the default policy, say so explicitly. That
  is usually the single most useful sentence in the reply.
- When a historical ticket contradicts the engine, say the past answer was
  wrong and name the clause that governs.
- If the documents do not cover something, say so and offer to escalate. Never
  invent process, prices or steps.
- Be direct and technical. Lead with the answer, then the reason, then caveats.

ACTIONS
You cannot execute anything. `propose_action` only PREPARES something for a
human to confirm in the interface. Never say an action has been taken; describe
what will happen and ask them to press Confirm.

ESCALATE when the request needs human judgment, an exception to policy, or an
action you have no tool for."""

CUSTOMER_PROMPT = """You are ParcelPilot's support assistant, talking to {name}
of account {account_id}. Reference time is {now}. Currency is INR.

You can only see this customer's own data. If something is not visible to you,
say you cannot see it and offer to bring in a support agent — never speculate
about why, and never mention other customers or internal systems.

HOW TO WORK
- Any question about a cancellation fee, a service credit or a response time
  MUST go through `calculate`. Never work a figure out yourself, and never
  state one that did not come from a tool result.
- State only what a tool actually returned. Do not add supporting detail you
  were not given: if no tool told you whether a pickup window is open, whether a
  driver has been assigned, or what a customer was previously told, do not say.
  A correct amount wrapped in an invented explanation is still a wrong answer,
  and it is the explanation the customer will repeat back to you.
- Their signed agreement is checked before the general policy, because it
  usually overrides it. Where it gives them something better, tell them.
- If the documents do not cover their question, say so plainly and offer to
  escalate to a person.

STYLE
- Warm, plain English. Lead with the answer.
- No internal vocabulary: no severity codes, no clause numbers, no issue
  numbers like KI-211, no document names, no tool names. Say "our records show"
  rather than naming a system.
- Where the engine gives you a caveat, include it in ordinary words. It is the
  honest part of the answer.
- Short. Three or four sentences unless they asked for detail."""


def system_prompt(user: User, now: str) -> str:
    if user.is_customer:
        return CUSTOMER_PROMPT.format(name=user.name, account_id=user.account_id,
                                      now=now)
    return INTERNAL_PROMPT.format(role=user.role, now=now)


def _event(kind: str, **data) -> dict:
    return {"type": kind, **data}


def run(user: User, question: str, history: list[dict] | None = None,
        subject: dict | None = None) -> Iterator[dict]:
    """One turn. Yields tool events, then the answer."""
    from app import store

    now = f"{store.snapshot():%Y-%m-%d %H:%M} IST"
    messages: list[dict] = [{"role": "system", "content": system_prompt(user, now)}]
    for h in (history or [])[-8:]:
        messages.append({"role": h["role"], "content": h["content"]})

    ask = question
    if subject and subject.get("ref"):
        # The record they picked in the UI. A default, not an answer -- the
        # agent must still look it up.
        ask = (f"{question}\n\n[The user opened this conversation about "
               f"{subject['ref']}. Treat it as the subject unless they name "
               f"another record. Look it up before stating anything about it.]")
    messages.append({"role": "user", "content": ask})

    schemas = tools.available(user)
    used: list[dict] = []
    proposals: list[dict] = []
    seen: dict[str, dict] = {}          # a tool call it has already made

    for step in range(MAX_STEPS):
        # The last step is for writing. Offering tools on it invites the model
        # to spend the budget searching and leave the turn with no answer at
        # all, which is worse than an answer drawn from what it already has.
        last_step = step == MAX_STEPS - 1
        resp = None
        last: Exception | None = None
        # A loop of several calls with large tool schemas hits the per-minute
        # token cap easily. That clears in seconds and is worth waiting for; a
        # spent daily allowance never clears and is not.
        for attempt in range(3):
            try:
                resp = client().chat.completions.create(
                    model=config.GROQ_MODEL, messages=messages,
                    tools=schemas,
                    tool_choice="none" if last_step else "auto",
                    temperature=0.1, max_tokens=900,
                    extra_body={"reasoning_format": "hidden",
                                "reasoning_effort": config.GROQ_REASONING_EFFORT})
                break
            except Exception as e:                                # noqa: BLE001
                last = e
                if daily_quota_spent(e) or attempt == 2:
                    break
                wait = retry_delay(e, attempt)
                yield _event("waiting", seconds=round(wait, 1),
                             reason="rate limited upstream")
                time.sleep(wait)
        if resp is None:
            yield _event("error", message=str(last)[:300])
            yield _event("answer", text=_unreachable(user, used))
            yield _event("done", steps=step, failed=True)
            return

        msg = resp.choices[0].message
        calls = msg.tool_calls or []

        if not calls:
            answer = (msg.content or "").strip() or (
                "I was not able to put an answer together for that. Could you "
                "rephrase it?")
            yield _event("answer", text=answer)
            if proposals:
                yield _event("proposals", items=proposals)
            yield _event("done", steps=step + 1, tools=used)
            return

        messages.append({"role": "assistant",
                         "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name,
                                                      "arguments": c.function.arguments}}
                                        for c in calls]})

        for c in calls:
            name = c.function.name
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            yield _event("tool_start", tool=name, call_id=c.id, args=args,
                         category=tools.CATEGORY.get(name, "Tool"),
                         friendly=tools.FRIENDLY.get(name, "Working"))

            # A tool called twice with the same arguments returns the same
            # thing, so the second call buys nothing and costs a step. The
            # model used to sit in exactly this loop -- six identical
            # `search_policies` calls, budget gone, no answer -- so the repeat
            # is served from the first result and named as a repeat, which is
            # the fact it needs in order to stop.
            key = f"{name}({json.dumps(args, sort_keys=True, default=str)})"
            if key in seen:
                result = dict(seen[key])
                result["repeated_call"] = (
                    f"You already called {key} this turn and this is the same "
                    f"result. Calling it again will not return anything new. "
                    f"Answer from what you have, or try a different tool or a "
                    f"materially different query.")
            else:
                result = tools.call(name, args, user)
                seen[key] = result
            summary = tools.summarise(name, result)
            used.append({"tool": name, "args": args, "summary": summary,
                         "category": tools.CATEGORY.get(name, "Tool"),
                         "result": result})
            if name == "propose_action" and result.get("proposal_id"):
                proposals.append(result)

            yield _event("tool_end", tool=name, call_id=c.id, summary=summary,
                         category=tools.CATEGORY.get(name, "Tool"),
                         friendly=tools.FRIENDLY.get(name, "Working"),
                         result=result)
            messages.append({"role": "tool", "tool_call_id": c.id, "name": name,
                             "content": json.dumps(result, default=str)[:9000]})

    # Out of steps. Everything computed so far is still true, so show it rather
    # than throwing the turn away.
    yield _event("answer", text=_out_of_steps(user, used))
    yield _event("done", steps=MAX_STEPS, truncated=True, tools=used)


def _decisions_from(used: list[dict]) -> list[dict]:
    return [u["result"]["decision"] for u in used
            if u["tool"] == "calculate" and "decision" in u.get("result", {})]


def _unreachable(user: User, used: list[dict]) -> str:
    """The model is down, but the tools already ran.

    Anything `calculate` returned is still correct, so it is shown rather than
    replaced with an apology. This is the payoff for keeping the arithmetic out
    of the model.
    """
    decs = _decisions_from(used)
    if not decs:
        return ("I could not reach the writing service just now, and I have "
                "nothing computed to show you yet. Please try again shortly.")
    lines = ["I could not reach the writing service, so here is the computed "
             "decision directly — the figures are unaffected:"]
    for d in decs:
        lines.append("")
        lines.append(d["headline"])
        if d["authority_used"] != "default policy" and user.is_internal:
            lines.append(f"(via {d['authority_used']})")
        lines.extend(d.get("caveats", []) if user.is_internal
                     else [text.plainly(c) for c in d.get("caveats", [])])
    return "\n".join(lines)


def _out_of_steps(user: User, used: list[dict]) -> str:
    decs = _decisions_from(used)
    if decs:
        return ("I ran out of investigation steps before finishing, but this much "
                "is settled:\n\n" + "\n".join(d["headline"] for d in decs))
    return ("This needed more steps than I am allowed in one turn. Narrowing it to "
            "one order or ticket will usually get there.")
