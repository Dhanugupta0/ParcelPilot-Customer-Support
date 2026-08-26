"""Policy documents in a vector store.

Every PDF in the pack is split into sections, embedded once, and searched by
meaning. Two things make that safe here, and they are worth stating because a
vector store is usually the wrong tool for dense policy text:

  1. It never supplies a NUMBER. Fees, credits and SLA targets come from
     `engine.py`, computed in Python from the workbook. Retrieval's only job is
     to find the passage that should be CITED beside an answer that has already
     been calculated. Blurring "INR 250" into "INR 500" is the classic failure
     of embeddings over policy, and it cannot happen if no figure is read out of
     a retrieved chunk.

  2. Authority is metadata, not similarity. A deprecated policy can be the
     closest match to a question and must still lose to the signed agreement.
     Precedence is applied AFTER scoring, from the rule in Support Policy v3 §1.

Chunks carry the tenant they belong to, so one customer's agreement is never
retrievable in another customer's session -- filtered in the query, not by
asking the model nicely.
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field

import numpy as np

from app import config

JINA_API_URL = "https://api.jina.ai/v1/embeddings"
DIM = 768  # jina-embeddings-v2-base-en output dimension

# Source precedence, straight from Support Policy v3 §1:
#   signed customer agreement > current support policy > current product docs
#   > historical tickets and notes (context only, may be wrong)
AUTHORITY_AGREEMENT = 1
AUTHORITY_POLICY = 2
AUTHORITY_PRODUCT = 3
AUTHORITY_HISTORY = 4

AUTHORITY_LABEL = {
    AUTHORITY_AGREEMENT: "signed customer agreement",
    AUTHORITY_POLICY: "current ParcelPilot policy",
    AUTHORITY_PRODUCT: "current product documentation",
    AUTHORITY_HISTORY: "historical ticket (context only)",
}

# What each file in the pack IS. Declared rather than inferred from the name at
# query time: "DEPRECATED" living in a filename is a convention, and a
# convention is not a control.
DOCS = {
    "01_Support_Policy_v3_CURRENT.pdf": dict(
        title="Support Policy v3", status="CURRENT",
        authority=AUTHORITY_POLICY, account_id=None),
    "02_Support_Policy_v2_DEPRECATED.pdf": dict(
        title="Support Policy v2", status="DEPRECATED",
        authority=AUTHORITY_POLICY, account_id=None),
    "03_Cancellation_and_Service_Credit_SOP_v4.pdf": dict(
        title="Cancellation & Service Credit SOP v4", status="CURRENT",
        authority=AUTHORITY_POLICY, account_id=None),
    "04_Product_Operations_Guide_and_Known_Issues.pdf": dict(
        title="Product Operations Guide", status="CURRENT",
        authority=AUTHORITY_PRODUCT, account_id=None),
    "05_Northstar_Logistics_Enterprise_Agreement.pdf": dict(
        title="Northstar Logistics Enterprise Agreement", status="CURRENT",
        authority=AUTHORITY_AGREEMENT, account_id="ACCT-001"),
    "06_LumenWorks_Service_Agreement.pdf": dict(
        title="LumenWorks Service Agreement", status="CURRENT",
        authority=AUTHORITY_AGREEMENT, account_id="ACCT-002"),
}


@dataclass
class Chunk:
    doc_file: str
    title: str
    status: str
    authority: int
    account_id: str | None
    section: str
    text: str
    score: float = 0.0

    @property
    def citation(self) -> str:
        return f"{self.title} {self.section}".strip()

    def to_dict(self) -> dict:
        return {"citation": self.citation, "title": self.title,
                "status": self.status, "section": self.section,
                "authority": AUTHORITY_LABEL[self.authority],
                "account_id": self.account_id, "text": self.text,
                "score": round(self.score, 4)}


_SECTION = re.compile(r"^\s*(\d+)\.\s+(.{3,80}?)\s*$", re.M)
_KI = re.compile(r"^\s*(KI-\d+)\s*[-–—]\s*(.{3,90}?)\s*$", re.M)


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split a document into (section label, body).

    Numbered headings first; known issues get their own chunks because KI-208
    and KI-211 are separate facts a question can be about, and burying them in
    one "known issues" blob makes both harder to retrieve.
    """
    marks: list[tuple[int, str]] = []
    for m in _SECTION.finditer(text):
        marks.append((m.start(), f"§{m.group(1)} ({m.group(2).strip()})"))
    for m in _KI.finditer(text):
        marks.append((m.start(), f"— {m.group(1)} ({m.group(2).strip()})"))
    marks.sort()
    if not marks:
        return [("", text.strip())]

    out = []
    head = text[:marks[0][0]].strip()
    if head:
        out.append(("(header)", head))
    for i, (pos, label) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[pos:end].strip()
        if body:
            out.append((label, body))
    return out


def build_chunks() -> list[Chunk]:
    import fitz
    chunks: list[Chunk] = []
    for filename, meta in DOCS.items():
        path = config.DATASET_DIR / filename
        if not path.exists():
            raise FileNotFoundError(
                f"{filename} is missing from {config.DATASET_DIR}. The document "
                f"pack is the source of truth; a missing file must fail loudly.")
        doc = fitz.open(path)
        text = "\n".join(p.get_text() for p in doc)
        # The PDFs use a bullet glyph that survives extraction as noise.
        text = text.replace("●", "-").replace("​", "")
        for section, body in _split_sections(text):
            chunks.append(Chunk(doc_file=filename, section=section,
                                text=re.sub(r"[ \t]+", " ", body).strip(),
                                **meta))
    return chunks


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------

_MAX_BATCH = 128  # Jina API batch limit


def _embed(texts: list[str]) -> np.ndarray:
    """Embed texts via the Jina Embeddings API.

    Sends texts in batches, returns L2-normalised float32 vectors.
    """
    api_key = config.JINA_API_KEY
    if not api_key:
        raise RuntimeError(
            "JINA_EMBED_MODEL is not set in .env. "
            "An API key is required for embedding generation.")

    all_vecs: list[list[float]] = []
    for start in range(0, len(texts), _MAX_BATCH):
        batch = texts[start:start + _MAX_BATCH]
        payload = json.dumps({
            "model": config.JINA_EMBED_MODEL_NAME,
            "input": batch,
            "normalized": True,
        }).encode()

        req = urllib.request.Request(
            JINA_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"Jina Embeddings API returned {e.code}: {e.read().decode()}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach Jina Embeddings API: {e.reason}"
            ) from e

        # Response data is sorted by index already, but sort to be safe.
        items = sorted(body["data"], key=lambda d: d["index"])
        all_vecs.extend(item["embedding"] for item in items)

    v = np.asarray(all_vecs, dtype=np.float32)
    if v.ndim == 1:
        v = v[None, :]
    # Jina returns normalised vectors when normalized=True, but clip for safety.
    return v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-9, None)


@dataclass
class Index:
    chunks: list[Chunk] = field(default_factory=list)
    matrix: np.ndarray | None = None

    def search(self, query: str, *, account_id: str | None,
               include_deprecated: bool = False, limit: int = 6,
               all_tenants: bool = False
               ) -> tuple[list[Chunk], list[dict]]:
        """Return (passages, excluded) ranked by authority then similarity.

        `excluded` is not a debugging aid -- it is part of the answer. A user
        asking about cancellation fees whose question matched the DEPRECATED
        policy deserves to be told that source exists and why it was not used.
        """
        q = _embed([query])[0]
        scores = self.matrix @ q

        kept: list[Chunk] = []
        excluded: list[dict] = []
        for i, ch in enumerate(self.chunks):
            c = Chunk(**{**ch.__dict__, "score": float(scores[i])})
            if c.status == "DEPRECATED" and not include_deprecated:
                if c.score > 0.25:
                    excluded.append({"citation": c.citation,
                                     "reason": "superseded by a current policy",
                                     "score": round(c.score, 3)})
                continue
            # A contract belongs to exactly one tenant. This is the isolation
            # boundary for documents, and it is a filter, not an instruction.
            #
            # `all_tenants` is for an internal reader who has not named an
            # account. Excluding every agreement from them as "another
            # customer's" was wrong twice over: it hid the clause that governs
            # the answer, and it told an agent asking about Northstar that the
            # Northstar contract belonged to someone else.
            if c.account_id and not all_tenants and c.account_id != account_id:
                excluded.append({"citation": c.citation,
                                 "reason": "another customer's agreement",
                                 "score": round(c.score, 3)})
                continue
            kept.append(c)

        # Authority first, similarity second. The signed agreement outranks the
        # SOP even when the SOP is the closer textual match, because that is
        # what Support Policy v3 §1 says must happen.
        kept.sort(key=lambda c: (c.authority, -c.score))
        strong = [c for c in kept if c.score > 0.15][:limit]
        return (strong or kept[:limit]), excluded


_index: Index | None = None


def index() -> Index:
    global _index
    if _index is None:
        chunks = build_chunks()
        _index = Index(chunks=chunks, matrix=_embed([c.text for c in chunks]))
    return _index
