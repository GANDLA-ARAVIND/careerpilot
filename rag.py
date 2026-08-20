"""Retrieval primitives for Coach - not a chatbot (see CLAUDE.md's RAG
scope). RAG exists here purely to narrow a large, unstructured JD corpus
down to a bounded, LLM-affordable set of chunks Coach can reason over,
where no structured field already answers the question (see
docs/decisions.md's Coach RAG-vs-SQL split).

Retrieval ranks candidate texts by similarity to the QUESTION being asked,
never to the resume. Resume-similarity retrieval would systematically favor
postings that already look like what the candidate has, and bury the ones
asking for what they don't - the opposite of what a gap analysis needs. The
resume enters only downstream, at synthesis (agents/coach.py), as something
the LLM compares the retrieved chunks against - never as part of ranking.

Reuses ranking.py's embedding model and JobEmbeddingRow cache rather than
building a second one: same model, same cache table, keyed the same way
(hash of the exact text embedded), so retrieval here and ranking.py's own
job-vs-resume ranking never duplicate an embedding for the same text.
"""

from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import Session

from ranking import embed_texts, get_job_embeddings


@dataclass
class RetrievalResult:
    """chunk texts + cosine similarity, sorted descending, plus honest
    bookkeeping about the retrieval itself. pool_size/retrieved_count are
    not incidental - they're what lets a caller tell "retrieval narrowed a
    large corpus" apart from "the pool was smaller than k and everything
    came back" (see is_noop). Surfacing this in every report is deliberate:
    at the corpus size this project has today, retrieval is frequently a
    no-op, and that has to stay visible rather than be silently assumed
    away as the corpus grows. See docs/decisions.md."""

    chunks: list[tuple[str, float]]
    pool_size: int
    retrieved_count: int

    @property
    def is_noop(self) -> bool:
        return self.retrieved_count >= self.pool_size


def retrieve(query: str, candidate_texts: list[str], session: Session, k: int) -> RetrievalResult:
    """Top-k of candidate_texts by cosine similarity to query. Callers are
    expected to pass an already-deduplicated candidate_texts (Coach dedupes
    on description_hash before calling this) - this function has no way to
    know which duplicates matter to the caller, so it doesn't guess."""
    pool_size = len(candidate_texts)
    if pool_size == 0:
        return RetrievalResult(chunks=[], pool_size=0, retrieved_count=0)

    query_vector = embed_texts([query])[0]
    embeddings_by_text = get_job_embeddings(session, candidate_texts)

    scored = [(text, float(np.dot(query_vector, embeddings_by_text[text]))) for text in candidate_texts]
    scored.sort(key=lambda pair: pair[1], reverse=True)

    top = scored[:k]
    return RetrievalResult(chunks=top, pool_size=pool_size, retrieved_count=len(top))
