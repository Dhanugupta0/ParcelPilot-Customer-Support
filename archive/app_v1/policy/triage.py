"""Severity triage for support tickets.

Deliberately rule-based rather than LLM-based, for three reasons:

* It must be explainable. "P1 because it matches 'suspected credential exposure'
  in Support Policy v3 s2" is auditable; "the model said P1" is not.
* It must be stable. The Signal Board re-ranks the queue on every refresh, and a
  queue that reshuffles because of sampling noise destroys trust faster than one
  that is occasionally wrong in a predictable way.
* It runs over every ticket on every refresh. Rules are free.

The signal patterns are anchored to the wording of the severity definitions in
the CURRENT policy, and each match reports which definition phrase it maps to.
An LLM pass can refine a borderline call later; the rules stay as the floor.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.core.models import Severity, Ticket
from app.policy.rules import defaults


class Signal(BaseModel):
    pattern: str
    matched: str
    maps_to: str


class TriageResult(BaseModel):
    severity: Severity
    rationale: str
    signals: list[Signal] = Field(default_factory=list)
    policy_definition: str = ""
    policy_source: str = ""
    confidence: str = "HIGH"
    ambiguous: bool = False


# (regex, severity, phrase from the policy's own severity definition)
_RULES: list[tuple[str, Severity, str]] = [
    # --- P1: complete outage -------------------------------------------------
    (r"\b(all|every|entire|no)\b[^.]{0,60}\b(user|shipment|order|request)s?\b[^.]{0,60}"
     r"\b(fail\w*|error\w*|down|blocked|unable|cannot|can't)\b",
     Severity.P1, "Complete production outage preventing all shipment creation"),
    (r"\b(complete|total|full)\s+(outage|failure|downtime)\b|\bproduction\s+outage\b",
     Severity.P1, "Complete production outage preventing all shipment creation"),
    (r"\b(all|every)\b[^.]{0,40}\bshipment creation\b[^.]{0,40}\bfail\w*\b",
     Severity.P1, "Complete production outage preventing all shipment creation"),
    (r"\bhttp\s*5\d\d\b|\b5\d\d\s+(error|response)\b",
     Severity.P1, "Event causing immediate material business risk with no workaround"),
    # --- P1: security --------------------------------------------------------
    (r"\b(api[\s_-]?key|secret|credential|password|token|private key)\b[^.]{0,80}"
     r"\b(expos|leak|publish|public|screenshot|shared|posted|compromis)",
     Severity.P1, "Confirmed security incident or suspected credential exposure"),
    (r"\b(security\s+(incident|breach)|data\s+breach|unauthori[sz]ed\s+access)\b",
     Severity.P1, "Confirmed security incident or suspected credential exposure"),
    # --- P2: major feature degraded -----------------------------------------
    (r"\b(bulk upload|api|integration|webhook|label|tracking|billing)\b[^.]{0,60}"
     r"\b(fail\w*|error\w*|broken|not working|unavailable|timeout|timing out)\b",
     Severity.P2, "Major feature unavailable or materially degraded"),
    (r"\b(degraded|intermittent|partially|slow|delays?)\b[^.]{0,50}"
     r"\b(service|performance|upload|sync|feature)\b",
     Severity.P2, "Major feature unavailable or materially degraded"),
    (r"\bfail\w* (?:at|around|after)\b|\bkeeps? failing\b|\breaches?\b[^.]{0,30}\band fail\w*\b",
     Severity.P2, "Major feature unavailable or materially degraded"),
    # --- P3: how-to / config -------------------------------------------------
    (r"\bhow (?:do|can|would|to)\b|\bwhere (?:do|can)\b|\bis it possible\b",
     Severity.P3, "How-to question or configuration request"),
    (r"\b(change|update|replace|add|remove)\b[^.]{0,40}"
     r"\b(contact|email|address|user|setting|preference|name)\b",
     Severity.P3, "Configuration request"),
]

# A workaround existing is the explicit P1 -> P2 discriminator in the policy.
#
# The subtlety that matters: a workaround must be another way to do THE THING
# THAT IS BROKEN, not merely some unrelated function that still happens to work.
# TKT-501 is the case in point -- "Every user gets HTTP 500 when creating any
# shipment. Existing shipments can still be viewed." Being able to view is not a
# workaround for being unable to create, and treating it as one silently demotes
# a genuine P1 outage to P2, which on the Northstar agreement is the difference
# between a 15-minute and a 1-hour target.
#
# So we require the workaround clause to name an action from the same family as
# the failure before it is allowed to demote anything.
_WORKAROUND = re.compile(
    r"\b(work(?:ing)?\s*around|workaround|still works?|works? fine|one[- ]by[- ]one|"
    r"individually|can still|able to|alternative)\b", re.I)

# Verbs describing the capability that is failing.
_FAILING_ACTION = re.compile(
    r"\b(creat\w*|book\w*|upload\w*|generat\w*|submit\w*|process\w*|"
    r"send\w*|import\w*|sync\w*|dispatch\w*)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


def _real_workaround(text: str) -> re.Match | None:
    """A workaround only counts if its sentence also names the failing action."""
    for sentence in _sentences(text):
        m = _WORKAROUND.search(sentence)
        if m and _FAILING_ACTION.search(sentence):
            return m
    return None


def triage(ticket: Ticket) -> TriageResult:
    d = defaults()
    text = f"{ticket.subject}. {ticket.description}"
    low = text.lower()

    hits: dict[Severity, list[Signal]] = {}
    for pattern, sev, maps_to in _RULES:
        m = re.search(pattern, low, re.I)
        if m:
            hits.setdefault(sev, []).append(Signal(
                pattern=pattern[:48] + "...", matched=m.group(0)[:90], maps_to=maps_to))

    if not hits:
        return TriageResult(
            severity=Severity.P3,
            rationale=("No P1 or P2 signal matched, so this defaults to P3 (minor defect, "
                       "how-to question, or limited operational impact)."),
            policy_definition=d.severity_definitions.get("P3", ""),
            policy_source=d.severity_citation,
            confidence="MEDIUM", ambiguous=True)

    severity = min(hits, key=lambda s: s.value)   # P1 < P2 < P3 lexicographically
    signals = hits[severity]
    ambiguous = False
    rationale_extra = ""

    # Policy v3 s2 draws the P1/P2 line at whether a workaround exists. A stated
    # workaround demotes an outage-shaped ticket -- unless it is a security
    # event, where a workaround is irrelevant.
    if severity is Severity.P1:
        is_security = any("credential" in s.maps_to or "security" in s.maps_to.lower()
                          for s in signals)
        wm = _real_workaround(text)
        if not is_security and wm:
            severity = Severity.P2
            signals = hits.get(Severity.P2, []) + [Signal(
                pattern="workaround-present", matched=wm.group(0),
                maps_to="core operations remain possible or a workaround exists")]
            rationale_extra = (" The ticket describes a working alternative, which the "
                               "policy treats as the P1/P2 discriminator.")

    if len(hits) > 1 and severity in hits and len(hits[severity]) == 1:
        ambiguous = True

    quoted = "; ".join(f"\"{s.matched}\" → {s.maps_to}" for s in signals[:3])
    return TriageResult(
        severity=severity,
        rationale=f"Matched {len(signals)} signal(s): {quoted}.{rationale_extra}",
        signals=signals,
        policy_definition=d.severity_definitions.get(severity.value, ""),
        policy_source=d.severity_citation,
        confidence="HIGH" if not ambiguous else "MEDIUM",
        ambiguous=ambiguous)
