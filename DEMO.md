# Demo — 5 minutes

**Before you record**

```bash
rm -rf var/                       # clean slate: no stale conversations
python scripts/preflight.py       # must say "Safe to deploy"
uvicorn app.api:app --port 8000
```

- Check your Groq daily quota has room — one agent turn costs ~4-8k tokens of
  the 200k/day free tier. **Six or seven questions is a whole demo.** Do a dry
  run the day before, not an hour before.
- Chrome force-dark makes the light UI look muddy: turn off
  `chrome://flags/#enable-force-dark`.
- Have two tabs open: **customer** and **employee**, both already signed in.
  Signing in on camera wastes 20 seconds twice.

---

## The spine of the story

Say this in the first fifteen seconds and everything else lands:

> "Most support bots let the model look things up and then do the maths in its
> head. Ask it *why INR 250* and the honest answer is 'because it sampled that.'
> I inverted it. **The model decides which tools to call; the tools decide what
> the answer is.** So every figure comes from code you can unit-test, and the
> model's job is routing and explaining."

---

## 0:00–0:40 · Architecture

Screen: `README.md`, the mermaid diagram.

> "Two users — a customer and a support agent — hitting one FastAPI app.
> The agent loop is ninety lines, no framework, because the whole claim is that
> the control flow is inspectable.
>
> It chooses between four tool categories. Document retrieval hits a vector
> store of the policy PDFs. Structured lookups hit SQLite, loaded from the
> workbook. **The third one is the one that matters** — `calculate` — the only
> source of a fee, a credit or an SLA deadline. And the fourth prepares actions
> that only a human can commit.
>
> Blue is the only non-deterministic box on this diagram. Green is where every
> number comes from, and where it gets checked on the way back out."

---

## 0:40–1:40 · The example from the brief

Screen: **employee** tab → Assistant.

Type: `Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.`

Point at the **tool chips appearing live** while it runs.

> "That's requirement six — you can see which tool is running."

When it answers, **expand the rule chain**.

> "This is the part I care about. The order was cancelled a hundred and twenty
> minutes after booking. The SOP says that's a two-fifty rupee fee — and if you
> look at ticket TKT-450 in this dataset, an agent told Northstar exactly that.
> **That agent was wrong.** Their signed agreement waives the fee outright.
>
> The engine applies the agreement, returns zero, and names the clause that
> overrode which. That's not the model explaining itself after the fact — it's
> the actual chain that produced the number."

---

## 1:40–2:30 · The question that must not be answered

Type: `A pickup is three hours late because of carrier fault. Should I get a service credit?`

> "It refuses, and asks which order. That's deliberate. Three hours qualifies
> under the standard SOP — the threshold is two hours. It does **not** qualify
> under the LumenWorks agreement, where the threshold is four.
>
> Answering generically here is answering a different question from the one
> asked. And that refusal is in the tool — `calculate` won't run without an
> order id — so it isn't relying on the model being careful."

Then: `Is a credit due on ORD-2002?`

> "Now it can. Three hundred rupees — and note that's not the default rule. The
> default is the lower of five hundred or ten percent of the fee, which would be
> two forty. LumenWorks' contract replaces **both** the threshold and the amount."

---

## 2:30–3:15 · The SLA board, and the Sunday

Screen: **SLA Board** tab.

> "This is computed for every open ticket at once, with no model involved.
> Two are breaching."

Point at **TKT-502**, within target.

> "Here's my favourite thing in the dataset. The snapshot is a **Sunday**.
> This ticket came in at 09:45 with a four-business-hour target — so naive
> arithmetic says 13:45 Sunday, breached.
>
> But LumenWorks' agreement excludes weekend cover. The clock doesn't start
> until Monday at nine, so it's due Monday at one and it is **not** breached.
> Getting that wrong invents an SLA failure that never happened and has an agent
> apologising for being late when they aren't."

---

## 3:15–4:00 · Actions, gated

Type: `TKT-505 is a P1 security incident and its SLA is breached. Prepare an escalation.`

> "It prepares. It does not execute — and it *cannot*: there's no tool in the
> schema that commits anything. The gate isn't a prompt asking it to be careful,
> it's the absence of a capability."

Press **Confirm**. Show the reference appearing.

> "A person pressed that. That's the only path to a state change."

*(Optional, if time: mention that a credit above INR 1,000 refuses an agent and
requires a manager — SOP v4 §3, enforced in the action layer.)*

---

## 4:00–4:40 · The customer side

Screen: **customer** tab (Ravi / Northstar).

> "Same engine, different product."

Click **ORD-1001** from their issue list.

> "They pick what it's about, so they never re-type a reference."

Type: `Can I cancel this without a fee?`

Point at the **right-hand panel** while it runs.

> "They see that the answer was looked up, in plain language — no clause
> numbers, no confidence scores, no tool names. And no internal issue codes:
> the SwiftShip caveat reaches them as 'pickup confirmations can be delayed',
> not 'KI-211'.
>
> The trust machinery all still runs. They just aren't asked to do quality
> control on their own support reply."

---

## 4:40–5:00 · Close

> "Fifty tests, and none of them need an API key — because the decision layer
> is code. A wrong number here is a failing assertion, not an unlucky sample.
>
> If I kept going: aggregate credit tracking, because Northstar's contract caps
> monthly credits at five thousand and nothing yet records what's been issued.
> That's the one place this system can be right about a single credit and wrong
> about the account."

---

## Checklist — the brief's requirements, and where each is on screen

| # | Requirement | Show it at |
|---|---|---|
| 1 | Chatbot, natural language, sources with differing authority | 0:40 — contract overrides SOP |
| 1 | Escalates what needs human judgement | 1:40 — refuses to answer generically; 3:15 — escalation |
| 2 | Access control **in the data/tool layer** | 4:00 — customer sees only their own; say the phrase "enforced in the query, not the prompt" |
| 3 | ≥3 distinct tools it chooses between | 0:40 — tool chips; 0:00 — four categories on the diagram |
| 4 | Confirmation before state change | 3:15 — Confirm button |
| 5 | Multi-step requests | 0:40 — look up order → account → agreement → calculate |
| 6 | Interface shows which tool is used | 0:40 (agent chips) and 4:00 (customer panel) |
| P2 | Trust and reliability | 0:40 (precedence) + 2:30 (weekend clock) + 4:40 (tests) |
| P1 | Proactive detection | 2:30 — SLA board, computed unprompted |

**Also submit:** `README.md`, `ARCHITECTURE.md`, `PRODUCT.md`, `AI_TOOLS.md`,
the repo link, and the hosted URL.

---

## Things not to do

- **Don't sign in on camera.** Two tabs, pre-signed.
- **Don't read the rule chain aloud line by line.** Point at it, say what it
  proves, move on.
- **Don't demo more than six questions.** Quota, and pacing.
- **Don't apologise for mocked actions** — the brief permits it. Say "the gate
  is real; what's behind it is a local ledger."
- **Don't skip the Sunday.** It is the single best evidence that the system
  reasons about the data rather than pattern-matching it.
