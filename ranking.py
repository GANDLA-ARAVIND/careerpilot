"""Local embedding-based ranking of filtered jobs against the resume - no LLM
calls. Cosine similarity here is a coarse relevance signal, not a fit
judgment: all-MiniLM-L6-v2 truncates at 256 tokens, so a full resume or JD
overflows it badly (measured: resume 787 tokens, typical filtered JD 800+).
extraction.py pulls the skills/requirements portion out first so the window
is spent on the part that matters - but extraction is pattern-based and can
miss a header it doesn't recognize, so every embedding here is paired with
diagnostics (extracted vs. fell back to full text, token count, still
truncated after extraction) instead of a silent best guess. This stage
exists purely to cut the filtered pool down to a top-N the LLM re-ranker can
afford to look at with real reasoning, per CLAUDE.md's cheap-filters-first
design - it is not the final word on fit.

sentence_transformers (and the torch/transformers it pulls in - hundreds of
MB, several seconds, and noisy import-time warnings from transformers'
optional torchvision probing) is imported lazily inside _get_model(), not
at module level. _get_model() already only instantiates the model on first
real use; importing the class eagerly at module load defeated half of that
laziness. This matters beyond this module: app.py's dashboard imports
agents.coach for missing_skills_below (pure SQL, no embeddings at all), and
agents.coach imports rag, which imports this module - so an eager import
here meant every dashboard startup paid the full torch cost for a code path
(rag.retrieve, via coach.market_gap) the dashboard never calls. See
docs/decisions.md.
"""

from __future__ import annotations  # SentenceTransformer only needs to exist as a type hint here, not a real import - see above

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np
from sqlalchemy.orm import Session

from db import JobEmbeddingRow
from extraction import extract_jd_requirements, extract_resume_sections
from models import JobPosting

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

MODEL_NAME = "all-MiniLM-L6-v2"
RESUME_PATH = Path("data/resume.txt")
RESUME_CACHE_PATH = Path("data/resume_embedding.json")
MAX_TOKENS = 256  # all-MiniLM-L6-v2's hard sequence limit
_RESUME_CACHE_KEYS = {"text_hash", "embedding", "extracted", "token_count", "truncated"}

_model: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # deferred - see module docstring

        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def _embed(texts: list[str]) -> np.ndarray:
    """L2-normalized embeddings, so cosine similarity reduces to a plain dot
    product wherever these are compared."""
    return _get_model().encode(texts, convert_to_numpy=True, normalize_embeddings=True)


def embed_texts(texts: list[str]) -> np.ndarray:
    """Public entry point onto the same model/normalization _embed uses -
    for callers outside this module (rag.py's retrieval query embedding)
    that have no business reaching into a private name. _embed itself stays
    as-is; every existing test in this file already monkeypatches it
    directly, so it isn't renamed out from under them."""
    return _embed(texts)


def _token_count(text: str) -> int:
    return len(_get_model().tokenizer.encode(text))


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ExtractionInfo:
    extracted: bool  # False = extraction found no recognized header, fell back to full text
    token_count: int  # of the text actually embedded, after any budget trimming
    truncated: bool  # still over MAX_TOKENS even after extraction/trimming


def _fit_to_budget(skills_text: str, projects_text: str, max_tokens: int) -> str:
    """Always keep skills_text intact; trim projects_text line by line so
    the combined text fits the token budget. This is the fix for the
    resume's own truncation - the full skills list used to get cut off
    mid-list because header/contact/summary text led the raw file before
    skills ever appeared. Guaranteeing skills survives puts the tradeoff on
    project detail instead."""
    lines = [line for line in projects_text.split("\n") if line.strip()]
    kept_lines: list[str] = []
    for line in lines:
        candidate = skills_text + "\n\n" + "\n".join(kept_lines + [line])
        if _token_count(candidate) > max_tokens:
            break
        kept_lines.append(line)

    if not kept_lines:
        return skills_text
    return skills_text + "\n\n" + "\n".join(kept_lines)


def _prepare_resume_text(resume_text: str) -> tuple[str, ExtractionInfo]:
    skills_text, projects_text, extracted = extract_resume_sections(resume_text)
    text = _fit_to_budget(skills_text, projects_text, MAX_TOKENS) if extracted else resume_text

    token_count = _token_count(text)
    return text, ExtractionInfo(extracted=extracted, token_count=token_count, truncated=token_count > MAX_TOKENS)


def _prepare_job_text(description: str) -> tuple[str, ExtractionInfo]:
    text, extracted = extract_jd_requirements(description)
    token_count = _token_count(text)
    return text, ExtractionInfo(extracted=extracted, token_count=token_count, truncated=token_count > MAX_TOKENS)


def load_resume_embedding(
    resume_path: Path = RESUME_PATH, cache_path: Path = RESUME_CACHE_PATH
) -> tuple[np.ndarray, ExtractionInfo]:
    """Embed the resume's extracted skills/projects text, cached to disk
    keyed on the raw resume file's own hash - an edited resume.txt
    invalidates the cache automatically instead of silently ranking against
    a stale version. Returns the embedding plus extraction diagnostics so a
    failed extraction is visible to the caller, not swallowed."""
    raw_text = resume_path.read_text(encoding="utf-8")
    text_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        # missing keys (e.g. a cache written by an older cache format) are
        # treated as a miss and recomputed, not a crash
        if _RESUME_CACHE_KEYS.issubset(cached) and cached["text_hash"] == text_hash:
            info = ExtractionInfo(cached["extracted"], cached["token_count"], cached["truncated"])
            return np.array(cached["embedding"], dtype=np.float32), info

    prepared_text, info = _prepare_resume_text(raw_text)
    embedding = _embed([prepared_text])[0]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "text_hash": text_hash,
                "embedding": embedding.tolist(),
                "extracted": info.extracted,
                "token_count": info.token_count,
                "truncated": info.truncated,
            }
        ),
        encoding="utf-8",
    )
    return embedding, info


def get_job_embeddings(session: Session, texts: list[str]) -> dict[str, np.ndarray]:
    """{text: embedding} for each text, reusing rows cached in the database
    under a hash of the text itself - not description_hash - so a change to
    extraction.py or the config header lists (which changes what text gets
    produced from the same description) automatically invalidates just the
    affected entries instead of silently serving a stale embedding under an
    unchanged key. See docs/decisions.md."""
    if not texts:
        return {}

    hash_to_text = {_text_hash(text): text for text in texts}
    needed_hashes = set(hash_to_text)
    cached_rows = session.query(JobEmbeddingRow).filter(JobEmbeddingRow.text_hash.in_(needed_hashes)).all()
    embeddings_by_hash = {row.text_hash: np.frombuffer(row.embedding, dtype=np.float32) for row in cached_rows}

    missing_hashes = list(needed_hashes - embeddings_by_hash.keys())
    if missing_hashes:
        missing_texts = [hash_to_text[text_hash] for text_hash in missing_hashes]
        vectors = _embed(missing_texts)
        for text_hash, vector in zip(missing_hashes, vectors):
            embeddings_by_hash[text_hash] = vector
            session.add(JobEmbeddingRow(text_hash=text_hash, embedding=vector.astype(np.float32).tobytes()))
        session.commit()

    return {hash_to_text[text_hash]: vector for text_hash, vector in embeddings_by_hash.items()}


@dataclass
class RankingDiagnostics:
    resume: ExtractionInfo
    jobs: dict[str, ExtractionInfo]  # keyed by description_hash

    @property
    def job_extraction_rate(self) -> float:
        if not self.jobs:
            return 0.0
        return sum(1 for info in self.jobs.values() if info.extracted) / len(self.jobs)


def rank_jobs(jobs: list[JobPosting], session: Session) -> tuple[list[tuple[JobPosting, float]], RankingDiagnostics]:
    """Rank jobs by cosine similarity to the resume's extracted
    skills/projects text, highest first. Returns diagnostics alongside the
    ranking so an extraction failure - on either side - is visible instead
    of silently affecting the score."""
    resume_embedding, resume_info = load_resume_embedding()

    prepared_by_hash: dict[str, tuple[str, ExtractionInfo]] = {}  # description_hash -> (text, info)
    for job in jobs:
        if job.description_hash not in prepared_by_hash:
            prepared_by_hash[job.description_hash] = _prepare_job_text(job.description)

    texts = [text for text, _ in prepared_by_hash.values()]
    embeddings_by_text = get_job_embeddings(session, texts)

    job_infos = {description_hash: info for description_hash, (_, info) in prepared_by_hash.items()}

    scored = []
    for job in jobs:
        text, _ = prepared_by_hash[job.description_hash]
        scored.append((job, float(np.dot(resume_embedding, embeddings_by_text[text]))))
    ranked = sorted(scored, key=lambda pair: pair[1], reverse=True)

    return ranked, RankingDiagnostics(resume=resume_info, jobs=job_infos)
