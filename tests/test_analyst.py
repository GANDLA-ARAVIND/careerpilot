import json

import pytest
from sqlalchemy.orm import Session

from agents.analyst import AnalystResult, analyze, derive_outcome, derive_verdict, is_unscored
from db import AnalystResultRow, get_engine
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


def _canned_json(fit_score=70, matched=None, missing=None, years_required=None, resume_meets_it=True, reasoning="Reasonable overlap."):
    return json.dumps(
        {
            "fit_score": fit_score,
            "matched_skills": matched if matched is not None else ["Python"],
            "missing_skills": missing if missing is not None else ["PostgreSQL"],
            "experience_gap": {"years_required": years_required, "resume_meets_it": resume_meets_it},
            "reasoning": reasoning,
        }
    )


class FakeLLMClient(LLMClient):
    """Records calls and returns a canned response, so tests never touch
    the network - same principle as ranking.py's tests replacing _embed."""

    def __init__(self, response_text: str, model_name: str = "fake-model"):
        self.response_text = response_text
        self._model_name = model_name
        self.calls: list[tuple[str, str, dict]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    def complete(self, system_instruction: str, user_message: str, response_schema: dict) -> str:
        self.calls.append((system_instruction, user_message, response_schema))
        return self.response_text


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# derive_verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fit_score,expected",
    [(100, "strong"), (75, "strong"), (74, "possible"), (40, "possible"), (39, "weak"), (0, "weak")],
)
def test_derive_verdict_thresholds(fit_score, expected):
    assert derive_verdict(fit_score) == expected


# ---------------------------------------------------------------------------
# is_unscored / derive_outcome - a job the model had no concrete technical
# requirements to compare the resume against (empty matched AND empty
# missing) is a fourth, distinct outcome, not a score. See docs/decisions.md
# (the Broccoli "Software Engineer" case: the extractor correctly found the
# posting's requirements section, and that section genuinely states no
# concrete technology - not a header-recognition bug).
# ---------------------------------------------------------------------------


def test_is_unscored_true_when_both_lists_empty():
    result = AnalystResult.model_validate_json(_canned_json(fit_score=60, matched=[], missing=[]))
    assert is_unscored(result) is True


@pytest.mark.parametrize(
    "matched,missing",
    [(["Python"], []), ([], ["Python"]), (["Python"], ["Go"])],
)
def test_is_unscored_false_when_either_list_has_an_entry(matched, missing):
    result = AnalystResult.model_validate_json(_canned_json(matched=matched, missing=missing))
    assert is_unscored(result) is False


def test_derive_outcome_returns_unscored_regardless_of_fit_score_value():
    """The whole point: a fabricated fit_score of 60 (mid-range, "possible"
    by derive_verdict alone) must not silently become a "possible" verdict
    just because both lists happen to be empty."""
    high = AnalystResult.model_validate_json(_canned_json(fit_score=60, matched=[], missing=[]))
    low = AnalystResult.model_validate_json(_canned_json(fit_score=0, matched=[], missing=[]))
    assert derive_outcome(high) == "unscored"
    assert derive_outcome(low) == "unscored"


def test_derive_outcome_falls_back_to_derive_verdict_when_scored():
    result = AnalystResult.model_validate_json(_canned_json(fit_score=80, matched=["Python"], missing=[]))
    assert derive_outcome(result) == derive_verdict(80) == "strong"


# ---------------------------------------------------------------------------
# analyze - cache miss / hit / staleness
# ---------------------------------------------------------------------------


def test_analyze_cache_miss_calls_client_and_stores(session):
    client = FakeLLMClient(_canned_json(fit_score=82))
    job = _make_posting()

    result, from_cache = analyze(job, "resume text", client, session)

    assert from_cache is False
    assert result.fit_score == 82
    assert len(client.calls) == 1
    assert session.query(AnalystResultRow).count() == 1


def test_analyze_cache_hit_skips_client_call(session):
    client = FakeLLMClient(_canned_json(fit_score=82))
    job = _make_posting()

    analyze(job, "resume text", client, session)
    result, from_cache = analyze(job, "resume text", client, session)

    assert from_cache is True
    assert result.fit_score == 82
    assert len(client.calls) == 1  # not called a second time


def test_analyze_different_resume_text_is_a_cache_miss(session):
    """Mirrors the embedding-cache regression test: the cache key must
    reflect the exact text sent, not the job's identity - a resume edit
    must not silently serve a stale verdict."""
    client = FakeLLMClient(_canned_json())
    job = _make_posting()

    analyze(job, "resume version one", client, session)
    analyze(job, "resume version two", client, session)

    assert len(client.calls) == 2
    assert session.query(AnalystResultRow).count() == 2


def test_analyze_different_requirements_text_is_a_cache_miss(session):
    client = FakeLLMClient(_canned_json())
    job_a = _make_posting(source_job_id="1", description="Requirements: Python.")
    job_b = _make_posting(source_job_id="2", description="Requirements: Java.")

    analyze(job_a, "resume text", client, session)
    analyze(job_b, "resume text", client, session)

    assert len(client.calls) == 2
    assert session.query(AnalystResultRow).count() == 2


def test_two_models_on_the_same_job_do_not_collide(session):
    """The two-stage design's core requirement: a cheap model (stage 1) and
    a stronger model (stage 2) analyzing the exact same (job, resume) pair
    must produce two independent cache entries, not one overwriting the
    other. Without model in the cache key, stage 2 would either silently
    reuse stage 1's cheaper verdict or clobber it."""
    job = _make_posting()

    cheap_client = FakeLLMClient(_canned_json(fit_score=20), model_name="gemini-3.5-flash-lite")
    strong_client = FakeLLMClient(_canned_json(fit_score=80), model_name="gemini-2.5-flash")

    cheap_result, cheap_from_cache = analyze(job, "resume text", cheap_client, session)
    strong_result, strong_from_cache = analyze(job, "resume text", strong_client, session)

    assert cheap_from_cache is False
    assert strong_from_cache is False
    assert cheap_result.fit_score == 20
    assert strong_result.fit_score == 80
    assert session.query(AnalystResultRow).count() == 2

    rows = session.query(AnalystResultRow).all()
    assert {row.model for row in rows} == {"gemini-3.5-flash-lite", "gemini-2.5-flash"}

    # re-running each against its own model is still a cache hit
    _, cheap_from_cache_again = analyze(job, "resume text", cheap_client, session)
    _, strong_from_cache_again = analyze(job, "resume text", strong_client, session)
    assert cheap_from_cache_again is True
    assert strong_from_cache_again is True
    assert len(cheap_client.calls) == 1
    assert len(strong_client.calls) == 1


def test_changing_system_instruction_is_a_cache_miss(session, monkeypatch):
    """Regression test for the same class of bug already found once in
    ranking.py's embedding cache: the cache key must reflect everything that
    determines the output. A prompt tune (a new rule, a reworded constraint)
    changes what a fixed (job, resume) pair would produce even though
    neither text input changed - the old cache key (resume+requirements
    only) would have silently kept serving the pre-tune verdict."""
    import agents.analyst as analyst_module

    client = FakeLLMClient(_canned_json())
    job = _make_posting()

    analyze(job, "resume text", client, session)
    assert len(client.calls) == 1

    monkeypatch.setattr(analyst_module, "SYSTEM_INSTRUCTION", "A completely different prompt.")
    analyze(job, "resume text", client, session)

    assert len(client.calls) == 2  # not served from the old prompt's cache entry
    assert session.query(AnalystResultRow).count() == 2


def test_analyze_sends_only_extracted_requirements_not_full_description(session):
    client = FakeLLMClient(_canned_json())
    job = _make_posting(
        description=(
            "About Acme: we are a fast-growing company with 10 years in the industry.\n\n"
            "Requirements\nPython, FastAPI, PostgreSQL.\n\n"
            "Benefits\nHealth insurance and PTO."
        )
    )

    analyze(job, "resume text", client, session)

    _, user_message, _ = client.calls[0]
    assert "Python, FastAPI, PostgreSQL" in user_message
    assert "fast-growing company" not in user_message
    assert "Health insurance" not in user_message


def test_analyze_result_persists_experience_gap_fields(session):
    client = FakeLLMClient(_canned_json(years_required=3.0, resume_meets_it=False))
    job = _make_posting()

    result, _ = analyze(job, "resume text", client, session)

    assert result.experience_gap.years_required == 3.0
    assert result.experience_gap.resume_meets_it is False

    row = session.query(AnalystResultRow).first()
    assert row.experience_years_required == 3.0
    assert row.resume_meets_experience is False
    assert row.verdict == derive_verdict(result.fit_score)


def test_analyze_stores_unscored_verdict_when_both_lists_empty(session):
    """The Broccoli case, end to end through the real cache-write path:
    a response with empty matched AND missing must be stored with
    verdict="unscored", not whatever derive_verdict would say about its
    (fabricated) fit_score."""
    client = FakeLLMClient(_canned_json(fit_score=60, matched=[], missing=[]))
    job = _make_posting()

    analyze(job, "resume text", client, session)

    row = session.query(AnalystResultRow).first()
    assert row.verdict == "unscored"
    assert row.fit_score == 60  # still stored as-is for diagnostics - callers decide not to trust it, not this layer


# ---------------------------------------------------------------------------
# AnalystResult schema validation
# ---------------------------------------------------------------------------


def test_analyst_result_rejects_extra_fields():
    """extra="forbid" - a field the model adds that isn't in the schema
    should fail loudly, not get silently ignored."""
    payload = json.loads(_canned_json())
    payload["unexpected_field"] = "surprise"
    with pytest.raises(Exception):
        AnalystResult.model_validate(payload)


def test_analyst_result_rejects_fit_score_out_of_range():
    payload = json.loads(_canned_json(fit_score=150))
    with pytest.raises(Exception):
        AnalystResult.model_validate(payload)


def test_analyst_result_accepts_null_years_required():
    payload = json.loads(_canned_json(years_required=None, resume_meets_it=False))
    result = AnalystResult.model_validate(payload)
    assert result.experience_gap.years_required is None
