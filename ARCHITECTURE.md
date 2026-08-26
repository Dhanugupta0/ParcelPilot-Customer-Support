# Architecture Note

## Agent design

One loop, ~90 lines, written directly against the chat-completions API rather
than through a framework (`app/agent.py`). For a system whose claim is that its
control flow is inspectable — who may call what, what is filtered, what needs
confirmation — an abstraction that hides the loop works against the goal.

The division of labour is the whole design:

> **The model decides which tools to call. The tools decide what the answer is.**

The agent picks `calculate(kind='cancellation', order_id='ORD-1001')`. The
engine returns INR 0 together with the clause that made it zero. Asking a model
to *route* is safe; asking it to do *arithmetic* is not, because a sampled
number cannot be defended to a customer.

Up to six steps per turn. Each step streams `tool_start` / `tool_end` events so
the interface can show what is running while it runs.

**Prompting carries three rules that the code also enforces**, because a rule
stated only in a prompt is a request, not a control:

1. Any fee, credit or SLA question must go through `calculate` — the tool is the
   only source of those figures.
2. Never state a figure, or a supporting fact, that no tool returned.
3. `propose_action` prepares; it never executes.

## Tool design

Four categories, exceeding the three required.

| Category | Tools | Why it is separate |
|---|---|---|
| **A · Document retrieval** | `search_policies` | Prose reasoning over clauses |
| **B · Structured data** | `lookup_order`, `lookup_ticket`, `lookup_account`, `list_tickets` | Facts about records |
| **C · Deterministic calculation** | `calculate` | **The addition that matters.** Without it the model reads a contract and a policy and does the sum in its head — exactly how a confidently wrong number is produced |
| **D · State-changing action** | `propose_action` | Gated on a human |

`tools.available(user)` withholds capabilities a role may not use. Removing a
tool from the schema is stronger than declining it at call time: the model
cannot be talked into invoking something it was never offered.

## Document and structured-data handling

**Documents → vector store.** Six PDFs are split into 25 sections (numbered
headings, plus one chunk per known issue so KI-208 and KI-211 are separately
retrievable), embedded with `all-MiniLM-L6-v2` locally. Groq serves no
embeddings endpoint, so this runs on the machine — offline and free.

Embeddings are usually the *wrong* tool for dense policy text: they blur
"INR 250" into "INR 500". They are safe here for one specific reason —
**retrieval never supplies a number.** Every figure comes from `engine.py`.
Search only has to find the passage worth *citing* beside an answer that has
already been calculated.

**Workbook → SQLite.** The `.xlsx` is the source, not the runtime store. It is
loaded into SQLite at every boot, so each lookup is a query with a `WHERE`
clause rather than a scan over parsed rows — which is what makes account
scoping enforceable in one place (`app/records.py`) instead of remembered at
every call site.

**Contract terms are transcribed into a table** (`engine.CONTRACTS`) rather than
parsed from PDF prose at runtime. Two agreements, fixed for this assessment; a
silent mis-parse of money or durations would be strictly worse than a table a
reviewer can check against the document in thirty seconds. Each field carries
the clause it came from.

## Source reliability and conflict handling

Precedence is taken from Support Policy v3 §1 and applied as **metadata, not
similarity**:

1. the customer's signed agreement
2. the current support policy and SOPs
3. current product documentation
4. historical tickets — context only, and the dataset warns some are **wrong**

A deprecated document can be the closest textual match to a question and must
still lose. Ranking is therefore authority-first, similarity-second.

- **Support Policy v2** is indexed but never returned as an answer. It appears
  in `excluded` with the reason, so an agent about to quote it can see it was
  considered and rejected.
- **Other customers' agreements** are filtered in the query. A customer session
  cannot retrieve them at all.
- **Historical resolutions** are returned by `lookup_ticket` with an explicit
  warning attached, so TKT-450's incorrect "INR 250 applies" is available as
  context and can be *contradicted* rather than repeated.
- **Known issues become caveats**: KI-211 means a SwiftShip `BOOKED` status does
  not prove a parcel is uncollected, so every such order carries that note. It
  is the difference between "it wasn't collected" and "we haven't been told it
  was collected".

## Major technical trade-offs

**Deterministic engine over model reasoning.** More code, and every rule has to
be written out. In exchange a wrong figure is a failing unit test rather than an
unlucky sample, and 50 tests run with no API key.

**SQLite over Postgres.** Everything else in the stack is a local file; a daemon
would be the only thing that must be running for the app to boot. No SQLite-only
syntax is used, so moving to Postgres is a connection string.

**Vectors for documents, SQL for records.** Not one store for both. Each is used
where it wins: semantic matching for prose, exact predicates for rows.

**A short agent loop over a long one.** Six steps. Beyond that the honest answer
is "narrow the question", not more speculative tool calls.

**Deliberately not built:** streaming token-by-token output (the tool trace is
the useful progress signal), a re-ranker (25 chunks), and a hosted vector
service (the index rebuilds in under a second).

## Failure behaviour

Because the decision is computed before the model writes, an outage costs the
prose and not the answer: whatever `calculate` returned is still correct and is
shown directly. The loop also distinguishes a per-minute rate limit (worth
waiting out, using the reset the provider states) from a spent daily quota
(never worth retrying).
