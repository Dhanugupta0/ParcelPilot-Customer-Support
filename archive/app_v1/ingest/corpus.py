"""Document ingest with an explicit authority model.

The precedence hierarchy is not something we invented -- Support Policy v3 s1
states it outright:

    "When sources conflict, use the signed customer agreement first, then the
     current support policy, then current product documentation. Historical
     tickets and internal notes are context only and may contain incorrect
     past guidance."

We turn that sentence into structured metadata on every chunk, because a rule
that lives only in a system prompt is a suggestion, while a rule that lives in
the retriever's filter is a guarantee.

    Tier 1  signed customer agreement   (scoped to one account)
    Tier 2  current support policy / SOP
    Tier 3  current product documentation
    Tier 4  deprecated docs + historical tickets -- CONTEXT ONLY, never authority

Metadata is derived from the document text (Status:, Effective:, Account:)
rather than hard-coded per filename, so dropping another agreement into
Dataset/ works without a code change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import fitz

from app import config
from app.core import clock

# --- authority ------------------------------------------------------------
TIER_CONTRACT = 1
TIER_POLICY = 2
TIER_PRODUCT_DOC = 3
TIER_CONTEXT_ONLY = 4

TIER_LABEL = {
    TIER_CONTRACT: "Signed customer agreement (highest authority)",
    TIER_POLICY: "Current ParcelPilot policy / SOP",
    TIER_PRODUCT_DOC: "Current product documentation",
    TIER_CONTEXT_ONLY: "Context only - deprecated or historical, NOT authoritative",
}

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def _parse_long_date(text: str) -> datetime | None:
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text or "")
    if not m:
        return None
    day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if mon not in MONTHS:
        return None
    return clock.ensure_tz(datetime(year, MONTHS[mon], day))


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    doc_title: str
    section: str
    text: str
    authority_tier: int
    status: str                      # CURRENT | DEPRECATED | RESOLVED
    doc_type: str                    # agreement | policy | sop | product_doc
    effective_date: datetime | None
    scoped_account_id: str | None    # set for agreements -> enforces contract scoping
    citation: str                    # e.g. "Support Policy v3 s3"

    @property
    def is_authoritative(self) -> bool:
        return self.authority_tier <= TIER_PRODUCT_DOC and self.status != "DEPRECATED"

    def to_dict(self, *, include_text: bool = True) -> dict:
        d = {
            "citation": self.citation,
            "document": self.doc_title,
            "section": self.section,
            "authority_tier": self.authority_tier,
            "authority": TIER_LABEL[self.authority_tier],
            "status": self.status,
            "effective_date": clock.fmt(self.effective_date),
            "scoped_to_account": self.scoped_account_id,
        }
        if include_text:
            d["text"] = self.text
        return d


@dataclass
class Document:
    doc_id: str
    path: Path
    title: str
    doc_type: str
    status: str
    authority_tier: int
    effective_date: datetime | None
    scoped_account_id: str | None
    raw_text: str
    tables: list[list[list[str]]] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)


def _classify(title: str, filename: str, body: str) -> tuple[str, str, str | None]:
    """-> (doc_type, status, scoped_account_id)"""
    hay = f"{title} {filename}".lower()
    if "agreement" in hay:
        doc_type = "agreement"
    elif "sop" in hay or "procedure" in hay:
        doc_type = "sop"
    elif "policy" in hay:
        doc_type = "policy"
    else:
        doc_type = "product_doc"

    status = "CURRENT"
    m = re.search(r"Status:\s*([A-Z][A-Z_ -]*)", body)
    if m:
        token = m.group(1).strip().split()[0].upper()
        status = "DEPRECATED" if token.startswith("DEPRECAT") else token
    elif "deprecated" in filename.lower():
        status = "DEPRECATED"

    acct = None
    m = re.search(r"Account:\s*(ACCT-\d+)", body)
    if m:
        acct = m.group(1)
    return doc_type, status, acct


def _tier_for(doc_type: str, status: str) -> int:
    if status == "DEPRECATED":
        return TIER_CONTEXT_ONLY          # superseded material can never win
    return {"agreement": TIER_CONTRACT,
            "policy": TIER_POLICY,
            "sop": TIER_POLICY,
            "product_doc": TIER_PRODUCT_DOC}.get(doc_type, TIER_PRODUCT_DOC)


_SECTION_RE = re.compile(r"^\s*(\d+)\.\s+(.{3,80})$", re.MULTILINE)


def _short_title(title: str) -> str:
    """'ParcelPilot Cancellation & Service Credit SOP v4' -> 'Cancellation & Service Credit SOP v4'"""
    t = re.sub(r"^ParcelPilot\s*[-–]?\s*", "", title).strip()
    return t or title


def _section_chunks(doc: Document) -> list[Chunk]:
    body = doc.raw_text
    marks = list(_SECTION_RE.finditer(body))
    short = _short_title(doc.title)
    out: list[Chunk] = []

    def add(idx: int, section: str, text: str, cite: str) -> None:
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < 25:
            return
        out.append(Chunk(
            chunk_id=f"{doc.doc_id}#{idx}",
            doc_id=doc.doc_id, doc_title=short, section=section, text=text,
            authority_tier=doc.authority_tier, status=doc.status,
            doc_type=doc.doc_type, effective_date=doc.effective_date,
            scoped_account_id=doc.scoped_account_id, citation=cite,
        ))

    # Preamble carries Status/Effective/Account -- always worth indexing.
    head_end = marks[0].start() if marks else len(body)
    add(0, "Header", body[:head_end], f"{short} (header)")

    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        heading = m.group(2).strip()
        add(i + 1, f"{m.group(1)}. {heading}",
            body[m.start():end], f"{short} §{m.group(1)} ({heading})")

    # Known-issue blocks (KI-xxx) get their own chunks so a query about one
    # issue cannot drag the whole guide -- including the RESOLVED issue -- along.
    #
    # Resolution status is decided by which SECTION the block sits in, not by
    # scanning for the word "Resolved" anywhere nearby: the Product Operations
    # Guide places KI-211 immediately before a heading called "3. Resolved
    # issue", and a naive scan mislabels the live SwiftShip webhook issue as
    # resolved -- which would quietly demote the one document TKT-504 needs.
    section_spans: list[tuple[str, int, int]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        section_spans.append((m.group(2).strip(), m.start(), end))
    if not section_spans:
        section_spans = [("", 0, len(body))]

    for sec_title, sec_start, sec_end in section_spans:
        segment = body[sec_start:sec_end]
        sec_is_resolved = "resolved" in sec_title.lower()
        for km in re.finditer(r"(KI-\d+)\s*[-–]\s*(.+?)(?=\n\s*KI-\d+\s*[-–]|\Z)",
                              segment, re.DOTALL):
            ki = km.group(1)
            block = km.group(2).strip()
            first_line = block.splitlines()[0].strip() if block else ""
            # Titles are short noun phrases; anything longer has run into prose.
            ki_title = re.split(r"(?<=[a-z])[:.]\s", first_line)[0].strip()
            ki_title = ki_title[:70].rstrip(" .:")
            ki_status_m = re.search(r"Status:\s*([A-Za-z]+)", block)
            ki_status = (ki_status_m.group(1).strip().upper()
                         if ki_status_m else ("RESOLVED" if sec_is_resolved else "CURRENT"))
            if sec_is_resolved or re.match(r"^\s*RESOLVED", ki_status):
                ki_status = "RESOLVED"
            out.append(Chunk(
                chunk_id=f"{doc.doc_id}#ki-{ki}",
                doc_id=doc.doc_id, doc_title=short,
                section=f"Known issue {ki}",
                text=f"{ki} - {block}".strip(),
                authority_tier=doc.authority_tier,
                status=ki_status,
                doc_type=doc.doc_type, effective_date=doc.effective_date,
                scoped_account_id=doc.scoped_account_id,
                citation=f"{short} – {ki} ({ki_title})",
            ))
    return out


def load_document(path: Path) -> Document:
    pdf = fitz.open(path)
    pages, tables = [], []
    for page in pdf:
        pages.append(page.get_text())
        try:
            for t in page.find_tables().tables:
                rows = [[(c or "").strip() for c in r] for r in t.extract()]
                if len(rows) > 1:
                    tables.append(rows)
        except Exception:
            pass
    raw = "\n".join(pages).replace("​", "").replace("●", "-")
    raw = re.sub(r"[ \t]+\n", "\n", raw)

    title = next((l.strip() for l in raw.splitlines() if l.strip()), path.stem)
    # Titles sometimes wrap onto a second line in these PDFs.
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if len(lines) > 1 and len(title) < 45 and not lines[1].startswith(("Status", "Account", "Effective")):
        title = f"{title} {lines[1]}"

    doc_type, status, acct = _classify(title, path.name, raw)
    eff = None
    m = re.search(r"(?:Effective|Updated):\s*([^\n]+)", raw)
    if m:
        eff = _parse_long_date(m.group(1))

    doc = Document(
        doc_id=path.stem, path=path, title=title, doc_type=doc_type, status=status,
        authority_tier=_tier_for(doc_type, status), effective_date=eff,
        scoped_account_id=acct, raw_text=raw, tables=tables,
    )
    doc.chunks = _section_chunks(doc)
    return doc


@lru_cache(maxsize=1)
def corpus() -> list[Document]:
    docs = [load_document(p) for p in sorted(config.DATASET_DIR.glob("*.pdf"))]
    return docs


@lru_cache(maxsize=1)
def all_chunks() -> list[Chunk]:
    return [c for d in corpus() for c in d.chunks]
