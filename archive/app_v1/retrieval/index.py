"""Hybrid lexical retrieval over the document pack.

A deliberate engineering trade-off worth defending: there is no vector database
here. The corpus is 6 documents and ~26 chunks of dense, high-jargon policy
text. At that size a semantic index adds a 500MB torch dependency, a slow cold
start and an extra hosted service, in exchange for recall we can already get
from BM25 plus character n-gram TF-IDF and a small domain synonym table. The
right time to add embeddings is when the corpus outgrows lexical matching, so
`Retriever` is written as a swappable interface rather than assumed away.

There ARE embeddings in this system, in `store/vectors.py`, but over conversation
history -- unbounded, written in customer English, and searched by someone who
does not know what words were used. That is a semantic problem; this is not.
Using different retrieval for the two is the point, not an inconsistency.

What the corpus size does NOT excuse is skipping governance -- see governed.py.
"""
from __future__ import annotations

import re
from functools import lru_cache

from rank_bm25 import BM25Okapi
from sklearn.feature_extraction.text import TfidfVectorizer

from app.ingest.corpus import Chunk, all_chunks

# Support vocabulary is narrow and customers do not use it. Expanding the query
# is cheaper and far more predictable than embeddings for this corpus.
SYNONYMS: dict[str, list[str]] = {
    "cancel": ["cancellation", "cancelled", "cancelling"],
    "cancellation": ["cancel", "fee", "cancelled"],
    "fee": ["charge", "cost", "cancellation"],
    "refund": ["credit", "service credit", "compensation"],
    "credit": ["service credit", "compensation", "refund"],
    "late": ["delay", "delayed", "missed", "overdue"],
    "delay": ["late", "delayed", "missed"],
    "pickup": ["collection", "collect", "picked up"],
    "sla": ["response target", "first-response", "severity", "response time"],
    "response": ["first-response", "target", "sla"],
    "severity": ["p1", "p2", "p3", "critical", "priority"],
    "outage": ["down", "failure", "critical", "p1"],
    "bulk": ["csv", "upload", "rows"],
    "upload": ["bulk", "csv", "rows"],
    "plan": ["entitlement", "tier", "enterprise", "growth", "standard"],
    "entitlement": ["plan", "included", "available"],
    "escalate": ["escalation", "p1", "urgent"],
    "contract": ["agreement", "terms"],
    "agreement": ["contract", "terms"],
    "webhook": ["confirmation", "status", "booked"],
}

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "for", "and",
         "or", "in", "on", "at", "it", "this", "that", "we", "i", "do", "does",
         "can", "be", "have", "has", "with", "my", "our", "you", "your"}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower())
            if t not in _STOP and len(t) > 1]


def expand(query: str) -> list[str]:
    toks = tokenize(query)
    out = list(toks)
    for t in toks:
        for syn in SYNONYMS.get(t, []):
            out.extend(tokenize(syn))
    return out


class HybridIndex:
    """BM25 + character n-gram TF-IDF, fused by reciprocal rank.

    RRF is used rather than a weighted score sum because the two scorers are on
    incomparable scales, and rank fusion needs no per-corpus calibration -- one
    less thing to silently drift when the documents change.
    """

    K_RRF = 60

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        corpus_text = [f"{c.doc_title} {c.section} {c.text}" for c in chunks]
        self.bm25 = BM25Okapi([tokenize(t) for t in corpus_text])
        # Character n-grams survive the morphology the synonym table misses
        # ("cancelling" vs "cancellation") and typos in customer questions.
        self.tfidf = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5),
                                     min_df=1, sublinear_tf=True)
        self.matrix = self.tfidf.fit_transform(corpus_text)

    def search(self, query: str, top_k: int = 8) -> list[tuple[Chunk, float, dict]]:
        if not (query or "").strip():
            return []
        bm_scores = self.bm25.get_scores(expand(query))
        bm_rank = {i: r for r, i in enumerate(
            sorted(range(len(self.chunks)), key=lambda i: -bm_scores[i]))}

        qv = self.tfidf.transform([query])
        tf_scores = (self.matrix @ qv.T).toarray().ravel()
        tf_rank = {i: r for r, i in enumerate(
            sorted(range(len(self.chunks)), key=lambda i: -tf_scores[i]))}

        fused = []
        for i, ch in enumerate(self.chunks):
            score = (1.0 / (self.K_RRF + bm_rank[i] + 1)
                     + 1.0 / (self.K_RRF + tf_rank[i] + 1))
            # NOTE: authority deliberately does NOT influence this score.
            # Authority answers "which source wins when two disagree", not
            # "which source is about the thing you asked". Conflating them makes
            # every query drag in the highest-tier document whether or not it is
            # relevant -- an earlier version of this file boosted contracts here
            # and, as a result, could not retrieve the support-policy SLA table
            # at all. Ranking is relevance; precedence lives in governed.py and
            # the policy engine.
            fused.append((ch, score, {"bm25": float(bm_scores[i]),
                                      "tfidf": float(tf_scores[i])}))
        fused.sort(key=lambda x: -x[1])
        # Drop chunks neither scorer had any signal for.
        return [f for f in fused[:top_k]
                if f[2]["bm25"] > 0.01 or f[2]["tfidf"] > 0.01]


@lru_cache(maxsize=1)
def index() -> HybridIndex:
    return HybridIndex(all_chunks())
