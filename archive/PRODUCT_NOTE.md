# Product note

The architecture note is `README.md`. This is the product half of the
submission: what I chose to solve, what I would build next, what I deliberately
did not build, and how I would tell whether any of it is working.

---

## Which additional client problem I chose

**Both**, because in a support system they are the same problem seen from two
sides. Proactive detection is worthless if the team does not trust what it
surfaces, and trust is cheap to claim until the system volunteers something
nobody asked it for.

### Problem 1 — Proactive issue detection

Five deterministic detectors run over every open ticket and order at the dataset
snapshot, with no question asked of them (`app/signals/detectors.py`):

| Detector | What it catches |
|---|---|
| SLA radar | Every open ticket auto-triaged, resolved against the *correct* target (contract or policy) on the *correct* clock, ranked by overrun |
| Complaint clusters | Single-link agglomeration over ticket text, with a rising-rate test against the preceding fortnight |
| Known-issue mapping | Clusters matched to documented issues, so a workaround can be sent instead of rediscovered per ticket |
| Cross-account impact | One problem hitting several customers at once |
| Order anomalies | Uncollected shipments accruing delay, carrier fault concentration |

The part I care about most is the clock. Targets in this data pack are written in
two different units — `15 minutes, 24x7` and `4 business hours` — and the
snapshot is a **Sunday**. A wall-clock implementation reports the wrong tickets
as on fire, which is worse than reporting none: it trains the team to ignore the
board. `app/core/clock.py` resolves each target in its own unit.

A board that only observes is still a report. **Proactive Outreach**
(`app/outreach/engine.py`) turns each signal into a specific customer,
a specific message, and a specific entitlement — and then, just as importantly,
suppresses the ones that should not be sent: outside contractual support hours,
already raised by the customer, recently contacted. The suppressed list is shown
in the interface, because *why we did not contact someone* is the part a support
manager needs to audit.

Nothing sends. Every draft goes through the same two-phase confirmation gate as
everything else, one at a time. "Approve all" prepares; it does not approve.

### Problem 2 — Trust and reliability

Four mechanisms, none of which is a prompt instruction:

1. **Source precedence in the retriever, not the model.** Deprecated documents
   are excluded before ranking. Historical ticket resolutions are admitted only
   as tier-4 context and can never outrank a contract. `retrieval/governed.py`
2. **Conflict detection on the decision path, not just the search path.** When a
   past resolution contradicts the governing source, the answer is *required* to
   name the overruled source and say it was wrong — an agent about to repeat a
   past answer is exactly who needs that warning. It runs on the decision path
   too, so the guarantee does not depend on the model choosing to search first.
3. **Deterministic decisions.** Fees, credits, SLA targets and business-hours
   arithmetic are computed by `policy/engine.py`. The model routes and narrates;
   it never calculates. The system prompt now makes this routing mandatory for
   every cancellation, credit and SLA question, including ones where a retrieved
   clause looks conclusive on its own — reading a clause is not the same as
   applying it.
4. **Abstention.** `agent/trust.py` checks the drafted answer's figures against
   what the tools actually returned. An answer asserting numbers no tool produced
   is withheld and replaced with an escalation offer. This is why streamed text
   is rendered as an unverified draft until the assessment has run.

---

## What I would build next

`NEXT.md` has the full prioritised list with reasoning. The three that matter
most, in order:

1. **Contract extraction with human sign-off.** The whole correctness story rests
   on the terms sheet being right, and today that is rule-based extraction over
   two well-structured agreements. Real contracts have amendments and
   effective-date ranges. The engine already reads a structured artefact, so this
   is a swap of one function rather than a redesign.
2. **Evaluation as a release gate.** A golden set that must pass before any
   prompt, model or rule change ships. Without it, every improvement is a guess.
3. **Confidence calibration against outcomes.** The trust bands are currently
   hand-tuned. They should be fitted to whether HIGH-confidence answers actually
   survive review, or the band is decoration.

---

## What I intentionally left out

Each of these is a deliberate trade, not an oversight.

| Left out | Why | What it would cost to add |
|---|---|---|
| A database | Sessions live in-process; proposals and the audit log are files under `var/`. Nothing here needs more, and a schema would have been time spent away from the decision logic. | Postgres for proposals + audit; Redis for sessions. Half a day. |
| A real identity provider | Auth is mocked as a directory (`core/principal.py`). The assessment permits this, and the part that matters — that the `Principal` is bound server-side and is a required argument on every data call — is real. | OIDC in front of `load_principal`. The rest is unchanged. |
| Real ticketing / email integration | Committed actions append to `var/executed_actions.jsonl` with a reference. The confirmation gate, permission re-check at the write, expiry and idempotency are all real; only the outbound call is mocked. | An adapter behind `_execute`. |
| Vector embeddings | BM25 + TF-IDF over ~40 clause-level chunks. At this corpus size embeddings would add a cold start and another hosted dependency for recall I could not measure as better. | Justified when the corpus grows past a few hundred documents. |
| LLM contract extraction | Rule-based, so it is testable and cannot hallucinate a clause. | See "what I would build next", item 1. |
| Horizontal scale-out | In-process sessions mean one instance. | Sessions to Redis first. |

---

## One metric

**Seven-day durable self-resolution rate**: the share of customer conversations
that were answered without escalation *and* did not produce a new ticket on the
same issue within seven days.

One number, chosen because it is hard to game in either direction:

- Escalating everything protects accuracy but shrinks the numerator, so caution
  is not free.
- Answering everything confidently inflates the numerator, but wrong answers come
  back as new tickets inside the window and remove themselves.

It is also the number the business already cares about, which means it does not
need a separate dashboard to be believed. I would track **withheld-answer rate**
and **conflict-detection hit rate** alongside it as diagnostics — but as
diagnostics, not as the goal. Optimising a proxy for trust is how you end up with
a system that hedges everything and helps nobody.

---

## AI tool usage

I used **Claude Code** (Anthropic's CLI agent) throughout, in three distinct
ways, and the split matters:

- **Where I let it drive:** the mechanical surface. Front-end rendering,
  the SSE event plumbing, docstrings, the test scaffolding, and repetitive
  refactors like threading `Principal` through every repository method.
- **Where I drove and used it as a reviewer:** the parts that decide whether an
  answer is right — `policy/engine.py`, `core/clock.py`, `retrieval/governed.py`
  and `agent/trust.py`. I specified the behaviour and the traps (two SLA units,
  a Sunday snapshot, contract-over-policy precedence, wrong historical
  resolutions), wrote the tests first, and used the model to argue against my
  implementation rather than to produce it.
- **Where I did not use it at all:** deciding what the system should refuse to
  do. Abstention thresholds, what needs manager approval, what is suppressed from
  outreach, and where the confirmation gate sits are product judgments, and
  handing those to a model would have defeated the point of the exercise.

I also used it as a data-pack reader — asking it to find contradictions between
the six documents and the workbook — which is how the deprecated-policy and
wrong-ticket-resolution traps ended up as explicit test cases rather than
surprises.
