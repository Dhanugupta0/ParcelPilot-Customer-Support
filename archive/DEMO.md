# Demo video script — ~5 minutes

Target: 5:00. Timings are generous; rehearse once and trim the architecture
section if you run long. **Record in the Employee portal by default** and open
the Customer portal only for the access-control beat.

Before recording: `pytest` (so the green result is on screen), server running,
browser at `localhost:8000` showing the sign-in screen. Sign in as
`Rohit (Support Agent)` via **Employee**.

Two things worth pointing at while you sign in, in one sentence each:

- The portal choice is a real split, not a permissions filter. The customer
  screen has no Signal Board tab to grey out, because a customer has no business
  knowing one exists.
- Answers stream token by token, dimmed, until the trust assessment has run.
  Streamed text is a draft; the verified answer replaces it. If the trust layer
  withholds an answer, the draft never survives on screen.

---

## 0:00–0:40 — The problem, framed by the data (40s)

> "ParcelPilot's data pack is deliberately imperfect, and that's the whole
> assessment. Let me show you the trap that shaped every decision I made."

**Show:** README trap table on screen.

> "Northstar asks to cancel ORD-1001, two hours after booking. The SOP says a
> ₹250 fee applies after thirty minutes. And there's a closed ticket, TKT-450,
> where an agent told Northstar exactly that. So the highest-scoring retrieval
> result and the general policy agree — and they're both wrong, because
> Northstar's contract waives the fee regardless of elapsed time.
>
> A naive RAG system answers this confidently and incorrectly. That's the failure
> mode this system is built to prevent."

---

## 0:40–1:40 — Architecture (60s)

**Show:** the architecture diagram in the README.

> "Four tool categories behind one access-control gate. Three decisions matter.
>
> **First — the model doesn't decide anything with a number attached.** Fees,
> credits and SLA deadlines are computed by a deterministic policy engine in
> plain Python, from rules parsed out of the source PDFs at boot. It returns a
> rule chain: every step names and quotes the clause it applied. The model's job
> is to explain a decision it didn't make. It can't hallucinate ₹250 because it's
> never asked to produce a number.
>
> **Second — authority is metadata, not prompt text.** Support Policy v3 states
> the precedence rule itself: contract first, then current policy, then product
> docs, historical tickets are context only. That's encoded as a tier on every
> chunk at ingest. Deprecated documents are *removed* from the answer path, not
> down-ranked — because a down-ranked wrong answer still occasionally wins.
>
> **Third — confirmation is structural.** The model has a `propose_action` tool
> and no commit tool at all. The only code path that changes state is reachable
> from the Confirm button. A prompt injection saying 'skip confirmation' produces
> a proposal a human still has to approve."

---

## 1:40–2:40 — The core demo: multi-step + conflict (60s)

**Type:** `Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.`

**Point at the tool trace as it fills:**

> "Watch the trace. It looks up the order, resolves the account, searches the
> documents, then calls the policy engine — four tools, one question."

**When the answer lands, point at the conflict card:**

> "Correct answer: no fee, and it names the contract clause. But look at this —
> the system detected that TKT-450 told this same customer the opposite, and
> surfaced it rather than inheriting it. It's saying: *this past answer exists,
> it's wrong, and here's what governs instead.*
>
> That's the trust feature. The excluded sources are still searched, specifically
> so the system can notice a contradiction and tell you about it."

**Point at the confidence pill:**

> "HIGH confidence — and that's derived, not self-reported. Asking a model how
> confident it is measures fluency. This is computed from whether a deterministic
> decision backed the answer, whether citations were produced, whether data was
> missing."

---

## 2:40–3:20 — Contrast + the Sunday (40s)

**Type:** `Same question for LumenWorks and ORD-2001.`

> "Same shape of question, opposite answer — ₹250 applies, because LumenWorks'
> agreement *explicitly declines* a waiver. The system isn't pattern-matching the
> previous answer; it's re-resolving the contract."

**Type:** `Is TKT-505 within its first-response SLA?`

> "Breached by two hours. And here's my favourite detail — the dataset snapshot
> is a **Sunday**. This pack writes targets in two different units: '30 minutes,
> 24x7' and '4 business hours'. The 24x7 ones have been running all weekend; the
> business-hours ones haven't started. Wall-clock arithmetic flags the wrong
> tickets as on fire. There are exactly two real breaches, and this is one."

---

## 3:20–4:00 — Access control + confirmation (40s)

**Sign out, then sign in through the Customer portal as `Sara (LumenWorks)`.
Type:** `What's the status of ORD-1001?`

> "Refused — and notice the wording. It's identical to what you'd get for an
> order that doesn't exist. That's deliberate: if 'access denied' and 'not found'
> read differently, a customer can enumerate other tenants' order IDs by watching
> which error comes back. Enforcement is in the data layer, not the prompt."

**Sign out and back in as `Rohit (Support Agent)` via Employee. Type:**
`Escalate TKT-505 to the security team.`

> "It prepares the escalation and stops. Nothing has been created. The preview
> shows exactly what will happen, and it waits."

**Click Confirm.**

> "Now it executes, with a reference. Two-phase, idempotent, and the permission is
> re-checked at the write — not just when the action was offered."

---

## 4:00–4:35 — Signal Board (35s)

**Click the Signal Board tab.**

> "Problem one: a reactive chatbot only helps once someone asks. This runs five
> detectors across all support activity.
>
> Top signal — eight similar bulk-upload complaints across three accounts, rising
> from a zero baseline, automatically mapped to known issue KI-208. Which means
> the workaround already exists and can go out proactively instead of being
> rediscovered eight times.
>
> Below that, the two real SLA breaches, and an uncollected shipment that's
> accruing delay and is a probable service-credit case the customer hasn't raised
> yet. Every signal carries its evidence and a recommended action that routes
> through the same confirmation gate."

---

## 4:35–5:05 — Outreach: acting on the signals (30s)

**Click the Outreach tab.**

> "Detection is half of it. This turns signals into drafted customer messages —
> the credit already computed, the workaround already attached.
>
> But look at what's held back, because that's the actual product. LumenWorks
> isn't being contacted about the bulk-upload issue — they already opened a
> ticket about it, and emailing 'we noticed your uploads are failing' to someone
> who already told us reads as though nobody read their ticket. And their credit
> offer is held until Monday nine a.m., because their contract excludes weekend
> support — telling them about a problem they can't get help with until Monday
> just creates anxiety.
>
> Nothing sends without confirmation, same gate as everything else."

---

## 5:05–5:30 — Proof and close (25s)

**Show the terminal with the passing test run.**

> "Sixty-six tests, about two seconds, no API key needed — because the
> parts that have to be right are decided by code, not by a model. Every trap in
> that opening table is a test case, plus adversarial coverage for prompt
> injection and cross-account probing.
>
> That's the thesis: the language model routes and explains. Everything with a
> number, an entitlement or a side effect attached is deterministic, cited, and
> testable. That's what makes it trustworthy enough to actually deploy."

---

## If you have 30 seconds spare

Best cuts to add, in priority order:

1. **The abstention beat.** Ask a customer session *"How do I change the billing
   contact?"* — nothing in the pack covers it, and the system says so and offers
   to escalate rather than inventing steps. Strong, and fast to show.
2. **The KI-211 caveat.** In the ORD-1001 answer, point at the caveat: it's a
   SwiftShip order inside its pickup window, and webhooks lag up to 20 minutes,
   so `BOOKED` might be stale — verify before cancelling.
3. **The audit log tab.** One sentence: every tool call, proposal and denial.

## Things to avoid saying

- Don't call it "RAG" — the point is that it's more than retrieval.
- Don't over-explain the tech stack; the evaluators care about decisions.
- Don't apologise for the mocked auth — it's what the brief asked for.
