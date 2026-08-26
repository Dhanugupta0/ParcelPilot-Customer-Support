# Everything used in this project

Complete inventory — dependencies, techniques, and what each is doing here.

---

## 1. Runtime

| Thing | Version | Why |
|---|---|---|
| **Python** | 3.12 (Docker) / 3.14 (dev) | Standard for this kind of work; `zoneinfo` and modern typing are stdlib |
| **Docker** | `python:3.12-slim` | Reproducible deploy; single process |

---

## 2. Python dependencies

Everything in `requirements.txt`, and nothing that isn't used.

| Package | Where it's used | What it does |
|---|---|---|
| **fastapi** | `app/main.py` | HTTP API, SSE streaming, static file serving |
| **uvicorn** | run command | ASGI server |
| **pydantic** v2 | `core/models.py`, `policy/engine.py`, `agent/actions.py` | Typed domain models, validation, clean JSON for tool results |
| **python-dotenv** | `app/config.py` | Loads `.env` |
| **openai** | `core/llm.py`, `agent/loop.py` | Client for Groq's OpenAI-compatible Chat Completions + tool calling |
| **openpyxl** | `data/repository.py` | Reads the `.xlsx` workbook |
| **PyMuPDF** (`fitz`) | `ingest/corpus.py` | PDF text **and `find_tables()`** — extracts the SLA matrix as real rows |
| **rank-bm25** | `retrieval/index.py` | BM25 Okapi lexical scoring |
| **scikit-learn** | `retrieval/index.py` | `TfidfVectorizer` with char n-grams |
| **sqlite3** (stdlib) | `store/db.py` | Durable conversations, tool calls, escalations |
| **sentence-transformers** | `store/vectors.py` | Local 384-dim embeddings for semantic search over past chats |
| **numpy** | transitive | Array ops behind sklearn |
| **pytest** | `tests/` | 88 tests (79 deterministic + 9 behavioural), with a custom `llm` marker |

**Deliberately NOT used:**

| Not used | Why |
|---|---|
| LangChain / LlamaIndex | The value here is inspectable control flow; a framework hides the loop. It's 80 lines. |
| Chroma / Pinecone / FAISS | Still not used **for the policy corpus** — 26 chunks, where BM25 beats embeddings on exact clauses and figures. Conversation history *does* use vectors (`store/vectors.py`), stored in the same SQLite file rather than a separate service. |
| A hosted embeddings API | Groq serves no embeddings endpoint, so `all-MiniLM-L6-v2` runs locally: offline, free, and no second provider to key. |
| A database **server** | SQLite. Everything else in the stack is a local file; a daemon would be the only thing that has to be running for the app to boot. |
| React / Next.js | One deploy, no build step, and the UI is ~600 lines total. |
| Streamlit | Can't do a live tool trace or a real dashboard well. |

---

## 3. Frontend

| Thing | Notes |
|---|---|
| **Vanilla JS** (356 lines) | No framework, no build step |
| **CSS custom properties** (200 lines) | Dark theme via `:root` tokens |
| **Server-Sent Events** | One-way streaming fits the tool trace exactly; simpler than WebSockets |
| **Escaping-first markdown** | Deliberately minimal — model output must never inject markup |

---

## 4. AI / LLM techniques

| Technique | Where | Notes |
|---|---|---|
| **Tool calling / function calling** | `agent/tools.py` | 8 tools, 5 categories, JSON-schema definitions |
| **Multi-step agent loop** | `agent/loop.py` | Max 8 steps, `tool_choice="auto"`, temp 0.1 |
| **Tool-availability scoping** | `loop._tools_for()` | Customers never *see* the signals tool — stronger than declining at call time |
| **Hybrid retrieval** | `retrieval/index.py` | BM25 + char n-gram TF-IDF |
| **Reciprocal Rank Fusion** | `retrieval/index.py` | Combines incomparable scorers without calibration |
| **Query expansion** | `retrieval/index.py` | Domain synonym table (`cancel`→`cancellation`, `sla`→`response target`) |
| **Metadata-filtered retrieval** | `retrieval/governed.py` | Authority tier + account scope as hard filters |
| **Source-diversity capping** | `retrieval/governed.py` | Max 3 chunks/doc so results span the precedence chain |
| **Conflict detection** | `retrieval/governed.py` | Topic overlap + numeric **or** polarity disagreement |
| **Grounding verification** | `agent/trust.py` | Every asserted figure must trace to a tool result |
| **Derived confidence** | `agent/trust.py` | Computed from signals, never self-reported |
| **Abstention / withholding** | `agent/loop.py` | Ungrounded answers are replaced, not shipped |
| **Structured extraction** | `ingest/contract_terms.py` | Contracts → typed terms with citations |
| **Retry with backoff** | `agent/loop.py` | Rate limit / timeout / 5xx only |
| **Human-in-the-loop gating** | `agent/actions.py` | Two-phase propose → confirm → commit |
| **Constrained generation** | `outreach/engine.py` | LLM writes prose over a deterministic fact list, never over raw documents |
| **Generate-then-verify** | `outreach/engine.py` | Draft is grounding-checked; falls back to the template if a figure is unsupported |

**Provider:** Groq, via its OpenAI-compatible endpoint — the `openai` SDK with a
different `base_url`, wired in one place (`app/core/llm.py`).

**Model:** `openai/gpt-oss-120b` for the agent (`GROQ_MODEL`), `qwen/qwen3.6-27b`
for the outreach prose pass (`GROQ_UTILITY_MODEL`). Both are reasoning models, so
`reasoning_format=hidden` keeps the chain of thought out of the streamed answer and
out of customer-facing email, with a `<think>`-stripper behind it as a safety net.

---

## 5. Non-AI techniques (where most of the correctness lives)

| Technique | Where | Notes |
|---|---|---|
| **Deterministic rules engine** | `policy/engine.py` | Every fee, credit, SLA. Returns a full rule chain |
| **Business-hours calendar** | `core/clock.py` | Two clock types; the Sunday problem |
| **Snapshot clock** | `core/clock.py` | Reference time from the README, never wall-clock |
| **Boot-time validation** | `policy/rules.py` | Refuses to start if a rule didn't parse |
| **Rule parsing from source PDFs** | `policy/rules.py` | Nothing hard-coded; swap in v4 and it follows |
| **Table extraction** | `ingest/corpus.py` | `find_tables()` → the plan × severity matrix |
| **Section-aware chunking** | `ingest/corpus.py` | Split on `N. Heading` → citable `§` references |
| **RBAC + tenant isolation** | `core/principal.py` | Permission sets; one choke point for account access |
| **Field-level redaction** | `core/models.py` | Role-driven, on the way out of the repository |
| **Uniform error responses** | `data/repository.py` | Denied ≡ not-found, to block ID enumeration |
| **Append-only audit log** | `core/audit.py` | JSONL — every call, proposal, commit, denial |
| **Idempotency** | `agent/actions.py` | Commit keyed by proposal id |
| **TTL expiry** | `agent/actions.py` | Stale approvals can't be replayed |
| **Rule-based classification** | `policy/triage.py` | Explainable, stable, free |
| **Single-link agglomerative clustering** | `signals/detectors.py` | Transitive; groups reworded complaints |
| **Rising-rate detection** | `signals/detectors.py` | Last 7 days vs preceding 14, normalised |
| **Jaccard similarity** | `signals/detectors.py` | Cluster pairing |
| **Seeded synthetic data** | `scripts/make_synthetic.py` | Deterministic; identical board every run |
| **Suppression rules** | `outreach/engine.py` | Already-raised, coverage hours, unprovable entitlement, repeat contact |
| **Contact ledger** | `outreach/engine.py` | JSONL; 7-day topic dedup so confirmed sends don't reappear |
| **Sentence-level content filtering** | `outreach/engine.py` | Strips internal staff directives from customer-facing text |

---

## 6. Data sources

**Supplied (read-only, `Dataset/`):**

| File | Tier | Role |
|---|---|---|
| `01_Support_Policy_v3_CURRENT.pdf` | 2 | SLA matrix, severity definitions, **the precedence rule itself** |
| `02_Support_Policy_v2_DEPRECATED.pdf` | 4 | Trap — must be unreachable |
| `03_Cancellation_and_Service_Credit_SOP_v4.pdf` | 2 | Fees, credit formula, approval threshold |
| `04_Product_Operations_Guide_and_Known_Issues.pdf` | 3 | Plan capabilities, KI-208 / KI-211 / KI-176 |
| `05_Northstar_Logistics_Enterprise_Agreement.pdf` | 1 | ACCT-001 overrides |
| `06_LumenWorks_Service_Agreement.pdf` | 1 | ACCT-002 overrides |
| `ParcelPilot_Assessment_Data.xlsx` | — | README (snapshot), accounts, orders, tickets |

**Added:** `data/synthetic_tickets.json` — 30 `SYN-`-prefixed tickets, flagged
`synthetic=True`, hidden from customers, no historical resolutions. Exists so the
Signal Board has a baseline to detect a rise against.

---

## 7. Testing

| | Count | Runtime | Needs a key |
|---|---|---|---|
| `test_golden.py` | 35 | ~0.8 s | No |
| `test_adversarial.py` | 22 | ~0.4 s | No |
| `test_outreach.py` | 13 | ~17 s | No |
| `test_e2e_llm.py` | 9 | ~35 s | Yes (auto-skipped) |
| **Total** | **79** | ~70 s | |

The deterministic 70 cover every trap in the pack and run in ~2 s —
because the parts that must be right are decided by code, not by a model.

---

## 8. Deployment

| File | Purpose |
|---|---|
| `Dockerfile` | `python:3.12-slim`, single uvicorn worker |
| `render.yaml` | Render blueprint, `GROQ_API_KEY` as `sync: false` |
| `Procfile` | Railway / Heroku |
| `.env.example` | Template; placeholder key detected and treated as absent |
| `/api/health` | Health check — snapshot, model, record counts |

---

## 9. Documentation

| File | Contents |
|---|---|
| `README.md` | Trap table, architecture, the five decisions, assumptions, trade-offs |
| `IMPLEMENTATION.md` | Layer-by-layer build guide + how to extend |
| `STACK.md` | This file |
| `NEXT.md` | Prioritised roadmap + what I'd deliberately not build |
| `PRODUCT_NOTE.md` | Product note: problems chosen, what was left out, the metric, AI tool usage |
| `DEMO.md` | 5-minute video script with timings |

---

## 10. Requirements coverage

| Requirement | Where |
|---|---|
| 1 · Chatbot, NL, source authority | `agent/loop.py` + `retrieval/governed.py` |
| 2 · Access control & privacy | `core/principal.py` + `data/repository.py` |
| 3 · ≥3 distinct tools | `agent/tools.py` — **8 across 5 categories** |
| 4 · Confirmation before actions | `agent/actions.py` — model has **no** commit tool |
| 5 · Multi-step requests | Verified: ORD-1001 uses 3–4 tools |
| 6 · Interface w/ tool visibility | `app/static/` — separate customer and employee portals, live SSE trace, token-streamed answers |
| 7 · Demo video | `DEMO.md` |
| **Problem 1** · Proactive detection | `signals/detectors.py` — 5 detectors + `outreach/engine.py` acts on them |
| **Problem 2** · Trust & reliability | `agent/trust.py` + conflict detection + abstention |
| **Beyond** | `NEXT.md`, `PRODUCT_NOTE.md` |
| Architecture note | `README.md` |
| Product note + AI tool usage | `PRODUCT_NOTE.md` |
