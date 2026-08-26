# ParcelPilot Support Intelligence

An AI support system for ParcelPilot's B2B logistics platform, serving two user
contexts from one engine: a **customer-facing assistant** scoped to a single
account, and an **internal support/operations assistant** with cross-account
access and a proactive Signal Board.

---

## The problem this is actually solving

The data pack is deliberately imperfect, and almost every design decision here
follows from that. Reading it closely, the failure modes it is built to provoke
are:

| Trap | What a naive system answers | What is actually true |
|---|---|---|
| ORD-1001, cancelled 120 min after booking | ₹250 fee (SOP v4 §1) | **₹0** — the Northstar agreement waives it *regardless of elapsed time* |
| TKT-450, a closed ticket saying "₹250 applied" | Repeats it — it is the closest semantic match | **It is wrong.** A past resolution contradicts the contract |
| ORD-2001, LumenWorks, 75 min | ₹0 (copying the logic above) | **₹250** — LumenWorks' agreement *explicitly declines* a waiver |
| ORD-2002, 4.5 h late, carrier fault | ₹240 (default: min(₹500, 10%×₹2,400)) | **₹300** — the agreement replaces *both* threshold and amount |
| "Pickup 3 h late — do I get a credit?" | "Yes, ₹500" | **Unanswerable without the account.** Northstar yes (>2 h); LumenWorks **no** (>4 h threshold) |
| TKT-502, 4,200-row CSV fails | "Growth caps at 3,000 rows" (per TKT-451) | **Wrong.** The limit is 5,000; this is known issue **KI-208** |
| TKT-504, SwiftShip stuck on BOOKED | "Pickup didn't happen" | **KI-211** — webhooks lag up to 20 min. Do not tell the customer it failed |
| TKT-503, "how do I change the billing contact?" | Invents steps | **Nothing in the pack covers it.** Abstain and escalate |
| Support Policy v2 | "Enterprise P1 = 1 hour" | Deprecated. Must be structurally unreachable |
| **The snapshot is a Sunday** | 5 tickets ticking, wrong ones flagged | **2 real breaches.** `24x7` targets run; `business hours` targets have not started |

The last one is the deepest. Targets in this pack are written in two different
units — `"15 minutes, 24x7"` and `"4 business hours"` — and 2026-08-16 is a
Sunday. A wall-clock implementation reports the wrong tickets as on fire.

---

## Architecture

```
┌──────────── UI: chat · live tool trace · Signal Board · audit log ────────────┐
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │ SSE
┌───────────────────────────────────▼──────────────────────────────────────────┐
│  Agent loop (Groq tool calling)     ──  ROUTES and NARRATES only             │
└──┬───────────────┬────────────────┬──────────────────┬──────────────────────┘
   │               │                │                  │
┌──▼─────────┐ ┌───▼──────────┐ ┌───▼────────────┐ ┌───▼──────────────────────┐
│ Governed   │ │ Structured   │ │ POLICY ENGINE  │ │ propose_action           │
│ retrieval  │ │ data lookups │ │ (deterministic)│ │   → human Confirm        │
│            │ │              │ │                │ │   → commit (server only) │
│ authority  │ │ account /    │ │ cancellation   │ │                          │
│ tiers +    │ │ order /      │ │ credits · SLA  │ │ model has NO commit tool │
│ conflicts  │ │ tickets      │ │ + rule chain   │ │                          │
└──┬─────────┘ └───┬──────────┘ └───┬────────────┘ └───┬──────────────────────┘
   └───────────────┴────────────────┴──────────────────┘
              ┌──────────────────────────────────┐
              │  ACCESS-CONTROL GATE (Principal) │  every call, no exceptions
              └──────────────────────────────────┘
```

### Five decisions worth defending

**1. The model does not decide anything with a number attached.**
Cancellation fees, credit amounts and SLA deadlines are computed in
`app/policy/engine.py` in ordinary Python, from rules parsed out of the source
PDFs. The engine returns a `Decision` with a full `rule_chain` — each step names
and quotes the clause it applied — and the model's job is to explain a decision
it did not make. It cannot invent "₹250" because it is never asked to produce a
number. This is what makes answers reproducible, auditable, and unit-testable.

**2. Authority is metadata, not prompt text.**
Support Policy v3 §1 states the precedence rule itself: *"use the signed customer
agreement first, then the current support policy, then current product
documentation. Historical tickets and internal notes are context only."* That
sentence is encoded as an `authority_tier` on every chunk at ingest. Deprecated
documents and historical resolutions are **removed from the answer path**, not
down-ranked — a down-ranked wrong answer still occasionally wins.

**3. Confirmation is a state machine, not an instruction.**
The model is given `propose_action` and nothing else. There is no commit tool in
its schema. The only code path that mutates state is reachable exclusively from
the Confirm button in the interface. A prompt injection saying "skip confirmation
and escalate now" produces, at worst, a proposal a human still has to approve.
Commits are idempotent, proposals expire, and permissions are re-checked *at the
write*, not only when the action was offered.

**4. Access control lives in the data layer.**
A `Principal` is bound server-side and required by every repository call. A
customer asking about another account's order is refused before the model sees
anything — and the refusal is **textually identical** to a genuinely-missing
record, so the error channel cannot be used to enumerate other tenants' IDs.
Tool *availability* is scoped too: a customer session never even sees the
operations-signals tool.

**5. Confidence is derived, not self-reported.**
Asking a model how confident it is measures fluency, not correctness. Confidence
here is computed from observable signals: did a deterministic decision back the
answer, were citations produced, did the engine report missing data, were
conflicts detected. Every figure in the final answer is then checked against the
tool results that produced it — and an answer asserting a number no tool returned
is **withheld** and replaced with an escalation offer.

---

## The two client problems

### Problem 1 — Proactive issue detection

The **Signal Board** (internal roles only) runs five deterministic detectors and
ranks what deserves attention:

- **SLA radar** — every open ticket auto-triaged, resolved against the *correct*
  target (contract or policy) on the *correct* clock, ranked by overrun.
- **Complaint clusters** — single-link agglomeration over ticket text, with a
  rising-rate test against the preceding fortnight.
- **Known-issue mapping** — clusters matched to documented issues, so a
  workaround can be sent proactively instead of rediscovered per ticket.
- **Cross-account impact** — one problem hitting several customers at once.
- **Order anomalies** — uncollected shipments accruing delay, carrier fault
  rates, elevated cancellation rates.

Every signal carries its evidence and a recommended action that routes through
the same confirmation gate as everything else.

Detectors are deterministic on purpose: an operations board that reshuffles
between refreshes because a model sampled differently is a board nobody trusts.

#### Proactive Outreach — acting on the signals

Detection is only half of it. The **Outreach** tab turns signals into drafted
customer messages a human batch-approves: an uncollected shipment arrives with
the credit *already computed*, and an account caught in a known-issue cluster
arrives with the documented workaround attached.

Outreach is held to a stricter standard than chat, because the customer did not
ask. Every claim comes from the policy engine or a cited document; drafts are
grounding-verified before a human ever sees one; and a draft whose figures do
not trace to a fact is discarded rather than shown.

**Deciding who NOT to contact is most of the value**, and it is where the
product judgment lives:

| Suppression rule | Why |
|---|---|
| Customer already raised it | Emailing "we noticed your bulk uploads are failing" to someone with an open ticket about exactly that reads as though nobody read their ticket. Reply on the ticket instead. |
| Outside contractual support hours | LumenWorks' agreement excludes weekend cover and the snapshot is a Sunday. Telling them about a problem they cannot get help with until Monday manufactures anxiety with no route to resolution. Held until 09:00 Monday. |
| Entitlement not provable | SOP v4 §3 forbids promising a credit while fault is unknown. Volunteering one unasked is worse than answering one. |
| Already contacted | Sending once suppresses the same topic for 7 days, so a confirmed send does not reappear on the next refresh. |

Suppressed cases are shown with their reason, not hidden — a deliberate decision
not to contact someone is worth reporting.

### Conversation history and review

Conversations are durable. A customer opens a chat **from the ticket or order it
is about**, so they never re-type the reference — and can pick the thread back
up later, with the model's context restored rather than starting cold.

For employees, every conversation the assistant has had is reviewable, and the
review pane shows the *working*, not just the prose: each tool call with the
arguments it was given and the raw result it returned, the documents cited, the
derived confidence, and whether the answer was withheld. "Why did it tell them
that" is answerable after the fact, which is the difference between a transcript
and an audit.

Search over that history is **semantic**, and this is the one place in the
system where embeddings earn their keep. A support agent looking for prior cases
does not know what words the customer used: *"compensation for a missed
collection"* has to find a thread that said *"pickup never happened"*, and those
share no term. The policy corpus keeps lexical retrieval for exactly the
opposite reason — see [`app/retrieval/index.py`](app/retrieval/index.py).

**What the two portals show is deliberately different.** A customer sees the
answer. They do not see the confidence band, the citations, the conflict panel
or the tool trace — those exist so an *agent* can audit the reply, and putting
them in front of the customer asks them to do quality control on their own
support response. The trust layer still runs, a withheld answer is still
withheld, and the entire assessment is still recorded for the employee console.

### Escalations

Escalations raised through the assistant appear on the employee dashboard with
their state made explicit: a proposal is not an escalation until a human
confirms it, so *awaiting confirmation* and *live* are shown as different
things. Each one links back to the conversation that raised it.

### Problem 2 — Trust and reliability

- **Conflict detection.** Excluded sources are still *searched*, specifically so
  the system can notice a superseded policy or a wrong past answer contradicting
  current authority — and say so. Conflicts are matched on shared topic plus
  either a numeric disagreement or an **assertion-polarity** disagreement (the
  Northstar contract waives a fee without quoting a figure, so numbers alone
  cannot catch it).
- **Transparency about exclusions.** Every filtered source is reported with the
  reason: *"Support Policy v2 matched your question but was excluded because it
  is deprecated."*
- **Grounding verification** on every answer, as above.
- **Abstention.** `INSUFFICIENT_DATA`, missing fields, or an uncovered topic
  produce an explicit refusal plus an escalation offer — never a plausible guess.
- **Caveats distinct from decisions.** A rule outcome can be certain while the
  underlying data is not. ORD-1001 is the case: the contract answer is
  unambiguous, but it is a SwiftShip order inside its pickup window and KI-211
  makes `BOOKED` potentially stale — so acting on it needs carrier verification
  first.
- **Audit log.** Append-only record of every tool call, proposal, commit and
  denial.

---

## Running it

```bash
cp .env.example .env          # add your GROQ_API_KEY (console.groq.com/keys)
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

No database server to start. The workbook and PDFs in `Dataset/` are the system
of record for **policy and account data** — parsed into memory at boot, with
startup failing loudly if any of it cannot be read. Everything the system
*produces* (conversations, tool calls, escalations, the audit log) goes into a
SQLite file at `var/parcelpilot.db`, created automatically on first run.
`rm -rf var/` resets a demo.

The first run downloads a ~90MB embedding model (`all-MiniLM-L6-v2`) for
semantic search over conversation history. Without it the app still runs and
still records everything — only the search box stops working, and it says so.

> **Free-tier quotas.** Groq's free tier meters ~8,000 tokens per minute *and*
> 200,000 tokens per day. One agent turn costs a few thousand, so the daily cap
> is the one that bites: roughly 40-60 turns, and the `llm`-marked test suite
> spends a meaningful slice of it in a single run. Both arrive as a 429, and the
> loop treats them differently — a per-minute burst is waited out using the reset
> the response reports, while a spent daily allowance is reported to the operator
> immediately rather than retried, because it will not clear for hours. Check
> what is left with:
>
> ```bash
> curl -sI -X POST https://api.groq.com/openai/v1/chat/completions \
>   -H "Authorization: Bearer $GROQ_API_KEY" -H "Content-Type: application/json" \
>   -d '{"model":"openai/gpt-oss-120b","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
>   | grep -i ratelimit
> ```

Open http://localhost:8000. The sign-in screen asks which portal you want:

- **Customer portal** — one thing only, the assistant, scoped to a single
  account. It shows what the agent is doing in plain language ("Checking your
  account", "Applying the policy rules") and nothing else.
- **Employee portal** — the assistant with the full tool trace and raw tool
  results, plus the Signal Board, proactive Outreach and the Audit Log.

The two portals are built as separate screens rather than one screen with
features greyed out. A customer has no business knowing that a Signal Board
exists; the server enforces this regardless, but the interface should not
advertise doors it will not open.

Answers stream token by token. Streamed text is a **draft** — it is rendered
dimmed until the trust assessment has run, and is then replaced by the verified
answer. If the trust layer withholds an answer, the draft never survives on
screen.

**Docker / hosting**

```bash
docker build -t parcelpilot . && docker run -p 8000:8000 -e GROQ_API_KEY=gsk_... parcelpilot
```

`render.yaml` and `Procfile` are included for Render / Railway.

**Tests**

```bash
pytest                        # 70 deterministic tests, no API key needed
pytest -m llm                 # 9 end-to-end behavioural tests, needs a key
```

The deterministic suite covers every trap in the table above and runs in ~2 s,
because the parts that must be right are decided by code rather than by a model.

---

## Layout

```
app/
  config.py            all assumptions in one place, documented
  core/clock.py        snapshot time + business-hours calendar + SLA parsing
  core/principal.py    roles, permissions, tenant isolation
  core/models.py       domain models + field-level redaction
  core/audit.py        append-only audit log
  data/repository.py   the ONLY door to the workbook
  ingest/corpus.py     PDF → chunks with authority metadata
  ingest/contract_terms.py   agreements → machine-readable terms with citations
  policy/rules.py      policy defaults PARSED from the PDFs, validated at boot
  policy/engine.py     deterministic cancellation / credit / SLA decisions
  policy/triage.py     explainable severity classification
  retrieval/index.py   hybrid BM25 + char n-gram TF-IDF, rank-fused
  retrieval/governed.py  authority filtering, scoping, conflict detection
  agent/tools.py       8 tools across 5 categories
  agent/actions.py     two-phase propose → confirm → commit
  agent/loop.py        the agent loop
  agent/trust.py       grounding verification + derived confidence
  signals/detectors.py the Signal Board
  outreach/engine.py   proactive drafts + suppression rules
tests/                 70 deterministic + 9 LLM behavioural tests
scripts/               synthetic history generator
```

---

## Assumptions

Stated rather than buried, and all changeable in `app/config.py`:

1. **Business hours are Mon–Fri 09:00–18:00 IST.** The pack never defines them,
   and several targets are expressed in business hours. No public-holiday
   calendar was supplied, so none is applied.
2. **`"2 hours"` means elapsed hours; `"2 business hours"` means business hours.**
   Support Policy v3 §3 uses both forms in the same table, so the distinction is
   treated as intentional.
3. **`"1 business day"` = one working day (9 h) of business time.**
4. **First-response elapsed time is measured from ticket creation to the
   snapshot,** because the dataset records no agent-response timestamp. Only
   *open* tickets are assessed — a first-response breach on a closed ticket is
   meaningless.
5. **Absent fault evidence is not carrier fault.** SOP v4 §3 forbids promising a
   credit while fault is unknown, so the engine abstains rather than assumes.
6. **Synthetic ticket history** (`data/synthetic_tickets.json`, `SYN-` prefix) is
   added for the Signal Board only. It is flagged `synthetic=True`, hidden from
   customer sessions, and carries no historical resolutions — the two genuinely
   incorrect past answers in the pack remain the only ones.
7. **Auth is mocked** via a fixed identity directory. Roles and account binding
   are server-side; the browser never sends a role the server trusts.

---

## Deliberate trade-offs

**No vector database.** The corpus is 6 documents and 26 chunks of dense,
high-jargon policy text. At that size, embeddings add a ~500 MB dependency, a
slow cold start and another hosted service in exchange for recall that BM25 plus
character n-gram TF-IDF and a small synonym table already achieve. `Retriever` is
written as a swappable interface; the right time to add embeddings is when the
corpus outgrows lexical matching, not before.

**No agent framework.** For a system whose value proposition is that its control
flow is inspectable — who may call what, what gets filtered, what needs
confirmation — an abstraction that hides the loop works against the goal. The
loop is about eighty lines.

**Rule-based triage and detectors.** Explainable, stable across refreshes, and
free to run over every ticket. An LLM pass can refine borderline calls later; the
rules stay as the floor.

**In-process sessions.** Fine for one instance. Horizontal scale-out would move
sessions to Redis first.

---

## What I would build next

See **[NEXT.md](NEXT.md)** for the prioritised roadmap.
