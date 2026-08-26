"""Semantic search over past conversations.

This is the one place in the system where embeddings earn their keep, and it is
worth being precise about why -- `retrieval/index.py` deliberately does NOT use
them, and both decisions are right.

  Policy corpus: 26 chunks of dense, high-jargon text where the query and the
  source share vocabulary ("cancellation fee", "INR 250", "§2"). Lexical BM25
  wins there, and an exact clause or figure is exactly what embeddings blur.

  Conversation history: unbounded, written in customer English, and searched by
  someone who does NOT know what words were used. "uploads keep breaking" has to
  find a thread that said "bulk CSV import times out". No amount of synonym
  table gets there; that is a semantic problem.

The model is local (all-MiniLM-L6-v2, 384-dim) because Groq serves no embedding
endpoint. It is loaded lazily on first use rather than at import, so a cold boot
of the API is not held up by a model load that most requests never need.

Cosine similarity is computed in numpy over the whole table. That is O(n) and
completely adequate: an ANN index is worth adding somewhere north of ~100k
conversations, and pretending otherwise here would be the same mistake the
retrieval module declined to make.
"""
from __future__ import annotations

import os
import threading

import numpy as np

from app.store import db

MODEL_NAME = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
DIM = 384

_model = None
_lock = threading.Lock()
_load_failed = False


def model():
    """Load once, on first use. Returns None if the model is unavailable.

    A missing model must degrade to "search does not work" rather than "the app
    does not start": conversations are still recorded and readable, and the
    index can be rebuilt later with rebuild().
    """
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _lock:
        if _model is None and not _load_failed:
            try:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(MODEL_NAME)
            except Exception as e:                                # noqa: BLE001
                print(f"[vectors] embedding model unavailable ({e}); "
                      f"semantic search disabled, transcripts still recorded")
                _load_failed = True
    return _model


def available() -> bool:
    return model() is not None


def _encode(texts: list[str]) -> np.ndarray | None:
    m = model()
    if m is None:
        return None
    v = np.asarray(m.encode(texts), dtype=np.float32)
    if v.ndim == 1:
        v = v[None, :]
    # Normalise once here so search is a plain dot product later.
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.clip(norms, 1e-9, None)


def summarise(convo: dict, messages: list[dict]) -> str:
    """The text that represents a conversation in the index.

    Deliberately the QUESTIONS plus the title, not the full transcript. The
    assistant's answers are long, templated and full of shared policy language,
    so including them pulls every conversation towards the same centroid and
    makes everything look similar to everything.
    """
    parts = [convo.get("title") or ""]
    if convo.get("context_ref"):
        parts.append(str(convo["context_ref"]))
    parts += [m["content"] for m in messages if m.get("role") == "user"][:12]
    return "\n".join(p for p in parts if p).strip()[:4000]


def index_conversation(cid: str) -> bool:
    """(Re)embed one conversation. Safe to call after every turn."""
    data = db.transcript(cid)
    if not data:
        return False
    text = summarise(data["conversation"], data["messages"])
    if not text:
        return False
    v = _encode([text])
    if v is None:
        return False
    db.upsert_vector(cid, text, DIM, v[0].tobytes())
    return True


def search(query: str, *, limit: int = 10, account_id: str | None = None,
           exclude: str | None = None) -> list[dict]:
    """Conversations most similar to `query`, most similar first.

    `account_id` scopes the result to one tenant. It is applied AFTER scoring
    but before returning, and callers in the API layer must pass it for any
    customer-visible search -- the vector table has no notion of who may read
    what, so isolation stays the caller's explicit responsibility.
    """
    rows = db.all_vectors()
    if not rows:
        return []
    q = _encode([query])
    if q is None:
        return []

    ids = [r["conversation_id"] for r in rows]
    mat = np.frombuffer(b"".join(r["vector"] for r in rows),
                        dtype=np.float32).reshape(len(rows), DIM)
    scores = mat @ q[0]

    order = np.argsort(-scores)
    out: list[dict] = []
    for i in order:
        cid = ids[int(i)]
        if cid == exclude:
            continue
        convo = db.get_conversation(cid)
        if convo is None:
            continue
        if account_id and convo.get("account_id") != account_id:
            continue
        convo["score"] = round(float(scores[int(i)]), 4)
        out.append(convo)
        if len(out) >= limit:
            break
    return out


def rebuild() -> int:
    """Re-embed every conversation. For a model change or a cold index."""
    n = 0
    for c in db.list_conversations(limit=10_000):
        if index_conversation(c["id"]):
            n += 1
    return n
