from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import (
    _ADDED_COLUMNS,
    JobPostingRow,
    RunMetricsRow,
    database_url,
    get_engine,
    get_last_fetched_at,
    is_postgres,
    last_successful_run,
    record_fetch,
    upsert_job,
    upsert_jobs,
)
from models import ATSSource, JobPosting


def _make_posting(**overrides):
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="123",
        company="Acme Corp",
        title="Software Engineer I",
        location="Hyderabad, India",
        description="We are looking for a software engineer to join our team.",
        url="https://boards.greenhouse.io/acme/jobs/123",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    with Session(engine) as session:
        yield session


def test_new_job_inserted_with_defaults(session):
    job = _make_posting()
    before = datetime.now(timezone.utc)

    outcome = upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    assert outcome == "new"
    row = session.get(JobPostingRow, job.content_hash)
    assert row.application_status == "new"
    assert row.edited is False
    assert row.filter_passed is True
    assert row.rejection_rule is None
    assert row.experience_years_required is None
    assert row.first_seen == row.last_seen
    assert row.first_seen.replace(tzinfo=timezone.utc) >= before.replace(microsecond=0)


def test_resighting_unchanged_job_updates_last_seen_only(session):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    first_seen_before = row.first_seen

    outcome = upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert outcome == "unchanged"
    assert row.edited is False
    assert row.first_seen == first_seen_before
    assert row.last_seen >= first_seen_before


def test_description_change_flags_edited_and_updates_text(session):
    job = _make_posting(description="Original JD text.")
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    changed = _make_posting(description="Updated JD text with new responsibilities.")
    outcome = upsert_job(session, changed, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert outcome == "edited"
    assert row.edited is True
    assert row.description == "Updated JD text with new responsibilities."
    assert row.description_hash == changed.description_hash


def test_application_status_never_clobbered_by_resighting(session):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    row.application_status = "applied"
    session.commit()

    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert row.application_status == "applied"


def test_application_status_survives_a_description_edit_too():
    """Even the "edited" path - not just a plain re-sighting - must not touch
    application_status. It's a separate concern from content freshness."""
    engine = get_engine(":memory:")
    with Session(engine) as session:
        job = _make_posting(description="Original JD text.")
        upsert_job(session, job, rejection_rule=None, experience_years_required=None)
        session.commit()

        row = session.get(JobPostingRow, job.content_hash)
        row.application_status = "applied"
        session.commit()

        changed = _make_posting(description="Updated JD text with new responsibilities.")
        upsert_job(session, changed, rejection_rule=None, experience_years_required=None)
        session.commit()

        row = session.get(JobPostingRow, job.content_hash)
        assert row.application_status == "applied"


def test_rejected_job_stored_with_rule(session):
    job = _make_posting(title="Senior Software Engineer")
    upsert_job(session, job, rejection_rule="seniority", experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert row.filter_passed is False
    assert row.rejection_rule == "seniority"


def test_rejection_rule_updates_if_rerun_after_a_keyword_tune(session):
    """A job rejected under an old keyword list should reflect the current
    rule outcome after a re-run, not stay stuck on a stale verdict."""
    job = _make_posting(title="Senior Software Engineer")
    upsert_job(session, job, rejection_rule="seniority", experience_years_required=None)
    session.commit()

    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert row.filter_passed is True
    assert row.rejection_rule is None


def test_experience_years_required_stored_and_is_advisory_not_a_rejection(session):
    """experience_years_required is stored on the row regardless of value -
    it's advisory only (see docs/decisions.md) and must never make
    filter_passed False on its own."""
    job = _make_posting(title="Software Engineer", description="Minimum 7 years of experience required.")
    upsert_job(session, job, rejection_rule=None, experience_years_required=7.0)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert row.experience_years_required == 7.0
    assert row.filter_passed is True  # a high figure alone must not reject


def test_experience_years_required_none_means_not_stated_not_zero(session):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert row.experience_years_required is None


def test_experience_years_required_updates_on_resighting(session):
    """If a re-fetch's description text yields a different parsed figure
    (or extraction logic changes what's found), the stored figure should
    track the current parse, not stay stuck on the first-seen value."""
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=3.0)
    session.commit()

    upsert_job(session, job, rejection_rule=None, experience_years_required=5.0)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert row.experience_years_required == 5.0


def test_batch_upsert_tallies_outcomes_by_type(session):
    unchanged_job = _make_posting(source_job_id="1", title="Backend Developer")
    upsert_job(session, unchanged_job, rejection_rule=None, experience_years_required=None)
    session.commit()

    edited_job = _make_posting(source_job_id="2", title="Frontend Developer", description="Original text.")
    upsert_job(session, edited_job, rejection_rule=None, experience_years_required=None)
    session.commit()

    batch = [
        (unchanged_job, None, None),  # same as before -> unchanged
        (_make_posting(source_job_id="2", title="Frontend Developer", description="Changed text."), None, None),  # edited
        (_make_posting(source_job_id="3", title="QA Engineer"), None, None),  # brand new
    ]

    outcomes = upsert_jobs(session, batch)

    assert outcomes == {"new": 1, "unchanged": 1, "edited": 1}


def test_different_content_hash_creates_separate_rows(session):
    upsert_job(session, _make_posting(title="Software Engineer I"), rejection_rule=None, experience_years_required=None)
    upsert_job(session, _make_posting(title="Software Engineer II"), rejection_rule=None, experience_years_required=None)
    session.commit()

    assert session.query(JobPostingRow).count() == 2


# ---------------------------------------------------------------------------
# applied_at - a real ALTER TABLE migration, not just a new column on a
# fresh schema (see get_engine's _ensure_added_columns)
# ---------------------------------------------------------------------------


def test_new_job_has_applied_at_null_by_default(session):
    job = _make_posting()
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    assert row.applied_at is None


def _create_pre_migration_db(path):
    """Builds a SQLite file with the job_postings schema exactly as it
    existed before applied_at was added - raw SQL, deliberately not going
    through get_engine()/Base.metadata, so this is a genuine "old database"
    fixture and not just a relabeled fresh one. Mirrors the real
    data/careerpilot.db's actual pre-migration column set."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE job_postings (
            content_hash TEXT PRIMARY KEY,
            description_hash TEXT,
            source TEXT,
            source_job_id TEXT,
            company TEXT,
            title TEXT,
            location TEXT,
            remote_type TEXT,
            description TEXT,
            url TEXT,
            posted_at DATETIME,
            first_seen DATETIME,
            last_seen DATETIME,
            edited BOOLEAN,
            filter_passed BOOLEAN,
            rejection_rule TEXT,
            experience_years_required FLOAT,
            application_status TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO job_postings (content_hash, company, title, application_status) VALUES (?, ?, ?, ?)",
        ("existing-hash-1", "Acme Corp", "Software Engineer I", "applied"),
    )
    conn.commit()
    conn.close()


def test_get_engine_migrates_a_pre_existing_db_missing_applied_at(tmp_path):
    db_path = tmp_path / "pre_migration.db"
    _create_pre_migration_db(db_path)

    engine = get_engine(db_path)

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(job_postings)").fetchall()}
    conn.close()
    assert "applied_at" in columns

    with Session(engine) as session:
        row = session.get(JobPostingRow, "existing-hash-1")
        assert row is not None  # pre-existing row survived the migration
        assert row.company == "Acme Corp"
        assert row.application_status == "applied"
        assert row.applied_at is None  # never fabricated - see JobPostingRow.applied_at's docstring


def test_get_engine_adds_total_tokens_to_a_pre_existing_metrics_table(tmp_path):
    """Regression, found in production rather than in a test: the real
    database had run_agent_metrics created by an earlier run, before
    total_tokens was added to the model. create_all saw the table already
    present and left it alone, so GET /api/meta/runtime failed with
    "no such column: run_agent_metrics.total_tokens".

    Builds the table in its pre-total_tokens shape deliberately - a table
    created by create_all would already have the column and prove
    nothing."""
    import sqlite3

    db_path = tmp_path / "old_metrics.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE run_agent_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            stage TEXT,
            agent TEXT,
            model TEXT,
            companies_checked INTEGER,
            jobs_retrieved INTEGER,
            jobs_processed INTEGER,
            llm_calls INTEGER,
            cache_hits INTEGER,
            cache_misses INTEGER,
            retries INTEGER,
            started_at DATETIME,
            finished_at DATETIME,
            duration_seconds FLOAT
        )
        """
    )
    conn.execute("INSERT INTO run_agent_metrics (run_id, stage, llm_calls) VALUES ('r1', 'stage1', 4)")
    conn.commit()
    conn.close()

    get_engine(db_path)

    conn = sqlite3.connect(str(db_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(run_agent_metrics)").fetchall()}
    preserved = conn.execute("SELECT run_id, llm_calls FROM run_agent_metrics").fetchall()
    conn.close()

    assert "total_tokens" in columns
    assert preserved == [("r1", 4)]  # existing rows survived the migration


def test_get_engine_migration_is_idempotent(tmp_path):
    """A second get_engine() call on an already-migrated file must not
    raise (e.g. from trying to ADD COLUMN a column that already exists)."""
    db_path = tmp_path / "pre_migration.db"
    _create_pre_migration_db(db_path)

    get_engine(db_path)
    get_engine(db_path)  # must not raise


# ---------------------------------------------------------------------------
# CompanyFetchStateRow / get_last_fetched_at / record_fetch - the state
# pipeline.fetch_all's Cadence.WEEKLY skip logic reads and writes. A brand
# new table, not a migrated column - create_all() builds it with no entry
# needed in _ADDED_COLUMNS.
# ---------------------------------------------------------------------------


def test_get_last_fetched_at_is_none_for_a_company_never_fetched(session):
    assert get_last_fetched_at(session, "Cisco") is None


def test_record_fetch_then_read_round_trips(session):
    before = datetime.now(timezone.utc)

    record_fetch(session, "Cisco")
    session.commit()

    last_fetched = get_last_fetched_at(session, "Cisco")
    assert last_fetched is not None
    assert last_fetched.tzinfo is None  # stored naive UTC, same convention as every other datetime column
    assert abs((last_fetched - before.replace(tzinfo=None)).total_seconds()) < 5


def test_record_fetch_accepts_an_explicit_when():
    """The weekly-skip tests in test_pipeline.py need to plant a fetch N
    days in the past - record_fetch must not always mean "now"."""
    engine = get_engine(":memory:")
    with Session(engine) as session:
        eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)

        record_fetch(session, "Cisco", when=eight_days_ago)
        session.commit()

        last_fetched = get_last_fetched_at(session, "Cisco")
        assert abs((last_fetched - eight_days_ago.replace(tzinfo=None)).total_seconds()) < 1


def test_record_fetch_updates_rather_than_duplicates(session):
    """A second fetch for the same company overwrites the one row - upsert,
    not insert, same shape as upsert_job for JobPostingRow."""
    record_fetch(session, "Cisco", when=datetime.now(timezone.utc) - timedelta(days=10))
    session.commit()
    first_read = get_last_fetched_at(session, "Cisco")

    record_fetch(session, "Cisco")  # now
    session.commit()
    second_read = get_last_fetched_at(session, "Cisco")

    assert second_read > first_read
    rows = session.execute(text("SELECT COUNT(*) FROM company_fetch_state WHERE company = 'Cisco'")).scalar()
    assert rows == 1


def test_record_fetch_is_independent_per_company(session):
    record_fetch(session, "Cisco", when=datetime.now(timezone.utc) - timedelta(days=1))
    record_fetch(session, "Adobe", when=datetime.now(timezone.utc) - timedelta(days=9))
    session.commit()

    assert get_last_fetched_at(session, "Cisco") > get_last_fetched_at(session, "Adobe")


# ---------------------------------------------------------------------------
# Dual-engine support: DATABASE_URL selects Postgres, absence selects
# SQLite. No test here connects to a real Postgres - these cover the URL
# normalization and the engine-selection branch, which is where the
# mistakes actually live. See docs/decisions.md.
# ---------------------------------------------------------------------------


def test_database_url_is_none_without_the_env_var(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url() is None
    assert is_postgres() is False


def test_database_url_normalizes_to_the_psycopg3_driver(monkeypatch):
    """A bare postgresql:// URL makes SQLAlchemy reach for psycopg2, which
    isn't installed - a ModuleNotFoundError is a confusing way to discover
    that. Neon hands out bare URLs, so this is the common path."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host.neon.tech/neondb")
    assert database_url() == "postgresql+psycopg://u:p@host.neon.tech/neondb"


def test_database_url_upgrades_the_legacy_postgres_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    assert database_url() == "postgresql+psycopg://u:p@host/db"


def test_database_url_treats_whitespace_only_as_unset(monkeypatch):
    """An empty or blank DATABASE_URL in a .env file must mean "local
    SQLite", not "connect to the empty string"."""
    monkeypatch.setenv("DATABASE_URL", "   ")
    assert database_url() is None


def test_get_engine_uses_sqlite_for_an_explicit_path_even_when_database_url_is_set(monkeypatch, tmp_path):
    """The safety property the whole test suite depends on: a developer
    with a real Neon URL in their .env must be able to run pytest without
    any test touching the deployed database."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host.neon.tech/neondb")

    engine = get_engine(tmp_path / "explicit.db")

    assert engine.dialect.name == "sqlite"


def test_get_engine_uses_sqlite_for_memory_even_when_database_url_is_set(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host.neon.tech/neondb")

    engine = get_engine(":memory:")

    assert engine.dialect.name == "sqlite"


def test_added_columns_carry_a_type_for_every_supported_dialect():
    """DATETIME does not exist in Postgres. A missing dialect entry would
    make _ensure_added_columns raise rather than silently skip a column,
    but catching it here is cheaper than catching it at deploy time."""
    for table, column, types_by_dialect in _ADDED_COLUMNS:
        assert "sqlite" in types_by_dialect, f"{table}.{column} missing a sqlite type"
        assert "postgresql" in types_by_dialect, f"{table}.{column} missing a postgresql type"
        assert "DATETIME" not in types_by_dialect["postgresql"].upper(), (
            f"{table}.{column} uses DATETIME for postgres, which is not a Postgres type"
        )


# ---------------------------------------------------------------------------
# last_successful_run - the freshness signal the frontend reads instead of
# a local file mtime (there is no shared filesystem once the writer is an
# ephemeral CI runner).
# ---------------------------------------------------------------------------


def _add_run(session, run_id, status, started, finished):
    session.add(
        RunMetricsRow(
            run_id=run_id, started_at=started, finished_at=finished, status=status, trigger="cli"
        )
    )
    session.commit()


def test_last_successful_run_is_none_when_nothing_has_run(session):
    assert last_successful_run(session) is None


def test_last_successful_run_returns_the_latest_completed_finish_time(session):
    _add_run(session, "r1", "completed", datetime(2026, 8, 17, 2, 0), datetime(2026, 8, 17, 2, 20))
    _add_run(session, "r2", "completed", datetime(2026, 8, 19, 2, 0), datetime(2026, 8, 19, 2, 25))

    assert last_successful_run(session) == datetime(2026, 8, 19, 2, 25)


def test_last_successful_run_ignores_running_and_failed_runs(session):
    """Only a run that actually completed says anything about data
    freshness. A crashed run's start time would overstate it."""
    _add_run(session, "ok", "completed", datetime(2026, 8, 17, 2, 0), datetime(2026, 8, 17, 2, 20))
    _add_run(session, "crashed", "running", datetime(2026, 8, 19, 2, 0), None)
    _add_run(session, "failed", "failed", datetime(2026, 8, 20, 2, 0), datetime(2026, 8, 20, 2, 5))

    assert last_successful_run(session) == datetime(2026, 8, 17, 2, 20)
