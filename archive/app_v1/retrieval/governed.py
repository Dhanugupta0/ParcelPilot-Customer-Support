"""Governed retrieval: authority, scope, and conflict detection.

Three guarantees, all enforced here rather than requested in a prompt:

1. SCOPE. A customer session can only ever retrieve its own agreement. Another
   tenant's contract is removed from the candidate set before ranking, so no
   amount of prompt injection can surface it -- the text never enters the
   context window.

2. AUTHORITY. Deprecated and context-only material (Support Policy v2,
   historical ticket resolutions, resolved known issues) is excluded from the
   answer path. It is not merely down-ranked. A down-ranked wrong answer is
   still a wrong answer that occasionally wins.

3. TRANSPARENCY. Everything excluded is reported back with the reason. "Support
   Policy v2 matched your question but was excluded because it is deprecated" is
   more trust-building than silently returning the right answer, and it is what
   lets a support agent audit the system's reasoning.

The conflict lane is the interesting part. Excluded sources are still SEARCHED,
precisely so the system can notice that a historical ticket or a superseded
policy says something different from the authoritative answer, and say so out
loud. That turns the pack's booby traps from a liability into a feature.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.models import Ticket
from app.core.principal import Perm, Principal
from app.data.repository import Repository
from app.ingest.corpus import TIER_CONTEXT_ONLY, TIER_LABEL, Chunk
from app.retrieval.index import index, tokenize


@dataclass
class Exclusion:
    citation: str
    reason: str
    detail: str = ""


@dataclass
class Conflict:
    """An authoritative source and a non-authoritative one disagree."""
    topic: str
    authoritative: str          # citation that wins
    authoritative_says: str
    conflicting: str            # citation that loses
    conflicting_says: str
    resolution: str
    severity: str = "warning"


@dataclass
class RetrievalResult:
    query: str
    results: list[Chunk] = field(default_factory=list)
    excluded: list[Exclusion] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    scope_note: str = ""

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "scope": self.scope_note,
            "results": [c.to_dict() for c in self.results],
            "excluded_sources": [
                {"citation": e.citation, "reason": e.reason, "detail": e.detail}
                for e in self.excluded],
            "conflicts_detected": [c.__dict__ for c in self.conflicts],
            "guidance": (
                "Answer only from `results`. Sources in `excluded_sources` were "
                "removed deliberately and must not be used as authority. If "
                "`conflicts_detected` is non-empty, tell the user plainly which "
                "source governs and why."),
        }


# --- amounts / durations we can compare across sources ---------------------
_MONEY = re.compile(r"INR\s*([\d,]+)", re.I)
_DURATION = re.compile(r"(\d+)\s*(minute|hour|day)s?", re.I)


def _numbers(text: str) -> tuple[set[str], set[str]]:
    money = {m.group(1).replace(",", "") for m in _MONEY.finditer(text)}
    dur = {f"{m.group(1)}{m.group(2).lower()}" for m in _DURATION.finditer(text)}
    return money, dur


# Two sources only "disagree" if they are talking about the same thing. Raw
# token overlap is far too loose -- every policy document in this pack shares
# vocabulary, so an unconstrained comparison pairs a cancellation ticket with
# the known-issues section and reports a conflict that does not exist. Noise
# here is worse than silence: a conflict panel that cries wolf is one the
# support team learns to dismiss.
_TOPIC_TERMS: dict[str, set[str]] = {
    "cancellation": {"cancel", "cancelled", "cancellation", "cancelling"},
    "service_credit": {"credit", "credits", "compensation", "refund"},
    "sla": {"p1", "p2", "p3", "severity", "response", "target", "targets",
            "escalation", "escalate", "critical"},
    "bulk_upload": {"bulk", "upload", "uploads", "csv", "rows", "row"},
    "pickup": {"pickup", "picked", "webhook", "booked", "collection"},
    "plan_entitlement": {"enterprise", "growth", "standard", "plan", "included"},
}


def _topics(text: str) -> set[str]:
    toks = set(tokenize(text))
    return {name for name, terms in _TOPIC_TERMS.items() if toks & terms}


def _topical_similarity(a: str, b: str) -> float:
    """Jaccard over content tokens, used only to pick the BEST pairing."""
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Numeric comparison alone is not enough. The single most consequential conflict
# in this pack is TKT-450 ("a INR 250 cancellation fee applied") against the
# Northstar agreement ("may cancel ... with no cancellation fee") -- and the
# contract states its position qualitatively, with no number to compare. So we
# also read the POLARITY of each source's assertion: does it impose the thing,
# or waive it? Opposite polarity on a shared topic is a conflict even when
# neither side quotes a figure.
_POLARITY_PATTERNS: dict[str, tuple[str, str]] = {
    # topic: (negating / waiving pattern, imposing / asserting pattern)
    "cancellation": (
        r"\b(no|without|waiv\w+|not charged|free)\b[^.]{0,40}\bfee\b"
        r"|\bfee\b[^.]{0,20}\bwaiv\w+",
        r"\b(charge|charged|fee applie\w*|fee of|applies)\b[^.]{0,25}"
        r"(INR|fee)|\bINR\s*[\d,]+\s*(cancellation\s*)?fee",
    ),
    "service_credit": (
        r"\b(no|not)\b[^.]{0,30}\b(credit|eligible)\b|\bnot eligible\b",
        r"\b(eligible|receives|is entitled|credit of)\b",
    ),
    "bulk_upload": (
        r"\bnot included\b|\bnot available\b|\bonly supports\b",
        r"\bavailable on\b|\bsupported\b|\bup to\b",
    ),
}


def _polarity(text: str, topic: str) -> int:
    """-1 waives/denies, +1 imposes/grants, 0 mixed or silent.

    Mixed deliberately returns 0. SOP v4 s1 says "charge INR 250 unless a
    customer agreement explicitly waives the cancellation fee" -- it asserts
    both sides, so it contradicts nobody and must not be flagged.
    """
    pats = _POLARITY_PATTERNS.get(topic)
    if not pats:
        return 0
    neg = bool(re.search(pats[0], text, re.I))
    pos = bool(re.search(pats[1], text, re.I))
    if neg and not pos:
        return -1
    if pos and not neg:
        return 1
    return 0


def _polarity_disagrees(a: str, b: str, topics: set[str]) -> str | None:
    for topic in sorted(topics):
        pa, pb = _polarity(a, topic), _polarity(b, topic)
        if pa and pb and pa != pb:
            return topic
    return None


def _numbers_disagree(a: str, b: str) -> bool:
    """Compare like with like: money against money, durations against durations."""
    a_money, a_dur = _numbers(a)
    b_money, b_dur = _numbers(b)
    if a_money and b_money and not (a_money & b_money):
        return True
    if a_dur and b_dur and not (a_dur & b_dur):
        return True
    return False


class GovernedRetriever:
    MAX_PER_DOC = 3

    def __init__(self, principal: Principal):
        self.p = principal
        self.repo = Repository(principal)

    # -- scope ---------------------------------------------------------------
    def _in_scope(self, ch: Chunk) -> tuple[bool, str]:
        """Contracts are visible only to their own account (or to staff)."""
        if ch.scoped_account_id is None:
            return True, ""
        if self.p.can(Perm.READ_ALL_ACCOUNTS):
            return True, ""
        if ch.scoped_account_id == self.p.account_id:
            return True, ""
        return False, "belongs to another customer's agreement"

    def search(self, query: str, *, top_k: int = 6,
               account_context: str | None = None) -> RetrievalResult:
        res = RetrievalResult(query=query)
        res.scope_note = self.p.scope_label()

        raw = index().search(query, top_k=18)
        kept: list[Chunk] = []
        context_only: list[Chunk] = []

        for ch, _score, _dbg in raw:
            ok, why = self._in_scope(ch)
            if not ok:
                # Not even reported by citation -- naming another tenant's
                # contract file is itself a small leak.
                continue

            # A staff member investigating one account should not have a
            # different customer's contract clause quoted back at them.
            if (account_context and ch.scoped_account_id
                    and ch.scoped_account_id != account_context):
                res.excluded.append(Exclusion(
                    citation=ch.citation,
                    reason="Out of account scope",
                    detail=f"Applies to {ch.scoped_account_id}, not {account_context}."))
                continue

            if ch.status == "DEPRECATED":
                res.excluded.append(Exclusion(
                    citation=ch.citation, reason="Superseded document",
                    detail="Marked DEPRECATED and explicitly retained for historical "
                           "reference only. Never valid as current authority."))
                context_only.append(ch)
                continue

            if ch.status == "RESOLVED":
                res.excluded.append(Exclusion(
                    citation=ch.citation, reason="Resolved known issue",
                    detail="The Product Operations Guide instructs that a resolved "
                           "issue must not be used to explain new incidents unless "
                           "evidence specifically matches it."))
                context_only.append(ch)
                continue

            if ch.authority_tier >= TIER_CONTEXT_ONLY:
                res.excluded.append(Exclusion(
                    citation=ch.citation, reason="Context only",
                    detail=TIER_LABEL[ch.authority_tier]))
                context_only.append(ch)
                continue

            kept.append(ch)

        # Rank order from the index is RELEVANCE order and is preserved.
        # The only reordering applied is a genuine relevance signal: when we know
        # which account the question is about, that account's own agreement is
        # more relevant to it than a general document, so it is pulled forward.
        if account_context:
            kept.sort(key=lambda c: 0 if c.scoped_account_id == account_context else 1)

        # Diversity cap: a multi-source question ("what does my contract say AND
        # what does the SOP say") is unanswerable if all six slots are filled by
        # one document. Cap per-document representation so the result set can
        # actually span the precedence chain.
        per_doc: dict[str, int] = {}
        diverse: list[Chunk] = []
        for ch in kept:
            n = per_doc.get(ch.doc_id, 0)
            if n >= self.MAX_PER_DOC:
                continue
            per_doc[ch.doc_id] = n + 1
            diverse.append(ch)
        res.results = diverse[:top_k]

        res.conflicts.extend(self._doc_conflicts(res.results, context_only))
        res.conflicts.extend(self._ticket_conflicts(query, res.results, account_context))
        return res

    # -- conflict detection --------------------------------------------------
    def _doc_conflicts(self, kept: list[Chunk],
                       context_only: list[Chunk]) -> list[Conflict]:
        """An excluded source contradicts an authoritative one on a shared topic.

        Emits at most ONE conflict per excluded source, paired with the single
        most topically similar authoritative chunk, and only when both sides
        share a topic tag and disagree on a comparable number.
        """
        out: list[Conflict] = []
        for loser in context_only:
            l_topics = _topics(loser.text)
            if not l_topics:
                continue
            best, best_sim = None, 0.0
            for winner in kept:
                if not (l_topics & _topics(winner.text)):
                    continue
                shared_topics = l_topics & _topics(winner.text)
                if not (_numbers_disagree(winner.text, loser.text)
                        or _polarity_disagrees(winner.text, loser.text, shared_topics)):
                    continue
                sim = _topical_similarity(winner.text, loser.text)
                if sim > best_sim:
                    best, best_sim = winner, sim
            if best is None or best_sim < 0.06:
                continue
            shared = ", ".join(sorted(l_topics & _topics(best.text)))
            out.append(Conflict(
                topic=f"{best.section} ({shared})",
                authoritative=best.citation,
                authoritative_says=best.text[:220].strip(),
                conflicting=loser.citation,
                conflicting_says=loser.text[:220].strip(),
                resolution=(f"{best.citation} governs. {loser.citation} is "
                            f"{loser.status.lower()} and carries no authority."),
            ))
        return out[:3]

    def _ticket_conflicts(self, query: str, kept: list[Chunk],
                          account_context: str | None = None) -> list[Conflict]:
        """Past resolutions that contradict current authority.

        The dataset README warns that some historical resolutions are wrong, and
        the pack contains two that are: TKT-450 contradicts the Northstar
        agreement on cancellation fees, and TKT-451 contradicts the Product
        Operations Guide on the bulk-upload row limit. A search for either topic
        ranks those tickets very highly -- that is the trap. We surface the
        contradiction rather than inheriting it.
        """
        if not self.p.can(Perm.READ_INTERNAL_FIELDS):
            return []   # customers never see historical resolutions at all
        out: list[Conflict] = []
        for t in self.repo.search_tickets(query, limit=8):
            if not t.historical_resolution:
                continue
            # A past answer given to one customer says nothing about another's
            # entitlements. When we know whose question this is, only that
            # account's history can contradict the answer we are about to give.
            if account_context and t.account_id != account_context:
                continue
            h_text = f"{t.subject} {t.historical_resolution}"
            h_topics = _topics(h_text)
            if not h_topics:
                continue
            best, best_sim = None, 0.0
            for ch in kept:
                if not (h_topics & _topics(ch.text)):
                    continue
                shared_topics = h_topics & _topics(ch.text)
                if not (_numbers_disagree(ch.text, t.historical_resolution)
                        or _polarity_disagrees(ch.text, h_text, shared_topics)):
                    continue
                sim = _topical_similarity(h_text, ch.text)
                if sim > best_sim:
                    best, best_sim = ch, sim
            if best is None or best_sim < 0.05:
                continue
            when = f" (closed {t.created_at:%d %b %Y})" if t.created_at else ""
            out.append(Conflict(
                topic=f"Past resolution on {t.ticket_id}",
                authoritative=best.citation,
                authoritative_says=best.text[:220].strip(),
                conflicting=f"{t.ticket_id}{when}",
                conflicting_says=t.historical_resolution.strip(),
                resolution=(
                    f"{best.citation} governs. The earlier answer on {t.ticket_id} is "
                    f"historical context only and appears to be incorrect; the dataset "
                    f"README warns that some past resolutions are wrong."),
                severity="high",
            ))
        return out[:2]
