# ParcelPilot — 5 Minute Demo Script

Speak the **SAY** blocks word for word. Everything in **TYPE** is copy-paste.
Every question below was run against the live system today — the answers
described are the answers you will get.

---

## Before you hit record

```bash
python scripts/preflight.py              # must say "Safe to deploy"
uvicorn app.api:app --port 8000
```

- **Two Chrome tabs, both already signed in.** Tab 1 = employee (Rohit).
  Tab 2 = customer (**Sara Iyer / LumenWorks**). Signing in on camera costs you
  forty seconds you do not have.
- Turn off `chrome://flags/#enable-force-dark` — it makes the light UI muddy.
- **Check your Groq quota.** Five questions is the whole demo. Do a dry run the
  day before, not an hour before.
- **The single most important thing:** each answer takes 10–25 seconds to
  stream. Every SAY block below is written to be spoken *while it is running*.
  Do not type a question and then go silent.

---

## 0:00 – 0:25 · The hook

**SHOW:** the employee console, SLA Board tab, already open.

**SAY:**

> "Most support bots let the model look things up, and then do the maths in its
> head. Ask one why it said two hundred and fifty rupees, and the honest answer
> is: because it sampled that number.
>
> I inverted it. **The model decides which tools to call. The tools decide what
> the answer is.** Every figure on screen comes from code you can unit-test. The
> model's job is routing, and explaining."

---

## 0:25 – 1:05 · The board, and the Sunday

**SHOW:** stay on **SLA Board**. Point at the four stat tiles, then scroll to
**TKT-502** under "Within target".

**SAY:**

> "This is computed for every open ticket at once. No model involved — this
> screen would render with the API key removed. Two tickets are breaching.
>
> But this is the one I want you to look at. TKT-502.
>
> The dataset snapshot is a **Sunday**. This ticket came in at 09:45 with a
> four-business-hour target. Naive arithmetic says it was due at 13:45 on
> Sunday — breached.
>
> LumenWorks' signed agreement excludes weekend cover. So the clock doesn't
> start until Monday at nine. It's due Monday at one, and it is **not**
> breaching.
>
> Getting that wrong invents an SLA failure that never happened, and puts an
> agent on the phone apologising for being late when they weren't."

---

## 1:05 – 2:00 · The question from the brief

**SHOW:** click the **Assistant** tab.

**TYPE:**

```
Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.
```

**SAY — start the moment you press Send, pointing at the right-hand panel:**

> "On the right is the agent workflow. That's not a progress bar — that's the
> real tool name, the arguments it was called with, and what came back. You can
> watch it choose `calculate`, and you can see it's tagged **deterministic
> calculation**. That's the category that produces every number in this system."

**Wait for the answer. Then expand the rule chain and SAY:**

> "Zero rupees. And here's the chain that produced it.
>
> The order was cancelled a hundred and twenty minutes after booking. The SOP
> says that is a two-hundred-and-fifty-rupee fee. But Northstar's signed
> agreement waives the cancellation fee outright, before pickup, regardless of
> elapsed time — and the engine names the clause that did the overriding.
>
> That is not the model explaining itself after the fact. That is the actual
> chain that produced the number."

---

## 2:00 – 2:45 · The past answer that was wrong

**TYPE:**

```
On TKT-450 an agent charged Northstar a 250 rupee cancellation fee. Was that correct?
```

**SAY while it runs — it will make three tool calls, point at them:**

> "Watch the workflow panel. Ticket, then account, then policy search. That's a
> multi-step request being decomposed — nobody told it that order.
>
> TKT-450 is a real closed ticket in this dataset, and the recorded resolution
> says an agent charged the fee."

**When it answers, SAY:**

> "It says the agent was wrong, and names the clause that governs.
>
> That matters because historical tickets are the fourth and lowest source of
> authority in the policy — they're context, and the dataset warns some of them
> are incorrect. A system that learns from its own past answers would have
> repeated this mistake. This one contradicts it."

---

## 2:45 – 3:20 · The question it should refuse

**TYPE:**

```
A pickup is three hours late because of carrier fault. Should the customer get a service credit?
```

**SAY:**

> "It won't answer. It asks which order — and that's the correct behaviour.
>
> Three hours **does** qualify under the standard SOP, where the threshold is
> two hours. It does **not** qualify under the LumenWorks agreement, where the
> threshold is four. So there is no generic answer. Answering anyway would mean
> answering a different question from the one asked.
>
> And that refusal isn't the model being cautious. `calculate` will not run
> without an order id. It's enforced in the tool."

---

## 3:20 – 4:05 · The gate

**TYPE:**

```
TKT-505 is a P1 security incident and its SLA is breached. Prepare an escalation.
```

**SAY while it runs:**

> "This is the only category of tool that touches state. And notice what it
> does — it prepares. It does not execute."

**When the proposal card appears, SAY:**

> "It cannot execute. There is no tool in the schema that commits anything. The
> gate isn't a prompt asking the model to be careful — it's the absence of a
> capability."

**Press Confirm. Point at the reference.**

> "A person pressed that. That is the only path to a state change in this
> system. And above a thousand rupees, a credit refuses an agent entirely and
> requires a manager — that's SOP v4, enforced in the action layer."

---

## 4:05 – 4:45 · The customer side

**SHOW:** switch to **Tab 2** — Sara Iyer, LumenWorks. Click **ORD-2002** from
her issue list.

**SAY:**

> "Same engine. Different product. She picks what it's about, so she never
> re-types a reference."

**TYPE:**

```
Am I owed a credit on this order?
```

**SAY while it runs, pointing at the right-hand panel:**

> "She gets the same panel the agent gets — but in plain language. 'Applying the
> policy rules.' No tool names, no clause numbers, no confidence scores.
>
> Three hundred rupees. And that is not the default rule — the default would be
> ten percent of the shipment fee, which is two hundred and forty. Her contract
> replaces **both** the threshold and the amount.
>
> One more thing. If she asks about ORD-1001 — Northstar's order — she's told
> it isn't visible, with no speculation about why. That's a filter in the
> query, not an instruction in the prompt. The model is never handed data it
> then has to be trusted to withhold."

---

## 4:45 – 5:00 · Close

**SAY:**

> "Fifty-seven tests, and none of them need an API key — because the decision
> layer is code. A wrong number here is a failing assertion, not an unlucky
> sample.
>
> If I kept going: Northstar's contract caps monthly credits at five thousand
> rupees, and nothing yet records what's already been issued. That's the one
> place this system can be right about a single credit and wrong about the
> account. I'd build that next."

---

# The questions, in order — copy-paste block

Employee tab:

```
Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.

On TKT-450 an agent charged Northstar a 250 rupee cancellation fee. Was that correct?

A pickup is three hours late because of carrier fault. Should the customer get a service credit?

TKT-505 is a P1 security incident and its SLA is breached. Prepare an escalation.
```

Customer tab (Sara Iyer / LumenWorks, with ORD-2002 opened):

```
Am I owed a credit on this order?
```

---

# Backup questions — if one misfires or you have spare time

All verified working:

| Question | What it shows |
|---|---|
| `TKT-502 says bulk upload fails for a 4,200 row CSV. What is actually going on?` | Retrieval finds KI-208 — the 5,000-row limit is real, but failures start near 3,000. A second wrong historical ticket (TKT-451) claimed the limit was 3,000. |
| `Show me every open ticket that is breaching` | Lists all accounts, runs the SLA engine per ticket, returns TKT-501 and TKT-505 — matching the board exactly. |
| `Is TKT-502 breaching its SLA?` | The Sunday case, through the assistant instead of the board. |
| `What does the bulk upload limit actually say?` | Single retrieval call, cites Product Operations Guide §1. |

**If a question hangs or errors:** say *"that's the upstream rate limit — the
figures are already computed, which is the point"* and switch to the SLA Board.
It needs no model at all.

---

# Facts you may be asked, with the numbers

| Thing | Value | Source |
|---|---|---|
| Snapshot (fixed, never wall-clock) | 2026-08-16 11:00 IST, a Sunday | workbook README sheet |
| Default cancellation fee | INR 250 after 30 minutes | SOP v4 §1 |
| Northstar cancellation | waived entirely before pickup | Northstar Agreement §2 |
| Default service credit | lower of INR 500 or 10% of fee, 2-hour threshold | SOP v4 §2 |
| LumenWorks credit | fixed INR 300, 4-hour threshold | LumenWorks Agreement §3 |
| Manager approval needed above | INR 1,000 | SOP v4 §3 |
| Northstar P1 first response | 15 minutes, 24×7 | Northstar Agreement §1 |
| Enterprise default P1 | 30 minutes, 24×7 | Support Policy v3 §3 |
| Bulk upload limit | 5,000 rows; failures above ~3,000 | Product Ops Guide §1, KI-208 |
| Northstar monthly credit cap | INR 5,000 (not yet tracked — say so) | Northstar Agreement §3 |
| Tests | 57, none require an API key | `pytest -q` |

---

# Three things not to do

1. **Don't go silent while an answer streams.** Every SAY block is sized to
   cover it.
2. **Don't read the rule chain aloud line by line.** Point at it, say what it
   proves, move on.
3. **Don't skip the Sunday.** It is the single best evidence that the system
   reasons about the data rather than pattern-matching it.
