"""Proactive customer outreach.

The Signal Board tells the team what is wrong. This turns a subset of those
signals into drafted customer communications that a human batch-approves.

WHY THIS IS THE HARD DIRECTION
------------------------------
Answering a question wrongly is bad. Volunteering a wrong statement to a
customer who never asked is worse: they did not invite it, they cannot correct
the premise, and it arrives with ParcelPilot's name on it. So outreach is held
to a stricter standard than chat:

  * Every factual claim in a draft comes from the deterministic policy engine or
    a cited document. Entitlements are COMPUTED before the message is drafted,
    never described loosely ("you may be eligible for compensation").
  * Drafts are grounding-verified before a human ever sees them. A draft whose
    figures do not trace to a tool result is discarded, not shown.
  * Nothing sends without explicit human approval, through the same two-phase
    gate as every other state change.

SUPPRESSION IS THE PRODUCT
--------------------------
Deciding who NOT to contact is most of the value here, and it is where the
judgment lives:

  * Do not tell a customer about a problem they have already raised. Emailing
    "we noticed your bulk uploads are failing" to someone with an open ticket
    about exactly that reads as though nobody read their ticket.
  * Respect contractual coverage. LumenWorks' agreement excludes weekend and
    after-hours support; cold-contacting them on a Sunday about a problem they
    cannot get help with until Monday creates an angry customer, not a
    reassured one.
  * Do not contact the same account about the same thing twice.
  * Never volunteer an entitlement the engine could not establish.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic import BaseModel, Field

from app import config
from app.core import clock
from app.core.models import Account, Order
from app.core.principal import Perm, Principal
from app.data.repository import Repository
from app.ingest.contract_terms import terms_for
from app.ingest.corpus import all_chunks
from app.policy import engine as policy_engine
from app.retrieval.index import tokenize
from app.signals.detectors import build_signals

# How long before the same account may be contacted about the same topic again.
REPEAT_SUPPRESSION_DAYS = 7
_LEDGER = config.VAR_DIR / "outreach_ledger.jsonl"


class Kind:
    CREDIT_OFFER = "service_credit_offer"
    KNOWN_ISSUE = "known_issue_advisory"
    SLA_APOLOGY = "sla_breach_apology"


KIND_LABEL = {
    Kind.CREDIT_OFFER: "Proactive service credit",
    Kind.KNOWN_ISSUE: "Known-issue advisory",
    Kind.SLA_APOLOGY: "Missed-response apology",
}


class Draft(BaseModel):
    candidate_id: str
    kind: str
    kind_label: str
    account_id: str
    account_name: str
    plan: str
    csm: str | None = None
    trigger: str = ""                      # which signal produced this
    subject: str
    facts: list[str] = Field(default_factory=list)      # deterministic statements
    body: str = ""
    body_source: str = "template"          # template | llm_verified
    citations: list[str] = Field(default_factory=list)
    entitlement_inr: float | None = None
    priority: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    requires_manager_approval: bool = False
    evidence: list[dict] = Field(default_factory=list)


class Suppressed(BaseModel):
    account_id: str
    account_name: str
    kind: str
    reason: str
    detail: str
    retry_after: str | None = None


class OutreachPlan(BaseModel):
    generated_at: str
    drafts: list[Draft] = Field(default_factory=list)
    suppressed: list[Suppressed] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------
# Ledger — what we have already sent
# --------------------------------------------------------------------------

def _topic_key(account_id: str, kind: str, subject_hint: str) -> str:
    h = hashlib.sha1(f"{account_id}|{kind}|{subject_hint}".encode()).hexdigest()[:12]
    return h


def record_sent(account_id: str, kind: str, subject_hint: str, ref: str) -> None:
    with _LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "sent_at": clock.fmt(clock.now()),
            "account_id": account_id, "kind": kind,
            "topic_key": _topic_key(account_id, kind, subject_hint),
            "ref": ref,
        }) + "\n")


def _recent_topics() -> dict[str, str]:
    """topic_key -> when it was last sent, within the suppression window."""
    if not _LEDGER.exists():
        return {}
    out: dict[str, str] = {}
    cutoff = clock.now() - timedelta(days=REPEAT_SUPPRESSION_DAYS)
    for line in _LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        sent = clock.parse_dt((rec.get("sent_at") or "").replace(" IST", ""))
        if sent and sent >= cutoff:
            out[rec["topic_key"]] = rec["sent_at"]
    return out


# --------------------------------------------------------------------------
# Suppression
# --------------------------------------------------------------------------

@dataclass
class _Context:
    repo: Repository
    recent: dict[str, str] = field(default_factory=dict)


def _coverage_hold(account: Account) -> tuple[bool, str]:
    """Should we hold this until the customer's support hours resume?

    A contract that excludes weekend and after-hours cover means the customer
    cannot reach anyone about what we are about to tell them. Reaching out
    anyway manufactures anxiety with no route to resolution.
    """
    terms = terms_for(account.account_id)
    if not terms or not terms.coverage_exclusion:
        return False, ""
    if clock.is_within_business_hours(clock.now()):
        return False, ""
    resume = clock.next_business_open(clock.now())
    return True, clock.fmt(resume)


def _open_ticket_about(ctx: _Context, account_id: str, terms: set[str]) -> str | None:
    """Has this customer already raised this? Returns the ticket id if so."""
    for t in ctx.repo.list_tickets(account_id=account_id, only_open=True):
        blob = set(tokenize(f"{t.subject} {t.description}"))
        if len(blob & terms) >= 2:
            return t.ticket_id
    return None


# --------------------------------------------------------------------------
# Draft bodies
# --------------------------------------------------------------------------

def _template_body(kind: str, account: Account, facts: list[str],
                   entitlement: float | None, next_step: str) -> str:
    hi = f"Hi {account.account_name} team,"
    lines = [hi, ""]
    if kind == Kind.CREDIT_OFFER:
        lines.append("We spotted a problem with one of your recent shipments before "
                     "you had to tell us about it, and we want to put it right.")
    elif kind == Kind.KNOWN_ISSUE:
        lines.append("We wanted to let you know about an issue our team is already "
                     "tracking, and how to work around it in the meantime.")
    else:
        lines.append("We did not get back to you as quickly as we should have, and "
                     "we wanted to acknowledge that directly.")
    lines.append("")
    lines.extend(f"- {f}" for f in facts)
    lines.append("")
    if entitlement is not None:
        lines.append(f"We have applied a service credit of INR {entitlement:,.0f} to "
                     f"your account for this. You do not need to do anything.")
        lines.append("")
    lines.append(next_step)
    lines.append("")
    lines.append(f"— {account.csm or 'The ParcelPilot support team'}")
    return "\n".join(lines)


_TONE_PROMPT = """\
You are writing a short proactive support email from ParcelPilot to a B2B \
logistics customer.

Rewrite the facts below into a warm, direct, professional email of at most 130 \
words. Rules you must not break:

- Use ONLY the facts given. Do not add any number, date, timeframe, promise or \
  cause that is not present in them.
- Do not speculate about why it happened beyond what the facts say.
- Do not invent a resolution date or an apology for anything not listed.
- Keep the exact figures as written.
- Plain text. No subject line. No markdown.
"""


def _llm_body(kind: str, account: Account, facts: list[str],
              entitlement: float | None, next_step: str) -> tuple[str, str]:
    """LLM prose over a deterministic fact list, then grounding-verified.

    Returns (body, source). Falls back to the template on any failure or if the
    generated text asserts a figure the facts do not contain -- an unverifiable
    proactive email is not worth the tone improvement.
    """
    fallback = _template_body(kind, account, facts, entitlement, next_step)
    if not config.GROQ_API_KEY:
        return fallback, "template"
    try:
        from app.agent import trust
        from app.core.llm import (client, strip_reasoning,
                                  utility_reasoning_params)
        payload = {
            "customer": account.account_name,
            "situation": KIND_LABEL[kind],
            "facts": facts,
            "service_credit_inr": entitlement,
            "next_step": next_step,
            "sign_off": account.csm or "The ParcelPilot support team",
        }
        resp = client().chat.completions.create(
            model=config.GROQ_UTILITY_MODEL,
            messages=[{"role": "system", "content": _TONE_PROMPT},
                      {"role": "user", "content": json.dumps(payload)}],
            temperature=0.3, max_tokens=320,
            # The utility model is a reasoner that will otherwise spend this
            # entire 320-token budget thinking and return an empty body -- or
            # return the thinking itself, wrapped in <think>, as the email.
            extra_body=utility_reasoning_params(),
        )
        body = strip_reasoning(resp.choices[0].message.content or "")
        if not body:
            return fallback, "template"
        report = trust.assess(body, [{"facts": facts, "credit": entitlement}],
                              ["outreach_facts"])
        if report.ungrounded_claims:
            # The tone was better; the numbers were not. Ship the safe one.
            return fallback, "template_after_verification_failure"
        return body, "llm_verified"
    except Exception:                                             # noqa: BLE001
        return fallback, "template"


# --------------------------------------------------------------------------
# Candidate builders
# --------------------------------------------------------------------------

def _credit_candidates(ctx: _Context, plan: OutreachPlan) -> None:
    """Uncollected shipments where a credit is already provable."""
    for order in ctx.repo.list_orders():
        if order.pickup_occurred:
            continue
        delay = order.pickup_delay_minutes() or 0.0
        if delay < 120:
            continue
        account = ctx.repo.get_account(order.account_id)
        decision = policy_engine.decide_service_credit(order, account)

        if decision.outcome is not policy_engine.Outcome.ELIGIBLE:
            # Never volunteer an entitlement the engine could not establish.
            plan.suppressed.append(Suppressed(
                account_id=account.account_id, account_name=account.account_name,
                kind=Kind.CREDIT_OFFER, reason="Entitlement not established",
                detail=(f"{order.order_id}: engine returned {decision.outcome.value}. "
                        "Proactive contact would imply a credit we cannot substantiate.")))
            continue

        topic = _topic_key(account.account_id, Kind.CREDIT_OFFER, order.order_id)
        if topic in ctx.recent:
            plan.suppressed.append(Suppressed(
                account_id=account.account_id, account_name=account.account_name,
                kind=Kind.CREDIT_OFFER, reason="Already contacted",
                detail=f"{order.order_id} outreach was sent {ctx.recent[topic]}."))
            continue

        hold, resume = _coverage_hold(account)
        if hold:
            plan.suppressed.append(Suppressed(
                account_id=account.account_id, account_name=account.account_name,
                kind=Kind.CREDIT_OFFER, reason="Outside contractual support hours",
                detail=("Their agreement excludes weekend and after-hours cover, so "
                        "they could not reach anyone about this until support resumes."),
                retry_after=resume))
            continue

        facts = [
            f"Your shipment {order.order_id} was scheduled for pickup between "
            f"{clock.fmt(order.pickup_window_start)} and {clock.fmt(order.pickup_window_end)}.",
            f"The carrier ({order.carrier}) did not collect it, and has accepted fault.",
            f"At our last check it was {delay/60:.1f} hours past the scheduled window.",
        ]
        citations = [s.source for s in decision.rule_chain if "§" in (s.source or "")]
        body, src = _llm_body(Kind.CREDIT_OFFER, account, facts, decision.amount_inr,
                              "We are chasing the carrier for a new collection time and "
                              "will confirm as soon as it is booked.")
        plan.drafts.append(Draft(
            candidate_id=f"OUT-{topic}", kind=Kind.CREDIT_OFFER,
            kind_label=KIND_LABEL[Kind.CREDIT_OFFER],
            account_id=account.account_id, account_name=account.account_name,
            plan=account.plan.value, csm=account.csm,
            trigger="ORD-STUCK",
            subject=f"About your shipment {order.order_id} — and a service credit",
            facts=facts, body=body, body_source=src,
            citations=sorted(set(citations)),
            entitlement_inr=decision.amount_inr,
            priority=6.0 + min(delay / 120.0, 4.0),
            requires_manager_approval=decision.requires_manager_approval,
            warnings=[c.message for c in decision.caveats],
            evidence=[{"order_id": order.order_id, "carrier": order.carrier,
                       "late_by": clock.humanise_minutes(delay),
                       "credit_inr": decision.amount_inr}]))


def _known_issue_candidates(ctx: _Context, plan: OutreachPlan,
                            signals: list) -> None:
    """Accounts caught in a cluster that maps to a documented known issue."""
    ki_chunks = {}
    for c in all_chunks():
        if c.section.startswith("Known issue") and c.status != "RESOLVED":
            key = c.citation.split("–")[-1].strip()
            ki_chunks[c.citation] = c

    for sig in signals:
        if sig.kind != "complaint_cluster" or not sig.sources:
            continue
        source = sig.sources[0]
        chunk = ki_chunks.get(source)
        if chunk is None:
            continue
        ki_facts = _known_issue_facts(chunk.text)
        if not ki_facts:
            continue

        for account_id in sig.accounts:
            account = ctx.repo.get_account(account_id)
            terms = set(tokenize(sig.title))

            already = _open_ticket_about(ctx, account_id, terms)
            if already:
                # They have already told us. A cold advisory here reads as
                # though nobody read their ticket -- reply on it instead.
                plan.suppressed.append(Suppressed(
                    account_id=account_id, account_name=account.account_name,
                    kind=Kind.KNOWN_ISSUE, reason="Customer has already raised this",
                    detail=(f"Open ticket {already} covers the same issue. Respond "
                            f"there rather than sending a separate advisory.")))
                continue

            topic = _topic_key(account_id, Kind.KNOWN_ISSUE, source)
            if topic in ctx.recent:
                plan.suppressed.append(Suppressed(
                    account_id=account_id, account_name=account.account_name,
                    kind=Kind.KNOWN_ISSUE, reason="Already contacted",
                    detail=f"Advisory for this issue was sent {ctx.recent[topic]}."))
                continue

            hold, resume = _coverage_hold(account)
            if hold:
                plan.suppressed.append(Suppressed(
                    account_id=account_id, account_name=account.account_name,
                    kind=Kind.KNOWN_ISSUE,
                    reason="Outside contractual support hours",
                    detail="Their agreement excludes weekend and after-hours cover.",
                    retry_after=resume))
                continue

            ki_id = source.split("–")[-1].split("(")[0].strip() if "–" in source else source
            facts = [
                f"We are tracking a known issue ({ki_id}) that matches reports we have "
                f"seen from your team.",
                *ki_facts,
                "Our engineering team is actively investigating a permanent fix.",
            ]
            body, src = _llm_body(
                Kind.KNOWN_ISSUE, account, facts, None,
                "You do not need to raise a ticket for this — we will update you when "
                "the fix ships.")
            plan.drafts.append(Draft(
                candidate_id=f"OUT-{topic}", kind=Kind.KNOWN_ISSUE,
                kind_label=KIND_LABEL[Kind.KNOWN_ISSUE],
                account_id=account_id, account_name=account.account_name,
                plan=account.plan.value, csm=account.csm,
                trigger=sig.signal_id,
                subject=f"Known issue affecting your account — {ki_id}",
                facts=facts, body=body, body_source=src,
                citations=[source], priority=4.0 + min(len(sig.accounts), 3),
                evidence=[{"signal": sig.signal_id, "tickets_in_cluster": len(sig.evidence),
                           "accounts_affected": len(sig.accounts)}]))


# Phrases that mark a sentence as guidance for a ParcelPilot agent rather than
# information for the customer. Detected explicitly rather than inferred: the
# cost of mailing an internal directive to a customer is high, and the cost of
# dropping one borderline sentence from an advisory is nearly zero.
_INTERNAL_MARKERS = (
    "telling a customer", "tell a customer", "tell the customer",
    "informing a customer", "inform the customer", "before telling",
    "do not use", "do not tell", "do not promise", "agent should",
    "unless evidence", "internal", "verify the carrier status",
)


def _known_issue_facts(text: str) -> list[str]:
    """Turn a known-issue block into precise, customer-safe facts.

    Two failure modes, both hit while building this:

    1. Line-based extraction is wrong -- these PDFs wrap mid-sentence, so
       splitting on newlines produced a truncated fragment that dropped the
       threshold entirely, and a model handed that fragment inverted the
       numbers: it told the customer the problem was uploads ABOVE the 5,000
       limit, when the documented issue is failures above ~3,000 DESPITE the
       limit being 5,000. That is exactly the mistake TKT-451 made.

    2. Known-issue documentation mixes customer-facing symptoms with internal
       instructions to support staff. KI-211 ends with "Before telling a
       customer that a pickup did not occur, verify the carrier status" -- that
       is guidance for an agent, and mailing it verbatim to the customer is
       both nonsensical and a small internal-process leak. Directives aimed at
       staff are stripped, not paraphrased.
    """
    body_lines = [ln for ln in text.splitlines()
                  if not re.match(r"^\s*(KI-\d+|Opened:|Status:)", ln.strip())]
    flat = " ".join(" ".join(body_lines).split())
    sentences = [x.strip() for x in re.split(r"(?<=\.)\s+", flat) if x.strip()]

    facts: list[str] = []
    for x in sentences:
        low = x.lower()
        if any(marker in low for marker in _INTERNAL_MARKERS):
            continue
        if len(x) < 25:
            continue
        facts.append(x.rstrip(".") + ".")
    return facts[:3]


# --------------------------------------------------------------------------

def build_outreach(principal: Principal) -> OutreachPlan:
    principal.require(Perm.VIEW_SIGNALS, "view proactive outreach")
    repo = Repository(principal)
    ctx = _Context(repo=repo, recent=_recent_topics())
    plan = OutreachPlan(generated_at=clock.fmt(clock.now()))

    board = build_signals(principal)
    _credit_candidates(ctx, plan)
    _known_issue_candidates(ctx, plan, board.signals)

    plan.drafts.sort(key=lambda d: -d.priority)
    plan.stats = {
        "drafts_ready": len(plan.drafts),
        "suppressed": len(plan.suppressed),
        "accounts_reached": len({d.account_id for d in plan.drafts}),
        "credits_offered_inr": sum(d.entitlement_inr or 0 for d in plan.drafts),
        "needs_manager_approval": sum(1 for d in plan.drafts if d.requires_manager_approval),
    }
    return plan


def get_draft(principal: Principal, candidate_id: str) -> Draft | None:
    for d in build_outreach(principal).drafts:
        if d.candidate_id == candidate_id:
            return d
    return None
