import json
from datetime import datetime, timezone

import numpy as np
import pytest
from sqlalchemy.orm import Session

import agents.coach as coach_module
import ranking
from agents.coach import market_gap, missing_skills_below
from config import GEMINI_MODEL_STAGE1, GEMINI_MODEL_STAGE2
from db import AnalystResultRow, get_engine, upsert_job
from llm import LLMClient
from models import ATSSource, JobPosting


def _make_posting(**overrides):
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        location="Bangalore, Karnataka",
        description="Requirements: Python, FastAPI, PostgreSQL.",
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    with Session(engine) as session:
        yield session


def _add_analyst_result(session, text_hash, model, fit_score, missing_skills):
    session.add(
        AnalystResultRow(
            text_hash=text_hash,
            model=model,
            fit_score=fit_score,
            matched_skills=json.dumps([]),
            missing_skills=json.dumps(missing_skills),
            experience_years_required=None,
            resume_meets_experience=False,
            verdict="weak",
            reasoning="test row",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# missing_skills_below - Q1, pure SQL
# ---------------------------------------------------------------------------


def test_missing_skills_below_counts_across_rows(session):
    _add_analyst_result(session, "h1", GEMINI_MODEL_STAGE1, fit_score=10, missing_skills=["Docker", "Kubernetes"])
    _add_analyst_result(session, "h2", GEMINI_MODEL_STAGE1, fit_score=20, missing_skills=["Docker"])
    _add_analyst_result(session, "h3", GEMINI_MODEL_STAGE1, fit_score=39, missing_skills=["Go"])

    report = missing_skills_below(session, threshold=40)

    assert report.job_count == 3
    assert report.skill_counts[0] == ("Docker", 2)
    assert ("Kubernetes", 1) in report.skill_counts
    assert ("Go", 1) in report.skill_counts


def test_missing_skills_below_excludes_scores_at_or_above_threshold(session):
    _add_analyst_result(session, "h1", GEMINI_MODEL_STAGE1, fit_score=39, missing_skills=["Docker"])
    _add_analyst_result(session, "h2", GEMINI_MODEL_STAGE1, fit_score=40, missing_skills=["Kubernetes"])

    report = missing_skills_below(session, threshold=40)

    assert report.job_count == 1
    assert report.skill_counts == [("Docker", 1)]


def test_missing_skills_below_ignores_stage2_rows(session):
    """The deliberate stage-1-only tie-break: a stage-2 row for the same or
    a different job must never enter this aggregate, even if its fit_score
    also clears the threshold - mixing models into one aggregate would mean
    different rows were judged by different models depending on rank."""
    _add_analyst_result(session, "h1", GEMINI_MODEL_STAGE1, fit_score=10, missing_skills=["Docker"])
    _add_analyst_result(session, "h2", GEMINI_MODEL_STAGE2, fit_score=5, missing_skills=["Kubernetes"])

    report = missing_skills_below(session, threshold=40)

    assert report.job_count == 1
    assert report.skill_counts == [("Docker", 1)]


def test_missing_skills_below_empty_when_nothing_qualifies(session):
    report = missing_skills_below(session, threshold=40)
    assert report.job_count == 0
    assert report.skill_counts == []


# ---------------------------------------------------------------------------
# market_gap - Q2/Q3, retrieval + synthesis
# ---------------------------------------------------------------------------


class FakeLLMClient(LLMClient):
    def __init__(self, answer: str):
        self._answer = answer
        self.calls: list[tuple[str, str, dict]] = []

    @property
    def model_name(self) -> str:
        return "fake-model"

    def complete(self, system_instruction: str, user_message: str, response_schema: dict) -> str:
        self.calls.append((system_instruction, user_message, response_schema))
        return json.dumps({"answer": self._answer})


def _fake_embed_factory(vector_map, default=(0.0, 0.0)):
    def fake_embed(texts):
        return np.array([vector_map.get(text, default) for text in texts], dtype=np.float32)

    return fake_embed


def test_market_gap_scopes_to_rule_filter_survivors_only(session, monkeypatch):
    """A senior/non-engineering/non-India posting must never reach the
    retrieval pool, even though its text would embed and retrieve just
    fine - the population is deterministic (filter_jobs), not left to
    semantic similarity to decide."""
    survivor = _make_posting(
        source_job_id="1",
        title="Software Engineer",
        location="Bangalore, India",
        description="Requirements: Python, FastAPI, PostgreSQL.",
    )
    rejected = _make_posting(
        source_job_id="2",
        title="Senior Software Engineer",
        location="Bangalore, India",
        description="Requirements: 10+ years leading engineering teams, Kubernetes at massive scale.",
    )
    upsert_job(session, survivor, rejection_rule=None, experience_years_required=None)
    upsert_job(session, rejected, rejection_rule="seniority", experience_years_required=None)
    session.commit()

    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory({}, default=(1.0, 0.0)))
    monkeypatch.setattr(coach_module, "prepare_resume_text", lambda: "Python")

    client = FakeLLMClient("some answer")
    market_gap("what's missing?", session, client, k=30)

    _, user_message, _ = client.calls[0]
    assert "PostgreSQL" in user_message  # survivor's extracted requirements present
    # the rejected (seniority-filtered) posting's distinguishing text must not appear
    assert "leading engineering teams" not in user_message


def test_market_gap_resume_enters_only_at_synthesis_not_retrieval(session, monkeypatch):
    """Retrieval must rank by similarity to the QUESTION, not the resume -
    verified by making the resume's embedding maximally dissimilar from
    every candidate and confirming retrieval still returns them (ranked by
    query similarity, which is set to match)."""
    job = _make_posting(source_job_id="1")
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    extracted_requirements_text = "Python, FastAPI, PostgreSQL."  # extract_jd_requirements strips the header itself
    vector_map = {
        "does the market want things I lack?": (1.0, 0.0),
        extracted_requirements_text: (1.0, 0.0),  # aligned with the QUESTION
    }
    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory(vector_map, default=(0.0, 1.0)))
    # resume text is never passed through embed_texts/get_job_embeddings at
    # all in market_gap - only used in the LLM user_message - so there is
    # nothing to align it with here; this test documents that expectation.
    monkeypatch.setattr(coach_module, "prepare_resume_text", lambda: "totally unrelated resume text")

    client = FakeLLMClient("some answer")
    report = market_gap("does the market want things I lack?", session, client, k=30)

    assert report.retrieval.retrieved_count == 1
    assert report.retrieval.chunks[0][1] == pytest.approx(1.0)


def test_market_gap_reports_noop_when_pool_smaller_than_k(session, monkeypatch):
    job = _make_posting(source_job_id="1")
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory({}, default=(1.0, 0.0)))
    monkeypatch.setattr(coach_module, "prepare_resume_text", lambda: "Python")

    client = FakeLLMClient("some answer")
    report = market_gap("question", session, client, k=30)

    assert report.retrieval.pool_size == 1
    assert report.retrieval.retrieved_count == 1
    assert report.retrieval.is_noop is True


def test_market_gap_dedupes_identical_postings_by_description_hash(session, monkeypatch):
    """The same job re-fetched on two different nights (same description,
    same content_hash-independent description_hash) must only occupy one
    retrieval slot, not two."""
    job_a = _make_posting(source_job_id="1", company="Acme")
    job_b = _make_posting(source_job_id="2", company="Acme")  # different source_job_id, identical description
    upsert_job(session, job_a, rejection_rule=None, experience_years_required=None)
    upsert_job(session, job_b, rejection_rule=None, experience_years_required=None)
    session.commit()

    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory({}, default=(1.0, 0.0)))
    monkeypatch.setattr(coach_module, "prepare_resume_text", lambda: "Python")

    client = FakeLLMClient("some answer")
    report = market_gap("question", session, client, k=30)

    assert report.retrieval.pool_size == 1


def test_market_gap_parses_answer_from_structured_response(session, monkeypatch):
    job = _make_posting(source_job_id="1")
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory({}, default=(1.0, 0.0)))
    monkeypatch.setattr(coach_module, "prepare_resume_text", lambda: "Python")

    client = FakeLLMClient("Kubernetes and Go show up repeatedly.")
    report = market_gap("question", session, client, k=30)

    assert report.answer == "Kubernetes and Go show up repeatedly."
