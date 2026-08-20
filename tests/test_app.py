import io
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

import app as app_module
from agents.analyst import SYSTEM_INSTRUCTION, _text_hash, prepare_resume_text
from app import (
    DashboardJob,
    MISSING_SKILLS_VISIBLE_COUNT,
    _clean_keyword_rows,
    _esc,
    _format_run_progress_message,
    _verdict_css_class,
    check_resume_extraction_quality,
    compute_company_stats,
    extract_pdf_text,
    format_age,
    load_applied_jobs,
    load_dashboard_jobs,
    make_run_progress_handler,
    render_empty_state_html,
    render_job_card_html,
    render_matched_chips_html,
    render_missing_chips_html,
    render_stats_strip_html,
    render_unscored_card_html,
    run_filter_pass,
    set_application_status,
)
from config import GEMINI_MODEL_STAGE1, GEMINI_MODEL_STAGE2
from db import AnalystResultRow, get_engine, upsert_job
from extraction import extract_jd_requirements
from models import ATSSource, CompanyConfig, JobPosting, Preferences
from pipeline import ProgressEvent


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


@pytest.fixture(autouse=True)
def fixed_resume_text(monkeypatch):
    """Every test uses the same resume text, so hashes computed here match
    hashes computed by _add_analyst_result below without touching the real
    data/resume.txt file."""
    monkeypatch.setattr(app_module, "prepare_resume_text", lambda: "Python, FastAPI")
    return "Python, FastAPI"


def _add_analyst_result(session, job, model, resume_text, fit_score, matched=None, missing=None):
    """Defaults to a non-empty matched_skills - tests that don't care about
    skill content still need a "real" (not unscored) row unless they
    explicitly pass matched=[]/missing=[] to test that path."""
    requirements_text, _ = extract_jd_requirements(job.description)
    text_hash = _text_hash(model, SYSTEM_INSTRUCTION, resume_text, requirements_text)
    session.add(
        AnalystResultRow(
            text_hash=text_hash,
            model=model,
            fit_score=fit_score,
            matched_skills=json.dumps(matched if matched is not None else ["Python"]),
            missing_skills=json.dumps(missing if missing is not None else []),
            experience_years_required=None,
            resume_meets_experience=False,
            verdict="weak",
            reasoning="test reasoning",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    session.commit()
    return text_hash


# ---------------------------------------------------------------------------
# load_dashboard_jobs
# ---------------------------------------------------------------------------


def test_prefers_stage2_score_over_stage1_for_the_same_job(session, fixed_resume_text):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    _add_analyst_result(session, job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=40)
    _add_analyst_result(session, job, GEMINI_MODEL_STAGE2, fixed_resume_text, fit_score=85)
    session.commit()

    scored, unscored, unanalyzed = load_dashboard_jobs(session, kept=[job], last_viewed_cutoff=None)

    assert len(scored) == 1
    assert scored[0].fit_score == 85
    assert scored[0].model == GEMINI_MODEL_STAGE2


def test_falls_back_to_stage1_when_no_stage2_result(session, fixed_resume_text):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    _add_analyst_result(session, job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=40)
    session.commit()

    scored, unscored, unanalyzed = load_dashboard_jobs(session, kept=[job], last_viewed_cutoff=None)

    assert len(scored) == 1
    assert scored[0].fit_score == 40
    assert scored[0].model == GEMINI_MODEL_STAGE1


def test_ignores_jobs_not_in_kept_even_with_a_cached_score(session, fixed_resume_text):
    """load_dashboard_jobs trusts the passed-in `kept` list completely - it
    no longer does its own rule filtering (see run_filter_pass, which owns
    that now). A job simply absent from `kept` must never appear, even if
    an AnalystResultRow exists for its exact text (e.g. a stray leftover
    from before a filter tune)."""
    job = _make_posting(title="Senior Software Engineer")
    upsert_job(session, job, rejection_rule="seniority", experience_years_required=None)
    _add_analyst_result(session, job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=90)
    session.commit()

    scored, unscored, unanalyzed = load_dashboard_jobs(session, kept=[], last_viewed_cutoff=None)

    assert scored == []
    assert unscored == []
    assert unanalyzed == 0


def test_counts_survivors_with_no_score_yet_rather_than_dropping_silently(session, fixed_resume_text):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    scored, unscored, unanalyzed = load_dashboard_jobs(session, kept=[job], last_viewed_cutoff=None)

    assert scored == []
    assert unscored == []
    assert unanalyzed == 1


def test_sorts_by_fit_score_descending(session, fixed_resume_text):
    job_a = _make_posting(source_job_id="1", company="Acme", description="Requirements: Python.")
    job_b = _make_posting(source_job_id="2", company="Globex", description="Requirements: Java.")
    upsert_job(session, job_a, rejection_rule=None, experience_years_required=None)
    upsert_job(session, job_b, rejection_rule=None, experience_years_required=None)
    _add_analyst_result(session, job_a, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=30)
    _add_analyst_result(session, job_b, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=90)
    session.commit()

    scored, _, _ = load_dashboard_jobs(session, kept=[job_a, job_b], last_viewed_cutoff=None)

    assert [dj.fit_score for dj in scored] == [90, 30]


def test_is_new_when_first_seen_after_cutoff(session, fixed_resume_text):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    _add_analyst_result(session, job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=50)
    session.commit()

    long_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    scored, _, _ = load_dashboard_jobs(session, kept=[job], last_viewed_cutoff=long_ago)

    assert scored[0].is_new is True


def test_is_not_new_when_first_seen_before_cutoff(session, fixed_resume_text):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    _add_analyst_result(session, job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=50)
    session.commit()

    far_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)
    scored, _, _ = load_dashboard_jobs(session, kept=[job], last_viewed_cutoff=far_future)

    assert scored[0].is_new is False


def test_everything_is_new_when_no_prior_cutoff_exists(session, fixed_resume_text):
    """First-ever dashboard open - nothing has been "seen" before, so
    everything currently scored is new by definition."""
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    _add_analyst_result(session, job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=50)
    session.commit()

    scored, _, _ = load_dashboard_jobs(session, kept=[job], last_viewed_cutoff=None)

    assert scored[0].is_new is True


def test_empty_kept_returns_empty_without_querying(session, fixed_resume_text):
    """No survivors at all - must short-circuit cleanly, not run an empty
    IN (...) query (some SQL dialects reject that outright)."""
    scored, unscored, unanalyzed = load_dashboard_jobs(session, kept=[], last_viewed_cutoff=None)

    assert (scored, unscored, unanalyzed) == ([], [], 0)


# ---------------------------------------------------------------------------
# load_dashboard_jobs - unscored (empty matched AND empty missing)
# ---------------------------------------------------------------------------


def test_unscored_job_excluded_from_scored_list_and_has_no_fit_score(session, fixed_resume_text):
    """The Broccoli case: both lists empty must never sit in `scored`
    alongside real fit_scores, fabricated or not."""
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    _add_analyst_result(session, job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=60, matched=[], missing=[])
    session.commit()

    scored, unscored, unanalyzed = load_dashboard_jobs(session, kept=[job], last_viewed_cutoff=None)

    assert scored == []
    assert unanalyzed == 0
    assert len(unscored) == 1
    assert unscored[0].fit_score is None
    assert unscored[0].is_unscored is True


def test_unscored_job_detected_regardless_of_stored_verdict_column(session, fixed_resume_text):
    """load_dashboard_jobs checks the parsed lists directly, not the stored
    verdict string - so an older row that predates the backfill (still
    saying "possible") is still correctly classified as unscored."""
    job = _make_posting()
    text_hash = _add_analyst_result(
        session, job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=60, matched=[], missing=[]
    )
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    row = session.get(AnalystResultRow, text_hash)
    row.verdict = "possible"  # simulate a pre-backfill stale row
    session.commit()

    scored, unscored, _ = load_dashboard_jobs(session, kept=[job], last_viewed_cutoff=None)

    assert scored == []
    assert len(unscored) == 1


def test_scored_and_unscored_jobs_coexist_independently(session, fixed_resume_text):
    scored_job = _make_posting(source_job_id="1", company="Acme", description="Requirements: Python.")
    unscored_job = _make_posting(source_job_id="2", company="Globex", description="Requirements: Java.")
    upsert_job(session, scored_job, rejection_rule=None, experience_years_required=None)
    upsert_job(session, unscored_job, rejection_rule=None, experience_years_required=None)
    _add_analyst_result(session, scored_job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=70, matched=["Python"])
    _add_analyst_result(session, unscored_job, GEMINI_MODEL_STAGE1, fixed_resume_text, fit_score=0, matched=[], missing=[])
    session.commit()

    scored, unscored, _ = load_dashboard_jobs(session, kept=[scored_job, unscored_job], last_viewed_cutoff=None)

    assert len(scored) == 1 and scored[0].job.company == "Acme"
    assert len(unscored) == 1 and unscored[0].job.company == "Globex"


# ---------------------------------------------------------------------------
# run_filter_pass
# ---------------------------------------------------------------------------


def test_run_filter_pass_splits_kept_and_rejected(session):
    survivor = _make_posting(source_job_id="1", company="Acme", title="Software Engineer")
    rejected = _make_posting(source_job_id="2", company="Acme", title="Senior Software Engineer")
    upsert_job(session, survivor, rejection_rule=None, experience_years_required=None)
    upsert_job(session, rejected, rejection_rule="seniority", experience_years_required=None)
    session.commit()

    result = run_filter_pass(session)

    assert result.total_fetched == 2
    assert [job.title for job in result.kept] == ["Software Engineer"]
    assert result.rejected_by == {"seniority": 1}
    assert len(result.rejected) == 1
    assert result.rejected[0].title == "Senior Software Engineer"
    assert result.rejected[0].reason == "seniority"


def test_run_filter_pass_counts_fetched_per_company_including_rejects(session):
    upsert_job(
        session,
        _make_posting(source_job_id="1", company="Acme", title="Software Engineer"),
        rejection_rule=None,
        experience_years_required=None,
    )
    upsert_job(
        session,
        _make_posting(source_job_id="2", company="Acme", title="Senior Software Engineer"),
        rejection_rule="seniority",
        experience_years_required=None,
    )
    upsert_job(
        session,
        _make_posting(source_job_id="3", company="Globex", title="Product Manager"),
        rejection_rule="not_allowlisted",
        experience_years_required=None,
    )
    session.commit()

    result = run_filter_pass(session)

    assert result.fetched_by_company == {"Acme": 2, "Globex": 1}


def test_run_filter_pass_kept_jobs_are_full_job_postings_with_description(session):
    """kept must be real, full JobPosting objects (description included) -
    only the rejected/lightweight path skips description; survivors still
    need it for extraction/analysis downstream."""
    job = _make_posting(description="Requirements: Python, FastAPI.")
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    result = run_filter_pass(session)

    assert result.kept[0].description == "Requirements: Python, FastAPI."


def test_run_filter_pass_empty_database_returns_empty_result(session):
    result = run_filter_pass(session)

    assert result.total_fetched == 0
    assert result.kept == []
    assert result.rejected == []
    assert result.rejected_by == {}


def test_run_filter_pass_accepts_preferences_override_for_roles_tab_preview(session):
    """The Roles tab's "preview impact before saving" step - a candidate
    Preferences changes what survives without touching config.* globals,
    so the live pass (no override) is unaffected."""
    job = _make_posting(title="Zzznotarealtitle")
    upsert_job(session, job, rejection_rule="not_allowlisted", experience_years_required=None)
    session.commit()

    live_result = run_filter_pass(session)
    assert live_result.kept == []

    candidate = Preferences(
        title_allowlist=["zzznotarealtitle"],
        seniority_keywords=["senior"],
        non_engineering_keywords=["sales"],
        india_location_keywords=["karnataka"],
    )
    preview_result = run_filter_pass(session, candidate)
    assert [j.title for j in preview_result.kept] == ["Zzznotarealtitle"]


# ---------------------------------------------------------------------------
# _clean_keyword_rows - the Roles tab's st.data_editor return-value cleanup
# ---------------------------------------------------------------------------


def test_clean_keyword_rows_strips_and_drops_blanks():
    rows = [{"keyword": "  python  "}, {"keyword": ""}, {"keyword": "   "}, {"keyword": "sql"}]
    assert _clean_keyword_rows(rows) == ["python", "sql"]


def test_clean_keyword_rows_deduplicates_case_insensitively_keeping_first():
    rows = [{"keyword": "Python"}, {"keyword": "python"}, {"keyword": "PYTHON"}]
    assert _clean_keyword_rows(rows) == ["Python"]


def test_clean_keyword_rows_preserves_order_not_sorted():
    rows = [{"keyword": "zebra"}, {"keyword": "apple"}]
    assert _clean_keyword_rows(rows) == ["zebra", "apple"]


def test_clean_keyword_rows_handles_missing_keyword_key():
    """st.data_editor's trailing blank row while the user is mid-add can
    come back with no "keyword" key at all, not just an empty string."""
    rows = [{"keyword": "python"}, {}]
    assert _clean_keyword_rows(rows) == ["python"]


def test_clean_keyword_rows_empty_input_returns_empty_list():
    assert _clean_keyword_rows([]) == []


# ---------------------------------------------------------------------------
# set_application_status
# ---------------------------------------------------------------------------


def test_set_application_status_updates_only_that_row(tmp_path):
    from db import JobPostingRow

    engine = get_engine(tmp_path / "test.db")
    with Session(engine) as session:
        job_a = _make_posting(source_job_id="1", company="Acme")
        job_b = _make_posting(source_job_id="2", company="Globex")
        upsert_job(session, job_a, rejection_rule=None, experience_years_required=None)
        upsert_job(session, job_b, rejection_rule=None, experience_years_required=None)
        session.commit()

    set_application_status(engine, job_a.content_hash, "applied")

    with Session(engine) as session:
        row_a = session.get(JobPostingRow, job_a.content_hash)
        row_b = session.get(JobPostingRow, job_b.content_hash)
        assert row_a.application_status == "applied"
        assert row_b.application_status == "new"


def test_set_application_status_on_unknown_hash_does_nothing(tmp_path):
    engine = get_engine(tmp_path / "test2.db")
    set_application_status(engine, "does-not-exist", "applied")  # must not raise


def test_set_application_status_to_applied_sets_applied_at(tmp_path):
    from db import JobPostingRow

    engine = get_engine(tmp_path / "test3.db")
    with Session(engine) as session:
        job = _make_posting()
        upsert_job(session, job, rejection_rule=None, experience_years_required=None)
        session.commit()

    set_application_status(engine, job.content_hash, "applied")

    with Session(engine) as session:
        row = session.get(JobPostingRow, job.content_hash)
        assert row.applied_at is not None


def test_set_application_status_non_applied_leaves_applied_at_null(tmp_path):
    from db import JobPostingRow

    engine = get_engine(tmp_path / "test4.db")
    with Session(engine) as session:
        job = _make_posting()
        upsert_job(session, job, rejection_rule=None, experience_years_required=None)
        session.commit()

    set_application_status(engine, job.content_hash, "interviewing")

    with Session(engine) as session:
        row = session.get(JobPostingRow, job.content_hash)
        assert row.applied_at is None


def test_set_application_status_does_not_overwrite_original_applied_at(tmp_path):
    """Re-clicking "applied" (or round-tripping through another status and
    back) must not reset the original application date."""
    from db import JobPostingRow

    engine = get_engine(tmp_path / "test5.db")
    with Session(engine) as session:
        job = _make_posting()
        upsert_job(session, job, rejection_rule=None, experience_years_required=None)
        session.commit()

    set_application_status(engine, job.content_hash, "applied")
    with Session(engine) as session:
        first_applied_at = session.get(JobPostingRow, job.content_hash).applied_at

    set_application_status(engine, job.content_hash, "applied")
    with Session(engine) as session:
        second_applied_at = session.get(JobPostingRow, job.content_hash).applied_at

    assert first_applied_at == second_applied_at


def test_set_application_status_away_from_applied_does_not_clear_applied_at():
    """A job applied to and then marked rejected is still a job that was
    applied to - see set_application_status's docstring. The Applied tab
    lists by applied_at, not current status, on purpose."""
    from db import JobPostingRow

    engine = get_engine(":memory:")
    with Session(engine) as session:
        job = _make_posting()
        upsert_job(session, job, rejection_rule=None, experience_years_required=None)
        session.commit()

    set_application_status(engine, job.content_hash, "applied")
    set_application_status(engine, job.content_hash, "rejected")

    with Session(engine) as session:
        row = session.get(JobPostingRow, job.content_hash)
        assert row.application_status == "rejected"
        assert row.applied_at is not None  # preserved, not cleared


# ---------------------------------------------------------------------------
# load_applied_jobs
# ---------------------------------------------------------------------------


def test_load_applied_jobs_empty_when_nothing_applied(session):
    upsert_job(session, _make_posting(), rejection_rule=None, experience_years_required=None)
    session.commit()

    assert load_applied_jobs(session) == []


def test_load_applied_jobs_excludes_jobs_never_marked_applied(session):
    engine = get_engine(":memory:")
    with Session(engine) as inner_session:
        applied_job = _make_posting(source_job_id="1", company="Acme")
        untouched_job = _make_posting(source_job_id="2", company="Globex")
        upsert_job(inner_session, applied_job, rejection_rule=None, experience_years_required=None)
        upsert_job(inner_session, untouched_job, rejection_rule=None, experience_years_required=None)
        inner_session.commit()

    set_application_status(engine, applied_job.content_hash, "applied")

    with Session(engine) as inner_session:
        result = load_applied_jobs(inner_session)

    assert [aj.company for aj in result] == ["Acme"]


def test_load_applied_jobs_sorted_most_recent_first():
    engine = get_engine(":memory:")
    with Session(engine) as session:
        older = _make_posting(source_job_id="1", company="Acme")
        newer = _make_posting(source_job_id="2", company="Globex")
        upsert_job(session, older, rejection_rule=None, experience_years_required=None)
        upsert_job(session, newer, rejection_rule=None, experience_years_required=None)
        session.commit()

    set_application_status(engine, older.content_hash, "applied")
    set_application_status(engine, newer.content_hash, "applied")
    # force a real, distinguishable ordering rather than relying on two
    # datetime.now() calls microseconds apart
    with Session(engine) as session:
        from db import JobPostingRow

        row_older = session.get(JobPostingRow, older.content_hash)
        row_older.applied_at = row_older.applied_at.replace(year=2020)
        session.commit()

    with Session(engine) as session:
        result = load_applied_jobs(session)

    assert [aj.company for aj in result] == ["Globex", "Acme"]  # newer first


def test_load_applied_jobs_includes_a_job_later_marked_rejected():
    """The specific behavior set_application_status's docstring promises -
    an outcome after applying must not remove the job from this list."""
    engine = get_engine(":memory:")
    with Session(engine) as session:
        job = _make_posting()
        upsert_job(session, job, rejection_rule=None, experience_years_required=None)
        session.commit()

    set_application_status(engine, job.content_hash, "applied")
    set_application_status(engine, job.content_hash, "rejected")

    with Session(engine) as session:
        result = load_applied_jobs(session)

    assert len(result) == 1
    assert result[0].application_status == "rejected"


# ---------------------------------------------------------------------------
# format_age
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta,expected_substring",
    [
        (timedelta(minutes=5), "m"),
        (timedelta(hours=3), "h"),
        (timedelta(days=3), "d"),
    ],
)
def test_format_age_uses_the_right_unit(delta, expected_substring):
    assert format_age(delta).endswith(expected_substring)


# ---------------------------------------------------------------------------
# compute_company_stats
# ---------------------------------------------------------------------------


def test_compute_company_stats_matches_fetched_and_survivors_by_name():
    companies = [
        CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme"),
        CompanyConfig(name="Globex", ats=ATSSource.LEVER, token="globex"),
    ]
    fetched_by_company = Counter({"Acme": 2, "Globex": 1})
    kept = [_make_posting(source_job_id="1", company="Acme", title="Software Engineer")]

    stats = compute_company_stats(companies, fetched_by_company, kept)

    by_name = {s.name: s for s in stats}
    assert by_name["Acme"].jobs_fetched == 2
    assert by_name["Acme"].survivors == 1
    assert by_name["Globex"].jobs_fetched == 1
    assert by_name["Globex"].survivors == 0


def test_compute_company_stats_zero_for_a_company_never_fetched():
    companies = [CompanyConfig(name="Untouched", ats=ATSSource.ASHBY, token="untouched")]

    stats = compute_company_stats(companies, Counter(), [])

    assert stats[0].jobs_fetched == 0
    assert stats[0].survivors == 0


# ---------------------------------------------------------------------------
# _format_run_progress_message / make_run_progress_handler - the Run tab's
# pipeline.ProgressEvent -> UI mapping
# ---------------------------------------------------------------------------


def test_format_run_progress_message_fetch_start_has_no_count():
    event = ProgressEvent(stage="fetch", message="x", total=62)
    assert _format_run_progress_message(event) == "Fetching from 62 companies..."


def test_format_run_progress_message_fetch_live_count():
    event = ProgressEvent(stage="fetch", message="x", current=12, total=62)
    assert _format_run_progress_message(event) == "Fetching from 62 companies... (12/62)"


def test_format_run_progress_message_filter_before_counts_known():
    assert _format_run_progress_message(ProgressEvent(stage="filter", message="x")) == "Filtering..."


def test_format_run_progress_message_filter_with_counts():
    event = ProgressEvent(stage="filter", message="x", extra={"kept": 41, "total": 7500})
    assert _format_run_progress_message(event) == "Filtering... 41 of 7500 survived."


def test_format_run_progress_message_names_analyst_explicitly_stage1():
    event = ProgressEvent(stage="stage1", message="x", total=41)
    assert _format_run_progress_message(event) == "Analyst: scoring 41 job(s)..."
    assert "Analyst" in _format_run_progress_message(event)


def test_format_run_progress_message_stage2_says_deep_pass():
    event = ProgressEvent(stage="stage2", message="x", total=15)
    message = _format_run_progress_message(event)
    assert "Analyst (deep pass)" in message
    assert "15" in message


def test_format_run_progress_message_unrecognized_stage_falls_back_to_raw_message():
    event = ProgressEvent(stage="something_new", message="a future stage's own text")
    assert _format_run_progress_message(event) == "a future stage's own text"


class _FakeStatus:
    """A minimal stand-in for Streamlit's StatusContainer - just enough to
    verify make_run_progress_handler's call pattern, not a real widget."""

    def __init__(self):
        self.labels: list[str] = []
        self.written: list[str] = []

    def update(self, label=None, **kwargs):
        self.labels.append(label)

    def write(self, text):
        self.written.append(text)


class _FakeQuotaBox:
    def __init__(self):
        self.captions: list[str] = []

    def caption(self, text):
        self.captions.append(text)


def test_run_progress_handler_updates_label_on_every_event():
    status = _FakeStatus()
    handler, _quota = make_run_progress_handler(status, _FakeQuotaBox())

    handler(ProgressEvent(stage="fetch", message="x", current=1, total=3))
    handler(ProgressEvent(stage="fetch", message="x", current=2, total=3))

    assert len(status.labels) == 2


def test_run_progress_handler_writes_body_line_only_once_per_stage():
    """41 per-job stage-1 events must not flood the status body with 41
    lines - only the first event for a given stage gets a body line; the
    live label already carries the count."""
    status = _FakeStatus()
    handler, _quota = make_run_progress_handler(status, _FakeQuotaBox())

    for i in range(1, 42):
        handler(ProgressEvent(stage="stage1", message="x", current=i, total=41))

    assert len(status.written) == 1


def test_run_progress_handler_writes_a_new_line_when_stage_changes():
    status = _FakeStatus()
    handler, _quota = make_run_progress_handler(status, _FakeQuotaBox())

    handler(ProgressEvent(stage="fetch", message="x", total=3))
    handler(ProgressEvent(stage="filter", message="x"))
    handler(ProgressEvent(stage="stage1", message="x", total=5))

    assert len(status.written) == 3


def test_run_progress_handler_tracks_quota_per_stage():
    status = _FakeStatus()
    quota_box = _FakeQuotaBox()
    handler, quota_state = make_run_progress_handler(status, quota_box)

    handler(ProgressEvent(stage="stage1", message="x", current=1, total=2, extra={"call_count": 1, "rpd": 500}))
    handler(ProgressEvent(stage="stage2", message="x", current=1, total=1, extra={"call_count": 1, "rpd": 20}))

    assert quota_state == {
        "stage1": {"call_count": 1, "rpd": 500},
        "stage2": {"call_count": 1, "rpd": 20},
    }
    assert quota_box.captions  # updated at least once
    assert "stage1" in quota_box.captions[-1] and "stage2" in quota_box.captions[-1]


def test_run_progress_handler_ignores_events_without_quota_info():
    status = _FakeStatus()
    quota_box = _FakeQuotaBox()
    handler, quota_state = make_run_progress_handler(status, quota_box)

    handler(ProgressEvent(stage="fetch", message="x", total=3))  # no call_count in extra

    assert quota_state == {}
    assert quota_box.captions == []


# ---------------------------------------------------------------------------
# check_resume_extraction_quality
# ---------------------------------------------------------------------------


def test_clean_extraction_produces_no_warnings():
    text = "Technical Skills\nPython, SQL, FastAPI.\n\nProjects\nBuilt a thing using normal words and spaces."
    assert check_resume_extraction_quality(text, previous_text=None) == []


def test_flags_unusually_long_words_from_missing_spaces():
    mangled = "TechnicalSkillsPythonSQLFastAPIProjectsBuiltathingusingnormalwordswithnospacesatallhereislongtext"
    warnings = check_resume_extraction_quality(mangled, previous_text=None)
    assert any("long word" in w for w in warnings)


def test_flags_low_space_to_character_ratio():
    # Real words but glued into few long tokens - low space ratio without
    # necessarily tripping the single-longest-word threshold by itself.
    mangled = " ".join(["abcdefghijklmnopqrstuvwxyz1234567890" for _ in range(30)])
    warnings = check_resume_extraction_quality(mangled, previous_text=None)
    # this construction has 1 space per 37 chars - well under MIN_SPACE_RATIO
    assert any("space-to-character" in w.lower() for w in warnings)


def test_flags_major_length_drop_versus_previous_resume():
    previous = "Technical Skills\n" + ("Python, SQL, FastAPI, Docker, Kubernetes. " * 50)
    new_text = "Technical Skills\nPython."
    warnings = check_resume_extraction_quality(new_text, previous_text=previous)
    assert any("down from" in w for w in warnings)


def test_no_length_drop_warning_when_no_previous_resume_exists():
    """First-ever upload - nothing to compare against, so this check must
    not fire just because previous_text is None."""
    warnings = check_resume_extraction_quality("Technical Skills\nPython.", previous_text=None)
    assert not any("down from" in w for w in warnings)


def test_no_length_drop_warning_when_new_text_is_similar_length():
    previous = "Technical Skills\nPython, SQL."
    new_text = "Technical Skills\nPython, SQL, and a bit more."
    warnings = check_resume_extraction_quality(new_text, previous_text=previous)
    assert not any("down from" in w for w in warnings)


def test_empty_text_does_not_crash():
    assert check_resume_extraction_quality("", previous_text=None) == []


# ---------------------------------------------------------------------------
# extract_pdf_text
# ---------------------------------------------------------------------------


def _make_pdf_bytes(text: str) -> bytes:
    """A minimal real PDF with one page of text, built directly with pypdf's
    own writer - not a hand-rolled byte string, so this test exercises the
    same library on both ends and stays valid across pypdf versions."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    stream_obj = DecodedStreamObject()
    stream_obj.set_data(f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1"))
    stream_ref = writer._add_object(stream_obj)
    page[NameObject("/Contents")] = stream_ref

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_ref = writer._add_object(font)

    resources = DictionaryObject()
    font_dict = DictionaryObject()
    font_dict[NameObject("/F1")] = font_ref
    resources[NameObject("/Font")] = font_dict
    page[NameObject("/Resources")] = resources

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_extract_pdf_text_reads_real_pdf_content():
    pdf_bytes = _make_pdf_bytes("Hello from a test PDF")
    assert extract_pdf_text(pdf_bytes) == "Hello from a test PDF"


def test_extract_pdf_text_joins_multiple_pages():
    from pypdf import PdfReader, PdfWriter

    first = PdfReader(io.BytesIO(_make_pdf_bytes("Page one text")))
    second = PdfReader(io.BytesIO(_make_pdf_bytes("Page two text")))
    writer = PdfWriter()
    writer.add_page(first.pages[0])
    writer.add_page(second.pages[0])
    buf = io.BytesIO()
    writer.write(buf)

    result = extract_pdf_text(buf.getvalue())
    assert "Page one text" in result
    assert "Page two text" in result


# ---------------------------------------------------------------------------
# HTML rendering layer - _esc, chips, job cards, stats strip, empty states.
# These are pure string-building functions with no st.* calls, tested the
# same way as the data-logic functions above.
# ---------------------------------------------------------------------------


def _make_dashboard_job(**overrides) -> DashboardJob:
    defaults = dict(
        job=_make_posting(),
        content_hash="abc123",
        fit_score=70,
        verdict="possible",
        matched_skills=["Python"],
        missing_skills=["Docker"],
        years_required=3.0,
        resume_meets_it=False,
        reasoning="Reasonable overlap.",
        model="gemini-3.5-flash-lite",
        application_status="new",
        first_seen=datetime.now(timezone.utc).replace(tzinfo=None),
        is_new=False,
        is_unscored=False,
    )
    defaults.update(overrides)
    return DashboardJob(**defaults)


def test_esc_escapes_html_special_characters():
    """Job titles/company names are external, untrusted input; matched/
    missing skills are LLM output, also untrusted. Both flow into hand-
    built HTML via unsafe_allow_html - this is the only thing standing
    between that and a real injection."""
    assert _esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert _esc("Q&A Engineer") == "Q&amp;A Engineer"
    assert _esc('Say "hi"') == "Say &quot;hi&quot;"
    assert _esc(85) == "85"  # non-string input (fit_score) must not raise


def test_verdict_css_class_maps_known_verdicts_through_unchanged():
    assert _verdict_css_class("strong") == "strong"
    assert _verdict_css_class("possible") == "possible"
    assert _verdict_css_class("weak") == "weak"


def test_verdict_css_class_defaults_unknown_values_to_unscored():
    assert _verdict_css_class("unscored") == "unscored"
    assert _verdict_css_class("anything-else") == "unscored"


def test_render_matched_chips_html_escapes_each_skill():
    result = render_matched_chips_html(["Python", "<b>Java</b>"])
    assert "cp-chip-matched" in result
    assert "&lt;b&gt;Java&lt;/b&gt;" in result
    assert "<b>Java</b>" not in result  # never unescaped


def test_render_matched_chips_html_empty_list_shows_explicit_message():
    result = render_matched_chips_html([])
    assert "No matched skills" in result
    assert "cp-chip-matched" not in result


def test_render_missing_chips_html_below_threshold_shows_all_inline():
    skills = [f"Skill{i}" for i in range(MISSING_SKILLS_VISIBLE_COUNT)]
    result = render_missing_chips_html(skills)
    assert "details" not in result
    for skill in skills:
        assert skill in result


def test_render_missing_chips_html_collapses_the_overflow():
    skills = [f"Skill{i}" for i in range(MISSING_SKILLS_VISIBLE_COUNT + 5)]
    result = render_missing_chips_html(skills)
    assert "<details" in result
    assert "+5 more" in result
    # visible ones appear before the <details> block; overflow ones appear inside it
    details_start = result.index("<details")
    for skill in skills[:MISSING_SKILLS_VISIBLE_COUNT]:
        assert result.index(skill) < details_start
    for skill in skills[MISSING_SKILLS_VISIBLE_COUNT:]:
        assert result.index(skill) > details_start


def test_render_missing_chips_html_empty_list_shows_explicit_message():
    result = render_missing_chips_html([])
    assert "Nothing stated as missing" in result


def test_render_job_card_html_includes_score_and_verdict_class():
    dj = _make_dashboard_job(fit_score=85, verdict="strong")
    result = render_job_card_html(dj)
    assert "cp-verdict-strong" in result
    assert ">85<" in result


def test_render_job_card_html_escapes_title_and_company():
    dj = _make_dashboard_job(job=_make_posting(title="C++ <Engineer>", company="Acme & Co"))
    result = render_job_card_html(dj)
    assert "&lt;Engineer&gt;" in result
    assert "Acme &amp; Co" in result
    assert "<Engineer>" not in result


def test_render_job_card_html_shows_new_badge_only_when_new():
    dj_new = _make_dashboard_job(is_new=True)
    dj_old = _make_dashboard_job(is_new=False)
    assert "cp-new-badge" in render_job_card_html(dj_new)
    assert "cp-new-badge" not in render_job_card_html(dj_old)


def test_render_job_card_html_experience_not_stated_when_years_required_is_none():
    dj = _make_dashboard_job(years_required=None)
    assert "experience not stated" in render_job_card_html(dj)


def test_render_unscored_card_html_shows_question_mark_not_a_number():
    dj = _make_dashboard_job(is_unscored=True, matched_skills=[], missing_skills=[], verdict="unscored")
    result = render_unscored_card_html(dj)
    assert ">?<" in result
    assert "cp-verdict-unscored" in result
    assert "No technical requirements were extracted" in result


def test_render_stats_strip_html_handles_zero_fetched_without_dividing_by_zero():
    result = render_stats_strip_html(total_fetched=0, survived=0, analyzed=0, unanalyzed=0)
    assert "0" in result  # renders, doesn't raise ZeroDivisionError


def test_render_stats_strip_html_includes_all_four_values():
    result = render_stats_strip_html(total_fetched=7564, survived=37, analyzed=37, unanalyzed=0)
    assert "7,564" in result
    assert "37" in result


def test_render_empty_state_html_escapes_and_includes_both_parts():
    result = render_empty_state_html("Title <here>", "Body & more")
    assert "Title &lt;here&gt;" in result
    assert "Body &amp; more" in result
