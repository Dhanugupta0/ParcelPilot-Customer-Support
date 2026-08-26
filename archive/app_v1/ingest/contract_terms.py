"""Turn signed agreements into machine-readable, citable terms.

Why this module exists
----------------------
The naive design lets the LLM read the agreement PDF and work out the answer.
That fails in exactly the way ParcelPilot is worried about: it is unreproducible,
unauditable, and a model that misreads one clause invents a number with total
confidence.

So contracts are parsed ONCE into a terms sheet. Every extracted term carries
the clause it came from and the verbatim quote, and the deterministic policy
engine reads the terms sheet -- never the prose. The model's job is to explain
a decision it did not make.

In production this extraction would be LLM-assisted with a human sign-off step
before a terms sheet goes live, and versioned per contract amendment. The
rule-based extractor here produces the same artefact shape, so that upgrade is a
swap of one function rather than a redesign. `scripts/extract_terms.py` writes
the reviewed sheet to data/contract_terms.json.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field

from app.core.clock import SLATarget, parse_sla_target
from app.ingest.corpus import Chunk, all_chunks


class Clause(BaseModel):
    """One extracted term, permanently attached to its source."""
    value: Any
    citation: str
    quote: str

    def cite(self) -> str:
        return f"{self.citation}: \"{self.quote.strip()}\""


class ContractTerms(BaseModel):
    account_id: str
    document: str
    status: str = "ACTIVE"

    # Support SLA overrides (Support Policy v3 s1 lets an agreement replace these)
    sla: dict[str, Clause] = Field(default_factory=dict)     # "P1" -> target string
    coverage_exclusion: Clause | None = None                 # e.g. no weekend cover

    # Cancellation
    cancellation_fee_waived: Clause | None = None            # True / False
    # Failed-pickup service credits
    credit_threshold_hours: Clause | None = None
    credit_fixed_amount_inr: Clause | None = None
    credit_monthly_cap_inr: Clause | None = None

    def sla_target(self, severity: str) -> tuple[SLATarget, Clause] | None:
        c = self.sla.get(severity.upper())
        if not c:
            return None
        return parse_sla_target(str(c.value)), c

    def summary(self) -> dict:
        out = {"account_id": self.account_id, "document": self.document,
               "status": self.status,
               "sla_overrides": {k: v.value for k, v in self.sla.items()}}
        for name in ("coverage_exclusion", "cancellation_fee_waived",
                     "credit_threshold_hours", "credit_fixed_amount_inr",
                     "credit_monthly_cap_inr"):
            c = getattr(self, name)
            if c is not None:
                out[name] = {"value": c.value, "source": c.cite()}
        return out


# --------------------------------------------------------------------------
# Rule-based extraction
# --------------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;])\s+|\n", text)
    return [p.strip() for p in parts if p.strip()]


def _find_sentence(text: str, *needles: str) -> str:
    """First sentence containing all needles -- used as the verbatim quote."""
    for s in _sentences(text):
        low = s.lower()
        if all(n.lower() in low for n in needles):
            return s
    return text.strip().split("\n")[0][:240]


def _extract_from_chunks(account_id: str, chunks: list[Chunk]) -> ContractTerms:
    doc_title = chunks[0].doc_title if chunks else ""
    terms = ContractTerms(account_id=account_id, document=doc_title)

    for ch in chunks:
        text, low, cite = ch.text, ch.text.lower(), ch.citation

        # --- support SLA: lines shaped like "P1: 15 minutes, 24x7" ----------
        if "support term" in ch.section.lower() or "p1" in low:
            for m in re.finditer(r"\bP([123])\b\s*[:\-–]\s*([^\n.;]+)", ch.text):
                sev, raw = f"P{m.group(1)}", m.group(2).strip().rstrip(".")
                try:
                    parse_sla_target(raw)
                except ValueError:
                    continue
                terms.sla[sev] = Clause(value=raw, citation=cite,
                                        quote=f"{sev}: {raw}")
            if re.search(r"no\s+weekend|after[- ]hours", low):
                terms.coverage_exclusion = Clause(
                    value="no_weekend_or_after_hours",
                    citation=cite,
                    quote=_find_sentence(ch.text, "weekend"))

        # --- cancellation fee ---------------------------------------------
        if "cancel" in low:
            # An explicit refusal of a waiver must beat the generic waiver
            # pattern, otherwise LumenWorks s2 ("No special cancellation-fee
            # waiver applies") would be read as granting one.
            if re.search(r"no\s+special\s+cancellation[- ]fee\s+waiver|"
                         r"no\s+cancellation[- ]fee\s+waiver\s+applies", low):
                terms.cancellation_fee_waived = Clause(
                    value=False, citation=cite,
                    quote=_find_sentence(ch.text, "waiver"))
            elif re.search(r"(with\s+no|without\s+(?:a\s+)?|no)\s+cancellation\s+fee", low):
                terms.cancellation_fee_waived = Clause(
                    value=True, citation=cite,
                    quote=_find_sentence(ch.text, "cancellation fee"))

        # --- failed-pickup credits ----------------------------------------
        if "credit" in low:
            m = re.search(r"more than\s+(\d+(?:\.\d+)?)\s*hours?", low)
            if m:
                terms.credit_threshold_hours = Clause(
                    value=float(m.group(1)), citation=cite,
                    quote=_find_sentence(ch.text, "hours"))
            m = re.search(r"fixed\s+INR\s*([\d,]+)", ch.text, re.I)
            if m:
                terms.credit_fixed_amount_inr = Clause(
                    value=float(m.group(1).replace(",", "")), citation=cite,
                    quote=_find_sentence(ch.text, "fixed"))
            m = re.search(r"capped at\s+INR\s*([\d,]+)", ch.text, re.I)
            if m:
                terms.credit_monthly_cap_inr = Clause(
                    value=float(m.group(1).replace(",", "")), citation=cite,
                    quote=_find_sentence(ch.text, "capped"))
    return terms


@lru_cache(maxsize=1)
def terms_by_account() -> dict[str, ContractTerms]:
    grouped: dict[str, list[Chunk]] = {}
    for ch in all_chunks():
        if ch.doc_type == "agreement" and ch.scoped_account_id:
            grouped.setdefault(ch.scoped_account_id, []).append(ch)
    return {acct: _extract_from_chunks(acct, chs) for acct, chs in grouped.items()}


def terms_for(account_id: str) -> ContractTerms | None:
    """None means: no signed agreement in the pack -> standard policy applies."""
    return terms_by_account().get(account_id)
