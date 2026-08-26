"""Default (non-contract) rules, parsed from the supplied PDFs.

Every number the engine uses -- the 30-minute free-cancellation window, the
INR 250 fee, the 2-hour credit threshold, the INR 500 / 10% formula, the
INR 1,000 manager-approval line, and the whole plan x severity SLA matrix --
is read out of the source documents at boot, together with the clause it came
from.

Nothing is hard-coded, for two reasons. The brief warns that the system may be
tested with other records from the same pack, and a support policy is exactly
the kind of document that gets revised. Swap in Support Policy v4 and the engine
follows it.

`validate()` asserts at startup that every rule was located. A silent parse
failure that quietly falls back to a guessed default is the single most
dangerous failure mode for a system like this, so we refuse to start instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from app.core.clock import SLATarget, parse_sla_target
from app.ingest.corpus import (TIER_CONTEXT_ONLY, Chunk, all_chunks, corpus)


@dataclass(frozen=True)
class Rule:
    """A parsed default with its provenance."""
    value: float | str
    citation: str
    quote: str

    def cite(self) -> str:
        return f"{self.citation}: \"{self.quote.strip()}\""


def _authoritative_chunks() -> list[Chunk]:
    """DEPRECATED material is excluded here, not filtered later.

    Support Policy v2 is retained in the pack and states Enterprise P1 = 1 hour.
    It must never be able to reach the engine, so it is dropped at the source.
    """
    return [c for c in all_chunks() if c.authority_tier < TIER_CONTEXT_ONLY
            and c.status != "DEPRECATED"]


def _search(pattern: str, *, doc_type: str | None = None,
            section_hint: str | None = None, flags=re.I) -> tuple[re.Match, Chunk] | None:
    for ch in _authoritative_chunks():
        if doc_type and ch.doc_type != doc_type:
            continue
        if section_hint and section_hint.lower() not in ch.section.lower():
            continue
        m = re.search(pattern, ch.text, flags)
        if m:
            return m, ch
    return None


def _rule(pattern: str, group: int = 1, *, cast=float, **kw) -> Rule | None:
    hit = _search(pattern, **kw)
    if not hit:
        return None
    m, ch = hit
    raw = m.group(group)
    val = cast(str(raw).replace(",", "")) if cast is float else cast(raw)
    line = m.group(0).strip()
    return Rule(value=val, citation=ch.citation, quote=re.sub(r"\s+", " ", line)[:220])


@dataclass
class PolicyDefaults:
    # Cancellation (SOP v4 s1)
    free_cancellation_window_minutes: Rule
    cancellation_fee_inr: Rule
    # Failed-pickup credits (SOP v4 s2)
    credit_threshold_hours: Rule
    credit_cap_inr: Rule
    credit_percent_of_fee: Rule
    # Approval (SOP v4 s3)
    manager_approval_above_inr: Rule
    # Support Policy v3 s3 -- plan x severity first-response matrix
    sla_matrix: dict[str, dict[str, str]]
    sla_citation: str
    # Support Policy v3 s2 -- severity definitions, used to explain triage
    severity_definitions: dict[str, str]
    severity_citation: str

    def sla_target(self, plan: str, severity: str) -> tuple[SLATarget, str] | None:
        row = self.sla_matrix.get(plan)
        if not row:
            return None
        raw = row.get(severity.upper())
        if not raw:
            return None
        return parse_sla_target(raw), self.sla_citation

    def validate(self) -> None:
        missing = [k for k in (
            "free_cancellation_window_minutes", "cancellation_fee_inr",
            "credit_threshold_hours", "credit_cap_inr", "credit_percent_of_fee",
            "manager_approval_above_inr") if getattr(self, k) is None]
        if missing:
            raise RuntimeError(
                "Policy ingest failed - these rules were not found in the supplied "
                f"documents: {missing}. Refusing to start rather than fall back to "
                "guessed defaults.")
        for plan in ("Enterprise", "Growth", "Standard"):
            row = self.sla_matrix.get(plan)
            if not row or not all(s in row for s in ("P1", "P2", "P3")):
                raise RuntimeError(f"SLA matrix incomplete for plan {plan!r}: {row}")


def _parse_sla_matrix() -> tuple[dict[str, dict[str, str]], str]:
    """Read the plan x severity table out of the CURRENT support policy."""
    for doc in corpus():
        if doc.doc_type != "policy" or doc.status == "DEPRECATED":
            continue
        for table in doc.tables:
            header = [h.strip() for h in table[0]]
            if not header or "plan" not in header[0].lower():
                continue
            sev_cols = {i: h.upper() for i, h in enumerate(header)
                        if re.fullmatch(r"P[123]", h.strip().upper())}
            if not sev_cols:
                continue
            matrix: dict[str, dict[str, str]] = {}
            for row in table[1:]:
                plan = (row[0] or "").strip()
                if not plan:
                    continue
                matrix[plan] = {sev: (row[i] or "").strip()
                                for i, sev in sev_cols.items() if (row[i] or "").strip()}
            if matrix:
                short = re.sub(r"^ParcelPilot\s*", "", doc.title).strip()
                return matrix, f"{short} §3 (Default first-response targets)"
    return {}, ""


def _parse_severity_definitions() -> tuple[dict[str, str], str]:
    hit = _search(r"P1\s*[-–]\s*Critical", doc_type="policy", section_hint="Severity")
    if not hit:
        return {}, ""
    _, ch = hit
    defs = {}
    for m in re.finditer(r"(P[123])\s*[-–]\s*([A-Za-z]+):\s*(.+?)(?=\n\s*-?\s*P[123]\s*[-–]|\Z)",
                         ch.text, re.DOTALL):
        defs[m.group(1)] = re.sub(r"\s+", " ", f"{m.group(2)}: {m.group(3)}").strip()
    return defs, ch.citation


@lru_cache(maxsize=1)
def defaults() -> PolicyDefaults:
    matrix, sla_cite = _parse_sla_matrix()
    sev_defs, sev_cite = _parse_severity_definitions()
    d = PolicyDefaults(
        free_cancellation_window_minutes=_rule(
            r"no fee within\s+(\d+)\s*minutes", doc_type="sop", section_hint="cancellation"),
        cancellation_fee_inr=_rule(
            r"charge\s+INR\s*([\d,]+)", doc_type="sop", section_hint="cancellation"),
        credit_threshold_hours=_rule(
            r"more than\s+(\d+(?:\.\d+)?)\s*hours past the end", doc_type="sop"),
        credit_cap_inr=_rule(
            r"lower of\s+INR\s*([\d,]+)", doc_type="sop"),
        credit_percent_of_fee=_rule(
            r"or\s+(\d+(?:\.\d+)?)\s*%\s*of the shipment fee", doc_type="sop"),
        manager_approval_above_inr=_rule(
            r"above\s+INR\s*([\d,]+)\s*requires manager approval", doc_type="sop"),
        sla_matrix=matrix, sla_citation=sla_cite,
        severity_definitions=sev_defs, severity_citation=sev_cite,
    )
    d.validate()
    return d
