# What I would build next

Prioritised by expected impact on adoption, which for a support system means:
does the team trust it enough to stop double-checking it?

---

## Tier 1 — Required before this touches a real customer

### 1. Contract extraction with human sign-off
**Why it matters most.** The entire correctness story rests on the terms sheet
being right. Today it is rule-based extraction over two agreements with a known
structure. Real contracts are messier: amendments, side letters, clauses that
qualify each other, terms that expire mid-month.

**What I would build:** LLM-assisted extraction producing the same
`ContractTerms` artefact, routed through a review queue where a human approves
each extracted clause against the highlighted source text before it goes live.
Terms sheets versioned per amendment with effective-date ranges, so a question
about a shipment from March resolves against the terms in force in March. The
engine already reads a structured artefact, so this is a swap of one function,
not a redesign.

**Second-order benefit:** an extraction diff on contract renewal ("this renewal
changes your P2 target from 1 hour to 2 hours") is a genuinely useful product in
its own right.

### 2. First-response timestamps and a real SLA clock
The dataset has no agent-response time, so elapsed time is measured to the
snapshot. That is the right proxy for an open ticket and wrong for everything
else. With real ticketing data I would track first-response, next-response and
resolution clocks separately, support pause states (waiting on customer), and
drive the board from live events rather than a recomputed snapshot.

### 3. Evaluation as a release gate
The 53-test suite is the seed. What is missing is regression coverage that grows
automatically: every escalation a human corrects becomes a test case. I would add
a golden set of a few hundred real questions with expected outcomes, run it on
every deploy, and block release on any regression in the entitlement-decision
subset. Also adversarial suites — prompt injection through ticket text and
attachments — run on a schedule, not once.

### 4. Confidence calibration against outcomes
Confidence is currently derived from sensible heuristics, but the weights are
hand-tuned. Once there is feedback data (was the answer accepted, corrected, or
escalated?), I would fit the thresholds so that "HIGH confidence" empirically
means a measured accuracy, and publish that number to the support team. A
confidence score nobody has validated is decoration.

---

## Tier 2 — Makes the product materially better

### 5. Resolution memory that is safe to learn from
The pack demonstrates the danger of learning from past tickets: two of them are
wrong. But discarding history entirely wastes the most valuable asset a support
team has. I would build a curated knowledge layer where a resolution is promoted
to reusable guidance only after an agent marks it correct AND it is checked
against current policy — with automatic demotion when the underlying policy
changes. Essentially: the conflict detector, run continuously over the archive
rather than per query.

### 6. Proactive outreach
The Signal Board already identifies uncollected shipments accruing delay and
clusters mapped to known workarounds. The obvious next step is acting on them:
draft a message to every affected customer, batched for one human approval, with
the credit already computed. Contacting a customer before they complain is the
highest-leverage thing in support, and the system has all the inputs.

### 7. Agent-assist inside the existing helpdesk
Support teams do not want another tab. A sidebar in the existing ticketing tool —
suggested reply, cited clauses, computed entitlement, one-click escalation with
the reasoning attached — gets adoption that a separate chat interface will not.
The API layer is already the right shape for this.

### 8. Deflection measurement
Track which questions the customer-facing assistant answers versus which escalate,
and why. That number is the business case, and it also points at exactly which
documentation gaps to fill — TKT-503 ("how do I change the billing contact?") is
unanswerable today purely because nothing documents it.

---

## Tier 3 — Scale and operational maturity

### 9. Retrieval that grows with the corpus
Six documents today. At a few hundred, lexical retrieval starts missing
paraphrases and the swap to a hybrid embedding index becomes worthwhile — kept
behind the existing `Retriever` interface, with the governance layer unchanged
because it operates on metadata rather than on scores.

### 10. Multi-tenant hardening
Sessions in Redis, per-tenant rate limits, encrypted contract storage, and
tenant-scoped audit export so a customer can be shown every decision made about
their account. For a B2B logistics platform this is table stakes at contract
renewal.

### 11. Policy change simulation
Before publishing Support Policy v4, show which open tickets change severity,
which SLAs move, and which customers are affected. The engine is deterministic,
so this is just running it twice — and it turns a policy revision from a leap of
faith into a reviewed change.

### 12. Cost and latency controls
Cheaper models for routing and classification, expensive ones only for final
composition; cached retrieval for repeated questions; a hard step budget per
turn. At hundreds of requests per week the current design is inexpensive, but the
controls should exist before they are needed.

---

## What I would deliberately not build

**Autonomous action.** Every state change stays behind human confirmation until
there is measured evidence the system is right often enough to earn a narrower
gate — and even then, only for low-blast-radius actions like tagging, never
credits.

**A general-purpose chatbot.** The value here is that the system knows what it
does not know. Widening scope to "ask us anything" dilutes exactly that.

**Learning directly from past resolutions without curation.** The data pack makes
the argument better than I can.
