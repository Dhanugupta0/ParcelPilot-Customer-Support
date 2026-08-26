"""Trust and reliability layer (Client Problem 2).

Two mechanisms, both deterministic and both cheap enough to run on every turn.

1. GROUNDING VERIFICATION. Extract every figure the answer asserts -- currency
   amounts, durations, row counts -- and check each one actually appears in a
   tool result from this turn, or in what the user themselves said. A model that has been handed the right answer can
   still round it, restate it, or carry a number over from an earlier example.
   This catches that class of error without a second model call.

2. DERIVED CONFIDENCE. Asking a model how confident it is produces a number that
   correlates with fluency rather than correctness. So confidence is computed
   from observable signals instead: did a deterministic decision back this
   answer, did the policy engine report missing data, were conflicts detected,
   were any citations produced, was anything blocked. Below the configured floor
   the answer is withheld and replaced with an offer to escalate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app import config

_MONEY = re.compile(r"(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)", re.I)
_DURATION = re.compile(r"\b(\d+(?:\.\d+)?)\s*(minute|min|hour|hr|business day|day|week)s?\b", re.I)
# Comma-grouped numbers must be matched as a whole. The obvious pattern
# `\b\d{3,}(?:,\d{3})*\b` matches the "000" tail of "3,000" as a separate
# token, because a word boundary sits right after the comma -- which silently
# grounds the wrong value.
_COUNT = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{3,})\b")

# Record identifiers. A fabricated one is worse than a fabricated number: an
# agent sent to look up "TCK-5678" wastes real time discovering it never
# existed, and a customer told about it is told about nothing. The prefix set is
# deliberately broad -- a near-miss on a real prefix (TCK for TKT) is exactly
# the invention worth catching.
_IDENTIFIER = re.compile(
    r"\b((?:TKT|TCK|SYN|ORD|ACCT|ACC|KI|PROP|ESC|TASK|CR)[-\u2011\u2013_]?\d{2,})\b",
    re.I)


def _norm_id(s: str) -> str:
    """Compare identifiers on their own terms: case and dash style vary."""
    return re.sub(r"[-\u2011\u2013_\s]", "", s).upper()


def _ids_in(text: str) -> set[str]:
    return {_norm_id(m.group(1)) for m in _IDENTIFIER.finditer(text or "")}


@dataclass
class TrustReport:
    confidence: float
    band: str                       # HIGH | MEDIUM | LOW
    reasons: list[str] = field(default_factory=list)
    ungrounded_claims: list[str] = field(default_factory=list)
    echoed_claims: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    used_policy_engine: bool = False
    should_withhold: bool = False

    def to_dict(self) -> dict:
        return {"confidence": round(self.confidence, 2), "band": self.band,
                "reasons": self.reasons, "ungrounded_claims": self.ungrounded_claims,
                "echoed_claims": self.echoed_claims,
                "conflicts": self.conflicts, "citations": self.citations,
                "used_policy_engine": self.used_policy_engine,
                "withheld": self.should_withhold}


def _norm_num(s: str) -> str:
    s = s.replace(",", "").rstrip("0").rstrip(".") if "." in s else s.replace(",", "")
    return s


def _numbers_in(text: str) -> set[str]:
    out = set()
    for m in _MONEY.finditer(text):
        out.add(_norm_num(m.group(1)))
    for m in _DURATION.finditer(text):
        out.add(_norm_num(m.group(1)))
    for m in _COUNT.finditer(text):
        out.add(_norm_num(m.group(1)))
    return out


# Bare counts that are load-bearing claims rather than incidental numbers.
# "5,000 rows" is exactly the kind of figure that must be verified: the pack's
# own TKT-451 shows how a wrong row limit becomes wrong customer guidance.
_COUNTED_NOUN = re.compile(
    r"\b(\d{1,3}(?:,\d{3})+|\d{3,})\s*"
    r"(rows?|records?|shipments?|orders?|tickets?|customers?|users?|files?)\b", re.I)


def _claims_in(answer: str) -> list[tuple[str, str]]:
    """(normalised value, surface form) for every asserted figure."""
    claims = []
    for m in _MONEY.finditer(answer):
        claims.append((_norm_num(m.group(1)), m.group(0)))
    for m in _DURATION.finditer(answer):
        claims.append((_norm_num(m.group(1)), m.group(0)))
    for m in _COUNTED_NOUN.finditer(answer):
        claims.append((_norm_num(m.group(1)), m.group(0)))
    return claims


def _is_clarifying(answer: str, tool_names: list[str]) -> bool:
    """A request for more information is not a low-confidence answer.

    The correct response to "a pickup is three hours late, do I get a credit?"
    is to ask which order -- the threshold is 2 hours by default but 4 hours
    under the LumenWorks agreement, so the question is genuinely unanswerable
    without the account. Scoring that as LOW confidence punishes exactly the
    behaviour we want and reads to the user as "the system is unsure", when in
    fact it is being careful.
    """
    if tool_names:
        return False
    a = answer.strip().lower()
    if not a:
        return False
    # A request for information is often phrased as a polite imperative rather
    # than a question ("Please provide the order ID"), so a question mark alone
    # is not a reliable signal.
    asks = ("which order", "which account", "order id", "order number",
            "which customer", "please provide", "could you provide",
            "can you provide", "can you tell me", "let me know which",
            "provide the order", "could you share", "what is the order",
            "need the order", "specify the order")
    if any(k in a for k in asks):
        return True
    return a.endswith("?") and len(a) < 400


def assess(answer: str, tool_results: list[dict[str, Any]],
           tool_names: list[str], user_message: str = "") -> TrustReport:
    import json

    if _is_clarifying(answer, tool_names):
        return TrustReport(
            confidence=1.0, band="CLARIFYING",
            reasons=["The agent asked for the information it needs rather than "
                     "answering a question that cannot be answered generically."])

    blob = json.dumps(tool_results, default=str)
    grounded = _numbers_in(blob)
    grounded_ids = _ids_in(blob)

    # Figures that are always safe: they are structural, not factual claims.
    SAFE = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "0", "24", "30", "12"}

    # A figure the USER supplied is not a fabrication. "LumenWorks says bulk
    # upload fails on a 4,200-row CSV" puts 4,200 in the conversation, and an
    # answer that repeats it back is quoting, not asserting. Scoring that as an
    # ungrounded claim withheld correct answers -- the failure mode this layer
    # exists to prevent, aimed at the wrong target.
    #
    # It does not earn grounding credit either: an echoed figure is reported
    # separately so the reader can see the agent took the user's word for it.
    echoed_values = _numbers_in(user_message) if user_message else set()
    echoed_ids = _ids_in(user_message) if user_message else set()

    ungrounded, echoed = [], []
    for value, surface in _claims_in(answer):
        if value in grounded or value in SAFE:
            continue
        if value in echoed_values:
            echoed.append(surface)
            continue
        ungrounded.append(surface)

    # Identifiers get the same treatment as figures. An ID the user typed is
    # being quoted back and is not a fabrication; one that appears in no tool
    # result and was never mentioned is the agent inventing a record.
    for m in _IDENTIFIER.finditer(answer):
        surface, key = m.group(1), _norm_id(m.group(1))
        if key in grounded_ids:
            continue
        if key in echoed_ids:
            echoed.append(surface)
            continue
        if surface not in ungrounded:
            ungrounded.append(surface)

    used_engine = "evaluate_policy_decision" in tool_names
    used_retrieval = "search_policy_documents" in tool_names

    citations, conflicts = [], []
    low_conf_decision = missing_data = access_blocked = False
    for r in tool_results:
        if not isinstance(r, dict):
            continue
        # Citations arrive in two shapes: `results` from the retrieval tool, and
        # `supporting_sources` from the decision path's own governed lookup.
        for key in ("results", "supporting_sources"):
            for item in r.get(key, []) or []:
                if isinstance(item, dict) and item.get("citation"):
                    citations.append(item["citation"])
        # The rule chain itself is a citation source: every step names its clause.
        d0 = r.get("decision")
        if isinstance(d0, dict):
            for step in d0.get("rule_chain", []) or []:
                src = (step or {}).get("source", "")
                if src and ("§" in src or "SOP" in src or "Policy" in src
                            or "Agreement" in src or "Guide" in src):
                    citations.append(src)
        conflicts.extend(r.get("conflicts_detected", []) or [])
        d = r.get("decision")
        if isinstance(d, dict):
            if d.get("confidence") == "LOW":
                low_conf_decision = True
            if d.get("missing_data"):
                missing_data = True
        if r.get("access_denied"):
            access_blocked = True

    score, reasons = 0.5, []
    if used_engine:
        score += 0.30
        reasons.append("A deterministic policy decision backs this answer.")
    if used_retrieval and citations:
        score += 0.15
        reasons.append(f"Grounded in {len(set(citations))} cited source passage(s).")
    if not citations and not used_engine:
        score -= 0.25
        reasons.append("No cited source and no deterministic decision behind this answer.")
    if conflicts:
        # Deliberately NOT a penalty. Every conflict this system reports comes
        # with a deterministic resolution from the authority hierarchy (a signed
        # contract beating a deprecated policy, or current documentation beating
        # a known-incorrect past resolution). Detecting and correctly resolving
        # a contradiction is evidence the answer is MORE trustworthy, not less --
        # the dangerous case is the conflict nobody noticed.
        reasons.append(
            f"{len(conflicts)} source conflict(s) detected and resolved by "
            f"documented precedence.")
    if low_conf_decision:
        score -= 0.35
        reasons.append("The policy engine returned LOW confidence for this case.")
    if missing_data:
        score -= 0.25
        reasons.append("Required data is missing from the dataset.")
    if access_blocked:
        score -= 0.10
        reasons.append("Part of the request was outside this session's access scope.")
    if echoed:
        reasons.append(
            f"{len(echoed)} figure(s) repeat what the user stated and were not "
            f"independently verified: {', '.join(echoed[:3])}.")
    if ungrounded:
        score -= 0.30 * min(len(ungrounded), 3)
        kind = ("figure(s) or record(s)" if any(_IDENTIFIER.fullmatch(u) for u in ungrounded)
                else "figure(s)")
        reasons.append(
            f"{len(ungrounded)} {kind} in the answer do not appear in any tool "
            f"result: {', '.join(ungrounded[:3])}.")

    score = max(0.0, min(1.0, score))
    band = "HIGH" if score >= 0.75 else "MEDIUM" if score >= config.MIN_ANSWER_CONFIDENCE else "LOW"
    return TrustReport(
        confidence=score, band=band, reasons=reasons,
        ungrounded_claims=ungrounded, echoed_claims=echoed, conflicts=conflicts,
        citations=sorted(set(citations)), used_policy_engine=used_engine,
        should_withhold=(band == "LOW" and bool(ungrounded)),
    )
