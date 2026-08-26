# Product Note

## Which additional client problem I chose

**Problem 2 — Trust and Reliability**, because it is the one that decides
whether the product gets used at all. A support team that catches the assistant
being confidently wrong once will check every answer afterwards, at which point
it is slower than the manual process it replaced.

I addressed it structurally rather than with disclaimers:

**The model cannot produce a number.** Every fee, credit amount, SLA target and
breach comes from `engine.py`. The agent chooses *which* calculation is
relevant; code decides what the answer is. This removes the failure mode instead
of warning about it.

**Every answer carries its working.** `decision.rule_chain` names the clause
behind each step, so *"why INR 0?"* is answered by replaying the chain — not by
asking the model to reconstruct its reasoning afterwards, which is a story about
an answer rather than the answer's cause.

**Precedence is enforced, not suggested.** A signed agreement beats the SOP even
when the SOP is the closer textual match. ORD-1001 is the case: the SOP says
INR 250 after 30 minutes, ticket TKT-450 shows an agent telling Northstar
exactly that, and the agreement waives it. The system answers INR 0 and says
which clause overrode which.

**Rejected sources are reported, not hidden.** Deprecated policies and other
customers' agreements appear in `excluded` with a reason. An agent about to
quote Support Policy v2 can see it was considered and why it lost.

**Uncertainty is a distinct outcome.** `INSUFFICIENT_DATA` is not
`NOT_ELIGIBLE`. "You are not owed this" and "I cannot tell yet" are different
answers, and SOP v4 §3 forbids promising a credit while fault is unknown.

**The question that must not be answered generically.** *"A pickup is three
hours late — do I get a credit?"* is refused. Three hours qualifies under the
SOP (2-hour threshold) and does not under the LumenWorks agreement (4 hours).
The tool requires an order id, so this is enforced in code rather than left to
the model's discretion.

**A last check on the prose.** `verify.py` confirms every figure and record id
in the final text came from a tool result. It should never fire; it exists
because "should never" is not a guarantee.

I also built a slice of **Problem 1 — Proactive Detection**: the SLA Board
computes severity, target and breach for every open ticket at once, with no
model involved, so an agent sees what is breaching before anyone asks.

## What else I would build, in priority order

1. **Aggregate credit tracking.** Northstar's agreement caps monthly credits at
   INR 5,000. The engine surfaces the cap as a caveat but cannot enforce it,
   because nothing records what has already been issued this month. This is the
   highest-value gap: it is the one place the system can currently be *right*
   about a single credit and *wrong* about the account.

2. **Complaint clustering.** Four tickets mention bulk upload. Detecting that
   they are one issue — and matching it to KI-208 — turns four investigations
   into one advisory. Highest leverage for a 20-person team.

3. **Proactive outreach with suppression.** Once clusters exist, the valuable
   half is deciding who *not* to contact: a customer who already raised a ticket,
   or one whose agreement excludes weekend cover on a Sunday.

4. **Answer-level feedback.** A thumbs-down tied to the rule chain tells you
   *which rule* was wrong, not just that an answer was. Without it, improving
   the engine is guesswork.

5. **Contract term extraction with review.** Terms are transcribed by hand
   today. At ten customers that is fine; at two hundred it is the bottleneck.
   The right shape is extract-then-confirm, never extract-and-trust.

## What I intentionally left out

- **Real ticketing integration.** Actions append to a local ledger. The
  confirmation *gate* is real; what happens after it is mocked, as the brief
  permits.
- **Authentication.** Roles are a mocked directory. Access control is enforced
  in the data layer, which is the part that would survive a real auth system
  being dropped in.
- **Token-by-token streaming.** The tool trace is the useful progress signal;
  streaming prose adds machinery without adding information.
- **Multi-turn memory beyond the session.** Conversations persist and can be
  resumed, but there is no cross-conversation profile.
- **A re-ranker over retrieval.** 25 chunks. It would be decoration.

## One metric

**Percentage of assistant answers a support agent sends without editing.**

Not accuracy on a fixed question set — that measures the eval, not the product.
Not deflection rate — a bot that answers wrongly deflects beautifully. Send-
without-editing is the only number that moves when the assistant is genuinely
doing the agent's work, and it degrades immediately if answers become subtly
wrong, because agents start rewriting them before they catch why.

The counter-metric to watch beside it: **escalations that arrive with the wrong
severity**, which is what a confidently wrong triage looks like downstream.
