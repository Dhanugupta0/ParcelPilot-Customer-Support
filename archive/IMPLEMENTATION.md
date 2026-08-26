# Implementation guide

How this system was actually built, in the order it was built, and why each
layer exists. If you need to explain or extend it, read this top to bottom.

---

## The one idea everything follows from

> **The language model routes and explains. It never decides anything that has a
> number, an entitlement, or a side effect attached.**

Every layer below is a consequence of that sentence. If you only remember one
thing for the video, remember this one.

---

## Build order

The layers are deliberately ordered so each one is testable before the next is
written. Nothing above depends on a model being available.

```
 1. Clock            ← everything time-based
 2. Identity         ← every data access
 3. Repository       ← the only door to the workbook
 4. Ingest           ← documents → chunks with authority metadata
 5. Contract terms   ← agreements → machine-readable, cited terms
 6. Policy rules     ← defaults parsed from the PDFs
 7. Policy engine    ← deterministic decisions          ◄ the core
 8. Triage           ← explainable severity
 9. Retrieval        ← relevance
10. Governance       ← authority, scoping, conflicts
11. Actions          ← two-phase confirmation
12. Tools            ← what the model may call
13. Agent loop       ← the only place a model appears
14. Trust            ← grounding + derived confidence
15. Signals          ← proactive detection
16. API + UI
```

---

### 1. Clock — `app/core/clock.py`

**Problem.** The workbook README pins the reference time to `2026-08-16 11:00
IST`. That date is a **Sunday**. And the pack writes SLA targets in two units:
`"15 minutes, 24x7"` and `"4 business hours"`.

**Solution.** A snapshot clock (never `datetime.now()`) plus a business-hours
calendar. `parse_sla_target()` reads the unit and decides which clock the target
runs on. `24x7` targets run through the weekend; business-hour targets have not
started at the snapshot.

**Why it's first.** Get this wrong and every SLA answer downstream is wrong.

---

### 2. Identity — `app/core/principal.py`

A `Principal` (user, role, account binding, permission set) is created
server-side from a mocked directory. It is a **required argument** on every data
call. The browser never sends a role the server trusts.

Three roles: `customer` (bound to one account), `support_agent`,
`support_manager` (adds credit approval, per SOP v4 §3).

---

### 3. Repository — `app/data/repository.py`

The only code that opens the `.xlsx`. Every accessor calls
`principal.assert_account_access()` before returning.

**Subtlety that matters:** an unauthorised lookup and a genuinely missing record
return the **identical** message. Otherwise a customer can enumerate other
tenants' order IDs by watching which error comes back. The true reason is kept in
`internal_reason` for the audit log only.

---

### 4. Ingest — `app/ingest/corpus.py`

PDFs → section chunks, each tagged with:

| Field | Purpose |
|---|---|
| `authority_tier` | 1 contract · 2 policy/SOP · 3 product docs · 4 context-only |
| `status` | CURRENT / DEPRECATED / RESOLVED / MONITORING |
| `scoped_account_id` | set on agreements → enforces contract scoping |
| `citation` | e.g. `Support Policy v3 §3` |

The tier hierarchy is not invented — Support Policy v3 §1 states it. Metadata is
**derived from the document text** (`Status:`, `Effective:`, `Account:`), so
dropping in another agreement works with no code change.

Known issues (`KI-xxx`) get their own chunks, with status read from the section
they sit in — KI-211 is *Monitoring*, not resolved, and mislabelling it would
demote the one document TKT-504 needs.

---

### 5. Contract terms — `app/ingest/contract_terms.py`

Agreements are parsed **once** into a `ContractTerms` object. Every extracted
value carries its citation and the verbatim quote it came from.

```
ACCT-001  cancellation_fee_waived = True   ← "…with no cancellation fee, regardless…"
ACCT-002  cancellation_fee_waived = False  ← "No special cancellation-fee waiver applies."
ACCT-002  credit_threshold_hours  = 4.0    ← "…more than 4 hours past the end…"
ACCT-002  credit_fixed_amount_inr = 300.0  ← "…a fixed INR 300 service credit."
```

**Why not let the model read the contract?** Because that is unreproducible,
unauditable, and one misread clause becomes a confidently invented number.

---

### 6. Policy rules — `app/policy/rules.py`

Defaults are **parsed from the PDFs at boot**, not hard-coded: the 30-minute
window, the ₹250 fee, the 2-hour threshold, the ₹500/10% formula, the ₹1,000
approval line, and the whole plan × severity SLA table (via
`PyMuPDF.find_tables()`).

`validate()` refuses to start if anything is missing. A silent parse failure that
falls back to a guessed default is the most dangerous failure mode here.

Deprecated documents are excluded **at this layer**, so v2's "Enterprise P1 = 1
hour" can never reach the engine.

---

### 7. Policy engine — `app/policy/engine.py` ◄ the core

Three decisions, all deterministic:

```python
decide_cancellation(order, account)  → Decision
decide_service_credit(order, account) → Decision
resolve_sla(account, severity, created_at) → SLAStatus
```

Each returns a `Decision` containing:

- `outcome` — enum, never prose
- `amount_inr` — computed, never generated
- `rule_chain[]` — every step, with the clause it applied and a quote
- `overrides_applied[]` — where a contract displaced a default
- `caveats[]` — reasons to verify before acting (distinct from the decision)
- `confidence` — HIGH / MEDIUM / LOW
- `missing_data[]` — triggers abstention

**Precedence is always: contract clause → SOP default.**

**The caveat mechanism** deserves a mention. A rule outcome can be certain while
the data is not. ORD-1001: the contract answer is unambiguous, but it is a
SwiftShip order inside its pickup window and KI-211 says confirmations lag 20
minutes — so `BOOKED` may be stale, and cancelling could mean cancelling an
already-collected parcel.

---

### 8. Triage — `app/policy/triage.py`

Rule-based severity classification, with patterns anchored to the wording of the
policy's own severity definitions. Each match reports which definition phrase it
maps to.

**The hard case:** TKT-501 says *"Every user gets HTTP 500 when creating any
shipment. Existing shipments can still be viewed."* A naive workaround detector
sees "can still" and demotes a P1 to P2 — which, on the Northstar agreement, is
the difference between a 15-minute and a 1-hour target. So a workaround only
counts if its sentence also names the **failing action**. Viewing is not a
workaround for creating.

---

### 9–10. Retrieval — `app/retrieval/`

**`index.py`** — BM25 + character n-gram TF-IDF, fused by reciprocal rank.
No vector DB: 26 chunks of dense policy text does not justify a 500 MB torch
dependency. `Retriever` is a swappable interface.

> **Ranking is relevance only.** An earlier version boosted contracts in the
> score and, as a result, could not retrieve the support-policy SLA table at all.
> Authority answers *"which source wins"*, not *"which source is about the thing
> you asked"*.

**`governed.py`** — three guarantees:

1. **Scope** — another tenant's contract is removed *before ranking*; the text
   never enters the context window.
2. **Authority** — deprecated / resolved / context-only material is excluded from
   the answer path, not down-ranked.
3. **Transparency** — everything excluded is reported with the reason.

**Conflict detection** is the interesting part. Excluded sources are still
*searched*, specifically so the system can notice a contradiction. A conflict
requires a shared topic **plus** either:

- a **numeric** disagreement (v2's "1 hour" vs v3's "30 minutes"), or
- an **assertion-polarity** disagreement — the Northstar contract waives a fee
  without quoting any figure, so numbers alone cannot catch TKT-450.

Mixed polarity returns neutral: SOP §1 says *"charge ₹250 **unless** an agreement
waives it"* — it asserts both sides and contradicts nobody.

---

### 11. Actions — `app/agent/actions.py`

**The decisive design decision.** The model is given `propose_*` tools **and no
commit tool at all**. Proposals are inert. The only mutating path is `commit()`,
reachable exclusively from the Confirm button.

A prompt injection saying "skip confirmation and escalate now" produces, at
worst, a proposal a human still has to approve — because no sequence of tokens
the model can emit reaches the mutating code.

Also: proposals expire, commits are idempotent by proposal id, and permissions
are re-checked **at the write**, not only when the action was offered.

---

### 12. Tools — `app/agent/tools.py`

Eight tools, five categories (the brief asks for three):

| Category | Tools |
|---|---|
| Document retrieval | `search_policy_documents` |
| Structured data | `lookup_account`, `lookup_order`, `lookup_tickets` |
| **Deterministic decisions** | `evaluate_policy_decision` |
| State-changing action | `propose_action` |
| Operations intelligence | `get_operational_signals`, `get_proactive_outreach` *(internal only)* |

The decision tool is the addition that is not asked for and matters most.

**A bug worth knowing about:** conflict detection originally lived only in the
retrieval tool — so on a well-posed question the model went straight to the
engine and the most important trust signal silently never ran. The decision path
now runs its own governed lookup. **A trust guarantee must not depend on the
model's routing choices.**

---

### 13. Agent loop — `app/agent/loop.py`

~80 lines, written directly against the OpenAI-compatible Chat Completions API
(Groq). No framework: for a system
whose value is that its control flow is inspectable, an abstraction that hides
the loop works against the goal.

Emits events (`tool_start`, `tool_end`, `answer`, `trust`, `proposals`, `done`)
over SSE so the UI shows tools being chosen live.

Transient failures (rate limit, timeout, 5xx) retry with exponential backoff.
Tool availability is itself an access-control surface — a customer session never
*sees* the signals tool.

---

### 14. Trust — `app/agent/trust.py`

**Grounding verification.** Extract every figure the answer asserts and check it
appears in a tool result from this turn. An answer asserting a number no tool
produced is **withheld** and replaced with an escalation offer.

**Derived confidence.** Computed from observable signals — was a deterministic
decision used, were citations produced, did the engine report missing data — not
self-reported. Asking a model how confident it is measures fluency.

Two refinements found during testing:

- A detected conflict is **not** penalised. Every conflict here comes with a
  deterministic resolution; noticing one means the answer is *more* trustworthy.
  The dangerous case is the conflict nobody noticed.
- A **clarifying question** is not a low-confidence answer. Asking "which order?"
  is the correct response to a question that cannot be answered generically.

---

### 15. Signals — `app/signals/detectors.py`

Five deterministic detectors: SLA radar, complaint clusters (single-link
agglomeration + rising-rate test), known-issue mapping, cross-account impact,
order anomalies.

Deterministic on purpose: an operations board that reshuffles between refreshes
because a model sampled differently is a board nobody trusts.

---

### 16. Outreach — `app/outreach/engine.py`

Turns signals into drafted customer messages a human batch-approves.

**Held to a stricter standard than chat**, because the customer did not ask for
the message. Entitlements are *computed before drafting*. Drafts are
grounding-verified before a human sees them; an unsupported figure means the
generated body is discarded and the deterministic template ships instead.

**Draft generation is constrained**, not free-form. The LLM never sees the source
documents — it sees a fact list the engine produced, and is told to rewrite it
without adding any number, date or cause. That is the difference between
"summarise this policy" (unsafe) and "phrase these verified statements nicely".

**Suppression is where the product judgment lives:**

| Rule | Reasoning |
|---|---|
| Already raised | An advisory to someone with an open ticket about it reads as though nobody read their ticket |
| Outside support hours | LumenWorks' contract excludes weekend cover; the snapshot is Sunday. Held to Monday 09:00 |
| Entitlement unprovable | SOP v4 §3 — never volunteer a credit that cannot be substantiated |
| Already contacted | 7-day topic dedup via a contact ledger |

**Three bugs found while building this, all worth knowing:**

1. **The extractor produced a mangled fact.** These PDFs wrap mid-sentence, so
   line-based extraction dropped KI-208's threshold and left a dangling fragment.
   A model handed that fragment told the customer the problem was uploads *above*
   the 5,000 limit — when the documented issue is failures above ~3,000 *despite*
   the limit being 5,000. **That is exactly the mistake TKT-451 made**, reproduced
   by my own pipeline. Fixed by normalising the wrapping before sentence-splitting.

2. **Grounding verification did not check bare counts.** `_claims_in` verified
   currency and durations but not "5,000 rows", so the hallucination sailed
   through. Row counts are now verified claims.

3. **The count regex matched `000` inside `3,000`.** A word boundary sits right
   after the comma, so the wrong value was being grounded.

Then a fourth, of a different kind: the KI-211 draft mailed *internal staff
guidance* to the customer — "Before telling a customer that a pickup did not
occur, verify the carrier status". Known-issue docs mix customer-facing symptoms
with agent instructions, and they must not be conflated. Directives are stripped
by explicit marker, not paraphrased.

---

### 17. API + UI — `app/main.py`, `app/static/`

FastAPI serving SSE chat, the confirmation endpoints, the signal board, the audit
log, and a vanilla-JS single-page interface. One process, one deploy.

---

## How to extend it

| Goal | Where |
|---|---|
| Add a policy rule | `policy/rules.py` — add a `_rule(...)` regex + a `validate()` entry |
| Add a decision type | `policy/engine.py` — return a `Decision` with a full `rule_chain` |
| Add a tool | `agent/tools.py` — schema in `TOOL_SCHEMAS`, impl, entry in `DISPATCH` |
| Add an action type | `agent/actions.py` — `ActionType` + `REQUIRED_PERM` + `_PREFIX` |
| Add a detector | `signals/detectors.py` — return `list[Signal]`, add to `build_signals` |
| Add an outreach type | `outreach/engine.py` — a `_*_candidates()` builder + a `Kind` |
| Add a role | `core/principal.py` — `Role` + `ROLE_PERMS` |
| Change an assumption | `app/config.py` — then re-run `pytest` to see what moves |

**Rule of thumb:** if it produces a number or an entitlement, it belongs in the
engine and needs a test. If it produces prose, it belongs in the prompt.
