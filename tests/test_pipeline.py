from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

import pipeline
from adapters.base import BoardNotFoundError
from db import get_engine, get_last_fetched_at, record_fetch
from models import ATSSource, Cadence, CompanyConfig, JobPosting


def _make_posting(**overrides):
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        location="Bangalore, Karnataka",
        description="Requirements: Python.",
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


class _RecordingCallback:
    """A plain recorder, not a mock - keeps assertions readable (list of
    real ProgressEvent objects) without pulling in a mocking library this
    project doesn't otherwise use."""

    def __init__(self):
        self.events: list[pipeline.ProgressEvent] = []

    def __call__(self, event: pipeline.ProgressEvent) -> None:
        self.events.append(event)


class _RaisingCallback:
    def __call__(self, event: pipeline.ProgressEvent) -> None:
        raise RuntimeError("simulated UI bug")


# ---------------------------------------------------------------------------
# _emit - the one call site every progress-reporting function routes through
# ---------------------------------------------------------------------------


def test_emit_calls_callback_with_event():
    recorder = _RecordingCallback()
    event = pipeline.ProgressEvent(stage="fetch", message="hello")

    pipeline._emit(recorder, event)

    assert recorder.events == [event]


def test_emit_noop_when_callback_is_none():
    pipeline._emit(None, pipeline.ProgressEvent(stage="fetch", message="hello"))  # must not raise


def test_emit_swallows_callback_exception():
    """A UI callback bug must never take down a real run - see _emit's
    docstring. This is the property the whole design leans on: on_progress
    is allowed to be sloppy without risking the pipeline it's observing."""
    pipeline._emit(_RaisingCallback(), pipeline.ProgressEvent(stage="fetch", message="hello"))  # must not raise


# ---------------------------------------------------------------------------
# fetch_all - progress events, and that on_progress=None changes nothing
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_fetchers(monkeypatch):
    """Replaces pipeline.FETCHERS with fakes keyed by ATSSource -> a
    function returning a fixed job list or raising - same fixture-shape
    convention as agents/scout.py's tests' patched_adapters.

    Takes the whole CompanyConfig, not (name, token) - matching FETCHERS'
    real signature since Workday needs three fields, not one token (see
    pipeline.py's FETCHERS)."""
    calls: list[str] = []

    def make_fetch(jobs_by_company: dict):
        def fetch(company: CompanyConfig) -> list[JobPosting]:
            calls.append(company.name)
            if company.name not in jobs_by_company:
                raise BoardNotFoundError(source="greenhouse", company=company.name, board_token=company.token or "")
            return jobs_by_company[company.name]

        return fetch

    return calls, make_fetch


def test_fetch_all_emits_a_start_event_with_total(monkeypatch, fake_fetchers):
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Acme": []})})
    companies = [CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme")]
    recorder = _RecordingCallback()

    pipeline.fetch_all(companies, on_progress=recorder)

    start_events = [e for e in recorder.events if e.total == 1 and e.current is None]
    assert len(start_events) == 1
    assert start_events[0].stage == "fetch"
    assert "1 companies" in start_events[0].message


def test_fetch_all_emits_one_progress_event_per_company_with_live_count(monkeypatch, fake_fetchers):
    calls, make_fetch = fake_fetchers
    jobs_by_company = {"Acme": [_make_posting(company="Acme")], "Globex": [_make_posting(company="Globex", source_job_id="2")]}
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch(jobs_by_company)})
    companies = [
        CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme"),
        CompanyConfig(name="Globex", ats=ATSSource.GREENHOUSE, token="globex"),
    ]
    recorder = _RecordingCallback()

    pipeline.fetch_all(companies, on_progress=recorder)

    per_company = [e for e in recorder.events if e.current is not None]
    assert [e.current for e in per_company] == [1, 2]
    assert [e.total for e in per_company] == [2, 2]
    assert per_company[0].extra["company"] == "Acme"
    assert per_company[0].extra["jobs_found"] == 1


def test_fetch_all_emits_progress_even_for_a_failed_company(monkeypatch, fake_fetchers):
    """A BoardNotFoundError company still counts toward the live total -
    the UI's count must reach the real company count even when some fail,
    not silently fall short."""
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({})})  # every company fails
    companies = [CompanyConfig(name="Ghost Co", ats=ATSSource.GREENHOUSE, token="ghost")]
    recorder = _RecordingCallback()

    jobs, failures, skipped = pipeline.fetch_all(companies, on_progress=recorder)

    assert len(failures) == 1
    assert skipped == []
    per_company = [e for e in recorder.events if e.current is not None]
    assert len(per_company) == 1
    assert per_company[0].extra["jobs_found"] == 0


def test_fetch_all_without_on_progress_behaves_exactly_as_before(monkeypatch, fake_fetchers):
    """on_progress defaults to None - every existing CLI call site must be
    completely unaffected by this parameter's existence."""
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Acme": [_make_posting()]})})
    companies = [CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme")]

    jobs, failures, skipped = pipeline.fetch_all(companies)  # no on_progress, no session passed at all

    assert len(jobs) == 1
    assert failures == []
    assert skipped == []


# ---------------------------------------------------------------------------
# fetch_all - Cadence.WEEKLY: skip when not due, fetch when due, --force,
# and no-session-means-cadence-disabled (same convention as on_progress=None
# on this same function)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    """A real in-memory DB - cadence needs an actual company_fetch_state
    table to read/write, not a fake. Same get_engine(':memory:') convention
    tests/test_db.py already uses."""
    engine = get_engine(":memory:")
    with Session(engine) as session:
        yield session


def _weekly_company(name: str = "Cisco") -> CompanyConfig:
    return CompanyConfig(name=name, ats=ATSSource.GREENHOUSE, token="cisco", cadence=Cadence.WEEKLY)


def test_fetch_all_ignores_cadence_entirely_without_a_session(monkeypatch, fake_fetchers):
    """session=None disables Cadence.WEEKLY altogether, the same "None means
    old behavior" convention on_progress already has on this function - a
    weekly company with no session passed is fetched every call, same as a
    nightly one."""
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Cisco": [_make_posting(company="Cisco")]})})

    jobs, failures, skipped = pipeline.fetch_all([_weekly_company()])  # no session

    assert len(jobs) == 1
    assert skipped == []
    assert calls == ["Cisco"]


def test_fetch_all_fetches_a_weekly_company_with_no_recorded_fetch_yet(monkeypatch, fake_fetchers, db_session):
    """A brand-new weekly company (never successfully fetched) is due on
    its first run, same as any new companies.yaml entry - the skip only
    ever applies once there's a real last-fetched time to compare against."""
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Cisco": [_make_posting(company="Cisco")]})})

    jobs, failures, skipped = pipeline.fetch_all([_weekly_company()], session=db_session)

    assert len(jobs) == 1
    assert skipped == []
    assert calls == ["Cisco"]


def test_fetch_all_skips_a_weekly_company_fetched_recently(monkeypatch, fake_fetchers, db_session):
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Cisco": [_make_posting(company="Cisco")]})})
    record_fetch(db_session, "Cisco", when=datetime.now(timezone.utc) - timedelta(days=2))
    db_session.commit()

    jobs, failures, skipped = pipeline.fetch_all([_weekly_company()], session=db_session)

    assert jobs == []
    assert calls == []  # FETCHERS never called for a company that's skipped
    assert len(skipped) == 1
    assert skipped[0][0].name == "Cisco"
    assert "2 day" in skipped[0][1]


def test_fetch_all_fetches_a_weekly_company_past_the_cadence_window(monkeypatch, fake_fetchers, db_session):
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Cisco": [_make_posting(company="Cisco")]})})
    record_fetch(db_session, "Cisco", when=datetime.now(timezone.utc) - timedelta(days=8))
    db_session.commit()

    jobs, failures, skipped = pipeline.fetch_all([_weekly_company()], session=db_session)

    assert len(jobs) == 1
    assert skipped == []
    assert calls == ["Cisco"]


def test_fetch_all_force_fetches_a_weekly_company_not_due_and_still_records_it(monkeypatch, fake_fetchers, db_session):
    """force=True bypasses the skip, but the fetch is still recorded - a
    forced run resets the weekly clock rather than leaving it stuck on the
    old timestamp forever."""
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Cisco": [_make_posting(company="Cisco")]})})
    record_fetch(db_session, "Cisco", when=datetime.now(timezone.utc) - timedelta(hours=1))
    db_session.commit()

    jobs, failures, skipped = pipeline.fetch_all([_weekly_company()], session=db_session, force=True)

    assert len(jobs) == 1
    assert skipped == []
    last_fetched = get_last_fetched_at(db_session, "Cisco")
    assert (datetime.now(timezone.utc).replace(tzinfo=None) - last_fetched) < timedelta(minutes=1)


def test_fetch_all_records_a_successful_fetch_for_the_next_call_to_check(monkeypatch, fake_fetchers, db_session):
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Cisco": [_make_posting(company="Cisco")]})})
    assert get_last_fetched_at(db_session, "Cisco") is None

    pipeline.fetch_all([_weekly_company()], session=db_session)

    assert get_last_fetched_at(db_session, "Cisco") is not None


def test_fetch_all_does_not_record_a_failed_fetch(monkeypatch, fake_fetchers, db_session):
    """A company whose fetch raised BoardNotFoundError should be retried
    next run, not treated as freshly fetched and skipped - see
    db.record_fetch's docstring."""
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({})})  # every company fails

    jobs, failures, skipped = pipeline.fetch_all([_weekly_company()], session=db_session)

    assert len(failures) == 1
    assert get_last_fetched_at(db_session, "Cisco") is None


def test_fetch_all_skip_event_extra_marks_skipped_distinctly(monkeypatch, fake_fetchers, db_session):
    """A skip is neither a success nor a failure - its ProgressEvent.extra
    shouldn't carry jobs_found/error keys that would make a UI mistake it
    for either."""
    calls, make_fetch = fake_fetchers
    monkeypatch.setattr(pipeline, "FETCHERS", {ATSSource.GREENHOUSE: make_fetch({"Cisco": [_make_posting(company="Cisco")]})})
    record_fetch(db_session, "Cisco", when=datetime.now(timezone.utc) - timedelta(days=1))
    db_session.commit()
    recorder = _RecordingCallback()

    pipeline.fetch_all([_weekly_company()], on_progress=recorder, session=db_session)

    per_company = [e for e in recorder.events if e.current is not None]
    assert len(per_company) == 1
    assert per_company[0].extra["skipped"] is True
    assert "jobs_found" not in per_company[0].extra
    assert "error" not in per_company[0].extra


# ---------------------------------------------------------------------------
# _run_analyst_over_jobs - per-job progress events, tagged with the caller's
# explicit `stage`
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, model_name: str = "fake-model"):
        self._model_name = model_name
        self.call_count = 0
        self.last_usage = None

    @property
    def model_name(self) -> str:
        return self._model_name


def _fake_analyze_factory(fit_scores: list[int]):
    """Returns a fake agents.analyst.analyze() that yields (result,
    from_cache=False) for each job in order, using AnalystResult directly
    (a real Pydantic model) rather than a further fake, so ProgressEvent's
    fit_score extra is checked against real validated data."""
    from agents.analyst import AnalystResult

    scores = iter(fit_scores)

    def fake_analyze(job, resume_text, client, session):
        client.call_count += 1
        score = next(scores)
        result = AnalystResult(
            fit_score=score,
            matched_skills=["Python"],
            missing_skills=[],
            experience_gap={"years_required": None, "resume_meets_it": True},
            reasoning="Looks fine.",
        )
        return result, False

    return fake_analyze


def test_run_analyst_over_jobs_emits_one_event_per_job_tagged_with_stage(monkeypatch):
    jobs = [_make_posting(source_job_id="1"), _make_posting(source_job_id="2")]
    monkeypatch.setattr(pipeline, "analyze", _fake_analyze_factory([70, 85]))
    recorder = _RecordingCallback()

    pipeline._run_analyst_over_jobs(jobs, _FakeClient(), "resume text", session=None, stage="stage2", on_progress=recorder)

    assert [e.current for e in recorder.events] == [1, 2]
    assert [e.total for e in recorder.events] == [2, 2]
    assert all(e.stage == "stage2" for e in recorder.events)
    assert [e.extra["fit_score"] for e in recorder.events] == [70, 85]


def test_run_analyst_over_jobs_stops_emitting_after_a_failure(monkeypatch):
    """A failure on job 2 of 3 must not emit a progress event for the job
    that never completed - the same "stop the batch, don't lose what
    already succeeded" contract _run_analyst_over_jobs already has for its
    print()/results list applies equally to progress events."""
    jobs = [_make_posting(source_job_id=str(i)) for i in range(1, 4)]

    def flaky_analyze(job, resume_text, client, session):
        if job.source_job_id == "2":
            raise RuntimeError("simulated failure")
        client.call_count += 1
        from agents.analyst import AnalystResult

        return (
            AnalystResult(
                fit_score=50,
                matched_skills=[],
                missing_skills=[],
                experience_gap={"years_required": None, "resume_meets_it": True},
                reasoning="ok",
            ),
            False,
        )

    monkeypatch.setattr(pipeline, "analyze", flaky_analyze)
    recorder = _RecordingCallback()

    pipeline._run_analyst_over_jobs(jobs, _FakeClient(), "resume text", session=None, on_progress=recorder)

    assert [e.current for e in recorder.events] == [1]  # only job 1 completed before job 2 failed


def test_run_analyst_over_jobs_without_on_progress_behaves_exactly_as_before(monkeypatch):
    jobs = [_make_posting()]
    monkeypatch.setattr(pipeline, "analyze", _fake_analyze_factory([90]))

    results, *_rest = pipeline._run_analyst_over_jobs(jobs, _FakeClient(), "resume text", session=None)  # no on_progress

    assert len(results) == 1
