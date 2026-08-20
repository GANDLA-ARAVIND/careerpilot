import numpy as np
import pytest
from sqlalchemy.orm import Session

import ranking
from db import JobEmbeddingRow, get_engine
from models import ATSSource, JobPosting
from ranking import ExtractionInfo


def _make_posting(**overrides):
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        location="Bangalore, Karnataka",
        description="We need a backend engineer with Python experience.",
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    with Session(engine) as session:
        yield session


def _fake_embed_factory(vector_map, default=(0.0, 0.0)):
    """Real _embed() is a slow, network-dependent model call - these tests
    replace it with a fixed text -> vector lookup so the caching and ranking
    logic can be verified fast and deterministically, without asserting
    anything about real embedding quality (that isn't a testable invariant)."""

    def fake_embed(texts):
        return np.array([vector_map.get(text, default) for text in texts], dtype=np.float32)

    return fake_embed


# ---------------------------------------------------------------------------
# resume embedding cache (disk, keyed on resume text hash)
# ---------------------------------------------------------------------------


def test_resume_embedding_cached_to_disk(tmp_path, monkeypatch):
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Technical Skills\nPython, SQL.\n\nProjects\nSome project.", encoding="utf-8")
    cache_path = tmp_path / "resume_embedding.json"

    calls = []

    def fake_embed(texts):
        calls.append(texts)
        return np.array([[1.0, 0.0]], dtype=np.float32)

    monkeypatch.setattr(ranking, "_embed", fake_embed)

    first_vec, first_info = ranking.load_resume_embedding(resume_path, cache_path)
    assert cache_path.exists()
    assert len(calls) == 1
    assert first_info.extracted is True

    second_vec, second_info = ranking.load_resume_embedding(resume_path, cache_path)
    assert len(calls) == 1  # cache hit - no second embed call
    np.testing.assert_array_equal(first_vec, second_vec)
    assert second_info == first_info


def test_resume_embedding_cache_invalidated_on_text_change(tmp_path, monkeypatch):
    resume_path = tmp_path / "resume.txt"
    cache_path = tmp_path / "resume_embedding.json"

    calls = []

    def fake_embed(texts):
        calls.append(texts)
        return np.array([[float(len(calls)), 0.0]], dtype=np.float32)

    monkeypatch.setattr(ranking, "_embed", fake_embed)

    resume_path.write_text("Technical Skills\nPython.\n\nProjects\nVersion one project.", encoding="utf-8")
    ranking.load_resume_embedding(resume_path, cache_path)
    assert len(calls) == 1

    resume_path.write_text("Technical Skills\nJava.\n\nProjects\nVersion two, edited project.", encoding="utf-8")
    ranking.load_resume_embedding(resume_path, cache_path)
    assert len(calls) == 2  # text changed -> cache miss -> re-embedded


def test_resume_extraction_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    """No "Technical Skills" or "Projects" headers at all - extraction must
    fail loudly (extracted=False), not silently embed something plausible."""
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Just some resume prose with no recognized section headers anywhere.", encoding="utf-8")
    cache_path = tmp_path / "resume_embedding.json"

    monkeypatch.setattr(ranking, "_embed", lambda texts: np.array([[1.0, 0.0] for _ in texts], dtype=np.float32))

    _, info = ranking.load_resume_embedding(resume_path, cache_path)
    assert info.extracted is False


# ---------------------------------------------------------------------------
# job embedding cache (database, keyed on a hash of the embedded text itself)
# ---------------------------------------------------------------------------


def test_job_embeddings_cached_in_db_and_reused(session, monkeypatch):
    calls = []

    def fake_embed(texts):
        calls.append(list(texts))
        return np.array([[1.0, 0.0] for _ in texts], dtype=np.float32)

    monkeypatch.setattr(ranking, "_embed", fake_embed)

    embeddings = ranking.get_job_embeddings(session, ["some job text"])
    assert "some job text" in embeddings
    assert len(calls) == 1
    assert session.query(JobEmbeddingRow).count() == 1

    embeddings_again = ranking.get_job_embeddings(session, ["some job text"])
    assert len(calls) == 1  # DB cache hit - no second embed call
    np.testing.assert_array_equal(embeddings["some job text"], embeddings_again["some job text"])


def test_only_new_texts_are_embedded(session, monkeypatch):
    calls = []

    def fake_embed(texts):
        calls.append(list(texts))
        return np.array([[1.0, 0.0] for _ in texts], dtype=np.float32)

    monkeypatch.setattr(ranking, "_embed", fake_embed)

    ranking.get_job_embeddings(session, ["cached text"])
    assert len(calls) == 1

    ranking.get_job_embeddings(session, ["cached text", "new text"])
    assert len(calls) == 2
    assert calls[1] == ["new text"]  # only the new text, not a re-embed of "cached text"


def test_changed_extracted_text_is_not_served_stale(session, monkeypatch):
    """The bug this fixes: keying the cache on description_hash meant a
    change to extraction.py or a header list - which changes what text gets
    extracted from the same description - was invisible to the cache lookup,
    so it kept serving an embedding of the OLD extracted text. Keying on a
    hash of the text itself means a different extraction naturally misses
    the cache and gets embedded fresh, with no manual invalidation needed."""
    calls = []

    def fake_embed(texts):
        calls.append(list(texts))
        return np.array([[1.0, 0.0] for _ in texts], dtype=np.float32)

    monkeypatch.setattr(ranking, "_embed", fake_embed)

    # same underlying JD, but extraction.py (or a header list) changed what
    # text gets pulled out of it between these two "runs"
    old_extraction = "Requirements: Python and SQL."
    new_extraction = "Requirements: Python, SQL, and FastAPI."

    ranking.get_job_embeddings(session, [old_extraction])
    assert len(calls) == 1

    embeddings = ranking.get_job_embeddings(session, [new_extraction])
    assert len(calls) == 2  # not served from the old extraction's cache entry
    assert new_extraction in embeddings


# ---------------------------------------------------------------------------
# ranking order and diagnostics
# ---------------------------------------------------------------------------


def test_rank_jobs_orders_by_cosine_similarity_descending(session, monkeypatch):
    resume_info = ExtractionInfo(extracted=True, token_count=50, truncated=False)
    monkeypatch.setattr(ranking, "load_resume_embedding", lambda: (np.array([1.0, 0.0], dtype=np.float32), resume_info))

    strong_match = _make_posting(source_job_id="1", title="A", description="strong match description")
    weak_match = _make_posting(source_job_id="2", title="B", description="weak match description")
    no_match = _make_posting(source_job_id="3", title="C", description="orthogonal description")

    # extract_jd_requirements finds no headers in these one-liners, so the
    # extracted text IS the description unchanged - fine for this test, which
    # is about score ordering, not extraction itself.
    vector_map = {
        strong_match.description: [1.0, 0.0],
        weak_match.description: [0.7071, 0.7071],
        no_match.description: [0.0, 1.0],
    }
    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory(vector_map))

    ranked, diagnostics = ranking.rank_jobs([weak_match, no_match, strong_match], session)

    assert [job.source_job_id for job, _ in ranked] == ["1", "2", "3"]
    assert ranked[0][1] == pytest.approx(1.0)
    assert ranked[1][1] == pytest.approx(0.7071, abs=1e-3)
    assert ranked[2][1] == pytest.approx(0.0, abs=1e-3)
    assert diagnostics.resume is resume_info
    assert len(diagnostics.jobs) == 3


def test_rank_jobs_empty_list_returns_empty(session, monkeypatch):
    resume_info = ExtractionInfo(extracted=True, token_count=50, truncated=False)
    monkeypatch.setattr(ranking, "load_resume_embedding", lambda: (np.array([1.0, 0.0], dtype=np.float32), resume_info))

    ranked, diagnostics = ranking.rank_jobs([], session)
    assert ranked == []
    assert diagnostics.jobs == {}


def test_job_extraction_rate_reflects_mixed_outcomes(session, monkeypatch):
    resume_info = ExtractionInfo(extracted=True, token_count=50, truncated=False)
    monkeypatch.setattr(ranking, "load_resume_embedding", lambda: (np.array([1.0, 0.0], dtype=np.float32), resume_info))
    monkeypatch.setattr(ranking, "_embed", lambda texts: np.array([[1.0, 0.0] for _ in texts], dtype=np.float32))

    clean = _make_posting(source_job_id="1", description="Requirements: Python and SQL.")
    fallback = _make_posting(source_job_id="2", description="Just flowing prose, no recognized section headers.")

    _, diagnostics = ranking.rank_jobs([clean, fallback], session)

    assert diagnostics.job_extraction_rate == pytest.approx(0.5)
    assert diagnostics.jobs[clean.description_hash].extracted is True
    assert diagnostics.jobs[fallback.description_hash].extracted is False


def test_duplicate_description_hash_only_prepared_once(session, monkeypatch):
    """Two postings sharing identical text (same description_hash) should
    only go through extraction/embedding once, not twice."""
    resume_info = ExtractionInfo(extracted=True, token_count=50, truncated=False)
    monkeypatch.setattr(ranking, "load_resume_embedding", lambda: (np.array([1.0, 0.0], dtype=np.float32), resume_info))

    calls = []

    def fake_embed(texts):
        calls.append(list(texts))
        return np.array([[1.0, 0.0] for _ in texts], dtype=np.float32)

    monkeypatch.setattr(ranking, "_embed", fake_embed)

    same_text = "identical description text"
    job_a = _make_posting(source_job_id="1", title="A", description=same_text)
    job_b = _make_posting(source_job_id="2", title="B", description=same_text)
    assert job_a.description_hash == job_b.description_hash

    _, diagnostics = ranking.rank_jobs([job_a, job_b], session)

    assert len(calls) == 1
    assert len(calls[0]) == 1  # one text embedded, not two
    assert len(diagnostics.jobs) == 1
