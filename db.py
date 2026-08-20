"""Storage layer. Runs on SQLite (local development, tests) or Postgres
(deployed - Neon), decided by the DATABASE_URL environment variable.

The two engines are not interchangeable in ways that matter here, and each
difference below was verified against the real database before being coded
around rather than assumed - see docs/decisions.md:

  - VARCHAR(n) is advisory in SQLite and ENFORCED in Postgres. `location`
    is Text, not String(300), because real multi-location postings
    semicolon-join arbitrarily many locations (three rows in the real
    archive exceed 300 characters; the longest is 649).
  - SQLite's WAL pragma has no Postgres equivalent and needs none -
    Postgres's MVCC already lets readers and writers proceed concurrently,
    which is the entire problem WAL was enabled to solve.
  - `ALTER TABLE ... ADD COLUMN` needs a per-dialect type name (DATETIME
    does not exist in Postgres) and gets IF NOT EXISTS where supported.
  - Row order without ORDER BY is stable-ish in SQLite (rowid order) and
    explicitly undefined in Postgres. Callers that slice or sample results
    must order explicitly; see pipeline.print_analyst_stage1 and
    export_labels.sample_rejected.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    NullPool,
    String,
    Text,
    create_engine,
    func,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from models import ATSSource, JobPosting, RemoteType

# llm.py also calls this, but db.py is imported by callers (the migration
# script, evaluate.py) that never touch llm.py - so DATABASE_URL has to be
# loaded here too rather than relying on that import side effect.
load_dotenv()

DEFAULT_DB_PATH = Path("data/careerpilot.db")


def database_url() -> Optional[str]:
    """The configured Postgres URL, or None for local SQLite.

    Normalized to the psycopg (v3) driver: a bare "postgresql://" URL makes
    SQLAlchemy reach for psycopg2, which isn't installed - the resulting
    ModuleNotFoundError is a confusing way to learn that. Neon hands out
    bare "postgresql://" strings, so this normalization is the common path,
    not an edge case."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return None
    if url.startswith("postgres://"):  # some providers still emit the legacy scheme
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def is_postgres() -> bool:
    return database_url() is not None


class Base(DeclarativeBase):
    pass


class JobPostingRow(Base):
    __tablename__ = "job_postings"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    description_hash: Mapped[str] = mapped_column(String(64))

    source: Mapped[str] = mapped_column(String(20))
    source_job_id: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    # Text, not String(300). SQLite never enforced the 300 limit so this
    # went unnoticed locally, but Postgres rejects an over-length value
    # outright - and real multi-location postings blow straight past it:
    # three rows in the archive semicolon-join a full US-state list, the
    # longest at 649 characters. A bigger VARCHAR would only move the
    # cliff; the data genuinely has no natural upper bound. See
    # docs/decisions.md.
    location: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    remote_type: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(500))
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)

    filter_passed: Mapped[bool] = mapped_column(Boolean)
    rejection_rule: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Parsed by filters.parse_max_experience_years, advisory only - not a
    # filter criterion (see docs/decisions.md). NULL means no requirement
    # was found in the description, not zero - callers must not conflate
    # "not stated" with "0 years".
    experience_years_required: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    application_status: Mapped[str] = mapped_column(String(20), default="new")
    # Set once, the first time application_status becomes "applied" - see
    # app.py's set_application_status. Never overwritten by a later
    # re-click, never cleared if the status later changes away from
    # "applied" (a job you applied to and then marked rejected is still a
    # job you applied to - the Applied tab lists by applied_at IS NOT
    # NULL, not by current status, so that history isn't lost). NULL means
    # never applied, not "applied on an unknown date" - the two must never
    # be conflated, same principle as experience_years_required above.
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class JobEmbeddingRow(Base):
    """Cached embedding, keyed on a hash of the exact text that was embedded
    - not on description_hash or content_hash. Ranking embeds extracted
    text (see extraction.py), not the raw description, and that extraction
    changes whenever extraction.py's logic or config's header lists get
    tuned - which is expected to happen often. Keying on description_hash
    previously meant an extraction change could silently leave stale
    embeddings served under an unchanged key; keying on the actual embedded
    text instead means any such change naturally invalidates just the
    affected entries. See docs/decisions.md. Raw vector bytes only;
    ranking.py owns the numpy (de)serialization, keeping that dependency out
    of this module."""

    __tablename__ = "job_embeddings"

    text_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)


class AnalystResultRow(Base):
    """Cached Analyst verdict, keyed on a hash of everything that determines
    the output: the model name, the system instruction, and the exact
    (resume text, requirements text) pair sent - not content_hash or
    description_hash, and not text alone. Two models analyzing the same job
    (the two-stage design: a cheap model screens everyone, a stronger model
    re-checks the top candidates) must not collide under one cache entry,
    and a resume edit, a prompt tune, or an extraction.py change must each
    invalidate just the affected entries automatically. Same fix as
    JobEmbeddingRow.text_hash; see docs/decisions.md.

    model is also a plain column, not just folded into the hash - so results
    from the two stages can be queried and compared directly (e.g. "every
    stage-2 verdict for a job") without needing to recompute hashes.

    verdict is derived from fit_score in agents/analyst.py, not asked of the
    LLM - stored here anyway since it's cheap and the point is to make it a
    sortable/filterable field, not recompute it every read."""

    __tablename__ = "analyst_results"

    text_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(100))
    fit_score: Mapped[int] = mapped_column(Integer)
    matched_skills: Mapped[str] = mapped_column(Text)  # JSON-encoded list[str]
    missing_skills: Mapped[str] = mapped_column(Text)  # JSON-encoded list[str]
    experience_years_required: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    resume_meets_experience: Mapped[bool] = mapped_column(Boolean)
    verdict: Mapped[str] = mapped_column(String(20))
    reasoning: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class RunMetricsRow(Base):
    """One row per orchestrator run - the run-level envelope. Written by
    api/services/metrics.py's MetricsCollector, which is itself just an
    on_progress callback (see pipeline.ProgressEvent), so nothing in
    orchestrator.py or pipeline.py knows this table exists.

    Split from RunAgentMetricsRow rather than kept as one wide table: a run
    has one start/finish/status, and repeating those on every per-agent row
    would mean three copies of the same truth that can disagree. `status`
    is "running" until the run ends, then "completed" or "failed" - a row
    left at "running" with no finished_at is a crashed or killed run, which
    is real information worth being able to see rather than something to
    clean up silently."""

    __tablename__ = "run_metrics"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # running | completed | failed
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), default="api")  # api | cli


class RunAgentMetricsRow(Base):
    """One row per (run, stage) - the per-agent/per-stage detail behind
    GET /api/meta/agents.

    `agent` is NULL for fetch and filter: those are pipeline stages, not
    agents (see pipeline.ProgressEvent.agent for why calling them Scout
    would be untrue). Only stage1/stage2 carry "Analyst".

    `retries` is deliberately nullable and left NULL, not 0. LangGraph's
    RetryPolicy retries a node silently - nothing observable is emitted
    when it happens - so a 0 here would be a confident claim that no retry
    occurred when the truth is that retries aren't measured. NULL says "not
    measured", which is the honest answer, and the API surfaces it as null
    rather than coercing it to a number. If retries ever become observable,
    this column is already the place to put them.

    Token counts come from the same per-job progress events the UI reads,
    so they cover exactly the calls this run actually made."""

    __tablename__ = "run_agent_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(20))  # fetch | filter | stage1 | stage2
    agent: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # NULL for non-agent stages
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    companies_checked: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    jobs_retrieved: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    jobs_processed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
    cache_misses: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)  # fresh calls only - cache hits spend none
    retries: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # NULL = not measured, never a false 0

    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class CompanyFetchStateRow(Base):
    """Last successful fetch time per company, keyed on name - what
    pipeline.fetch_all's weekly-cadence skip reads to decide whether a
    `Cadence.WEEKLY` company (see models.py) is due yet. A brand-new table,
    not a column added to an existing one, so it needed no entry in
    _ADDED_COLUMNS below - Base.metadata.create_all() creates a missing
    TABLE on its own; only a missing COLUMN on a table it finds already
    present needs the ALTER TABLE machinery.

    Written only when a company's fetch actually succeeds (see
    pipeline.fetch_all) - a company whose last attempt failed should be
    retried next run, not treated as freshly fetched and skipped again."""

    __tablename__ = "company_fetch_state"

    company: Mapped[str] = mapped_column(String(200), primary_key=True)
    last_fetched_at: Mapped[datetime] = mapped_column(DateTime)


def get_last_fetched_at(session: Session, company: str) -> Optional[datetime]:
    """None means never successfully fetched - a nightly company that's
    always due, and a weekly company that's due on its very first run."""
    row = session.get(CompanyFetchStateRow, company)
    return row.last_fetched_at if row is not None else None


def get_last_fetched_map(session: Session, companies: Iterable[str]) -> dict[str, datetime]:
    """Every recorded last-fetch time for `companies`, in ONE query.

    Exists so pipeline.fetch_all can read the whole cadence picture in a
    single short transaction before it starts fetching, rather than
    querying per company inside the fetch loop. That loop runs for 40+
    minutes when the large Workday tenants are due, and a transaction left
    open across it is terminated by Neon's idle-in-transaction timeout -
    which is exactly how this was found, in a real GitHub Actions run. See
    docs/decisions.md.

    Companies with no recorded fetch are simply absent from the returned
    dict; callers treat a missing key as "never fetched, therefore due"."""
    names = list(companies)
    if not names:
        return {}
    rows = session.query(CompanyFetchStateRow).filter(CompanyFetchStateRow.company.in_(names)).all()
    return {row.company: row.last_fetched_at for row in rows}


def record_fetch(session: Session, company: str, when: Optional[datetime] = None) -> None:
    """Upsert - a plain select-then-write like upsert_job, not SQL-level
    ON CONFLICT, for the same reason: one local process, no concurrent-
    writer race to defend against. Does not commit; callers batching
    several companies in one fetch_all() run commit once at the end."""
    now = _to_naive_utc(when or datetime.now(timezone.utc))
    row = session.get(CompanyFetchStateRow, company)
    if row is None:
        session.add(CompanyFetchStateRow(company=company, last_fetched_at=now))
    else:
        row.last_fetched_at = now


def _enable_wal(engine) -> None:
    """SQLite's default rollback journal takes a database-wide exclusive
    lock for the duration of a write, so a reader that arrives mid-write
    gets `database is locked` rather than waiting politely. That was
    tolerable when every writer was a CLI run nobody was reading during -
    it stops being tolerable the moment the API serves GET /api/jobs while
    a background orchestrator run is writing analyst results, which is the
    normal case for Mission Control (watch the run progress while browsing
    jobs).

    WAL (write-ahead logging) lets readers and one writer proceed
    concurrently. It's a persistent property of the database file, not a
    per-connection setting, so this only actually does work the first time;
    it's re-issued on every get_engine() because that's cheaper than
    checking, and issuing it again on an already-WAL database is a no-op.

    SQLite only. Not applied to :memory: (WAL requires a real file, and
    in-memory databases have no cross-connection concurrency to protect
    anyway), and deliberately NOT translated to anything on Postgres:
    `PRAGMA` is a syntax error there, and the concurrency problem WAL
    solves does not exist under Postgres's MVCC. There is nothing to port,
    only something to skip."""
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))


# Columns added to a table AFTER that table already existed in a real
# database. Base.metadata.create_all() only creates missing TABLES, never
# missing COLUMNS on a table it finds already present - so a column added
# to a model later is simply absent from any pre-existing database file,
# and every query touching it fails with "no such column" until migrated.
#
# Both entries here were found the same way, in production rather than in
# a test: applied_at when the Applied tab first queried it, and
# total_tokens when GET /api/meta/runtime did - because in each case the
# table had been created by an earlier run, before the column existed.
# Appending to this list is the whole procedure for adding a nullable
# column from now on.
#
# (table, column, {dialect: SQL type}). The type is per-dialect because
# DATETIME simply does not exist in Postgres - `ALTER TABLE ... ADD COLUMN
# applied_at DATETIME` fails there with `type "datetime" does not exist`.
# SQLite natively supports `ALTER TABLE ... ADD COLUMN` for a single
# nullable column with no default - one of the few ALTER forms it handles
# without a full table rebuild - and Postgres supports the same plus
# IF NOT EXISTS. Anything needing a NOT NULL or a type change belongs in a
# real migration tool (Alembic), not here; this list deliberately only
# handles the nullable-add case it was built for. See docs/decisions.md.
_ADDED_COLUMNS: list[tuple[str, str, dict[str, str]]] = [
    ("job_postings", "applied_at", {"sqlite": "DATETIME", "postgresql": "TIMESTAMP"}),
    ("run_agent_metrics", "total_tokens", {"sqlite": "INTEGER DEFAULT 0", "postgresql": "INTEGER DEFAULT 0"}),
]


def _ensure_added_columns(engine) -> None:
    """Applies every entry in _ADDED_COLUMNS that's missing.

    Idempotent and cheap: one inspection per table, so this is safe to run
    unconditionally from get_engine() on every process start. A brand-new
    database (where create_all already built the full current schema) and
    an already-migrated one both skip every ALTER."""
    dialect = engine.dialect.name
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, column, types_by_dialect in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue  # create_all already built it with the current schema
        if column in {col["name"] for col in inspector.get_columns(table)}:
            continue
        sql_type = types_by_dialect.get(dialect)
        if sql_type is None:
            raise RuntimeError(
                f"no {dialect!r} column type recorded for {table}.{column} in _ADDED_COLUMNS - "
                "add one rather than letting the migration silently skip a column"
            )
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


# Postgres connection settings. Two very different callers:
#   - the FastAPI server: long-lived, concurrent, wants a real pool.
#   - a one-shot CLI/GitHub Actions run: pooling a connection the process
#     is about to drop is pure overhead, so NullPool is correct there.
# pool_pre_ping is non-negotiable on Neon either way: it sleeps idle
# connections, and a pooled-but-dead connection surfaces as an operational
# error on the next query rather than reconnecting on its own. pre_ping
# spends one cheap round trip to find out, and reconnects transparently.
POOL_SIZE = 5
MAX_OVERFLOW = 2
POOL_RECYCLE_SECONDS = 300


def _postgres_engine(url: str, *, pooled: bool):
    connect_args = {}
    # Neon's pooled endpoint (-pooler in the hostname) is PgBouncer in
    # transaction mode, where server-side prepared statements can outlive
    # the transaction that made them and collide. psycopg3 auto-prepares
    # after a few executions, so that's disabled here rather than left to
    # surface as an intermittent DuplicatePreparedStatement under load.
    if "-pooler." in url:
        connect_args["prepare_threshold"] = None

    if not pooled:
        return create_engine(url, poolclass=NullPool, pool_pre_ping=True, connect_args=connect_args)
    return create_engine(
        url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
        connect_args=connect_args,
    )


def get_engine(db_path: Union[str, Path] = DEFAULT_DB_PATH, *, pooled: bool = True):
    """Postgres when DATABASE_URL is set, SQLite otherwise.

    `db_path` is ignored for Postgres but kept in the signature so every
    existing caller - including tests that pass ":memory:" - works
    unchanged. A test passing an explicit path still gets SQLite even when
    DATABASE_URL is set in the developer's own .env, so running the suite
    never touches the deployed database."""
    url = database_url()
    if url is not None and str(db_path) == str(DEFAULT_DB_PATH):
        engine = _postgres_engine(url, pooled=pooled)
        Base.metadata.create_all(engine)
        _ensure_added_columns(engine)
        return engine  # no WAL - see _enable_wal

    is_memory = str(db_path) == ":memory:"
    if not is_memory:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    _ensure_added_columns(engine)
    if not is_memory:
        _enable_wal(engine)  # see _enable_wal - file-backed SQLite only
    return engine


def last_successful_run(session: Session) -> Optional[datetime]:
    """When the most recent successfully-completed run finished, or None if
    no run has ever completed.

    Read from run_metrics, not from a file mtime: the deployed API and the
    process that writes the data are different machines, and on GitHub
    Actions the writer's filesystem is destroyed the moment the run ends,
    so there is no file left to stat. None means "no completed run on
    record" - a genuinely different fact from "the data is old", and the
    API surfaces it as null rather than as a zero or an epoch date.

    Deliberately keyed on status == "completed": a row still at "running"
    is a crashed or in-flight run (see RunMetricsRow), and reporting its
    start time as data freshness would overstate how fresh the data is."""
    return session.query(func.max(RunMetricsRow.finished_at)).filter(RunMetricsRow.status == "completed").scalar()


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite has no native timezone-aware datetime storage - SQLAlchemy's
    DateTime type silently drops tzinfo on write regardless of the
    timezone=True flag. Every datetime in this table is UTC by convention;
    normalizing to naive UTC here makes that explicit instead of relying on
    round-tripping that doesn't actually happen.

    Postgres *does* have a timezone-aware type (TIMESTAMPTZ), so the
    original reason no longer applies there - but the convention is kept
    identical on both engines deliberately. The columns map to TIMESTAMP
    WITHOUT TIME ZONE, every stored value stays naive UTC, and the
    frontend's parseUtc helper keeps working unchanged. Switching to
    TIMESTAMPTZ on Postgres alone would make the same column mean different
    things on the two engines, which is a worse problem than the one it
    would solve."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def job_posting_from_row(row: JobPostingRow) -> JobPosting:
    """Reconstruct a JobPosting from a stored row - the inverse of what
    upsert_job stores. content_hash/description_hash are recomputed by
    JobPosting's own validator from these same fields, not read back from
    the row directly; they'll match as long as the row's identity-relevant
    fields haven't drifted from what produced them originally."""
    return JobPosting(
        source=ATSSource(row.source),
        source_job_id=row.source_job_id,
        company=row.company,
        title=row.title,
        location=row.location,
        remote_type=RemoteType(row.remote_type),
        description=row.description,
        url=row.url,
        posted_at=row.posted_at,
    )


def upsert_job(
    session: Session, job: JobPosting, rejection_rule: Optional[str], experience_years_required: Optional[float]
) -> str:
    """Insert or update one posting, keyed on content_hash. Returns "new",
    "unchanged", or "edited" so callers can tally a run summary.

    Implemented as a plain select-then-write, not a SQL-level ON CONFLICT
    upsert: this file is written by one local process once a night, so
    there's no concurrent-writer race to defend against, and select-then-write
    is far easier to read, reason about, and test than dialect-specific
    upsert SQL. application_status and first_seen are never touched by the
    update path - the entire point of this function is to never clobber a
    status the user has already set by hand.
    """
    now = _to_naive_utc(datetime.now(timezone.utc))
    existing = session.get(JobPostingRow, job.content_hash)
    filter_passed = rejection_rule is None

    if existing is None:
        session.add(
            JobPostingRow(
                content_hash=job.content_hash,
                description_hash=job.description_hash,
                source=job.source.value,
                source_job_id=job.source_job_id,
                company=job.company,
                title=job.title,
                location=job.location,
                remote_type=job.remote_type.value,
                description=job.description,
                url=str(job.url),
                posted_at=_to_naive_utc(job.posted_at),
                first_seen=now,
                last_seen=now,
                edited=False,
                filter_passed=filter_passed,
                rejection_rule=rejection_rule,
                experience_years_required=experience_years_required,
                application_status="new",
            )
        )
        return "new"

    outcome = "edited" if job.description_hash != existing.description_hash else "unchanged"

    existing.description_hash = job.description_hash
    existing.source = job.source.value
    existing.source_job_id = job.source_job_id
    existing.company = job.company
    existing.title = job.title
    existing.location = job.location
    existing.remote_type = job.remote_type.value
    existing.description = job.description
    existing.url = str(job.url)
    existing.posted_at = _to_naive_utc(job.posted_at)
    existing.last_seen = now
    existing.filter_passed = filter_passed
    existing.rejection_rule = rejection_rule
    existing.experience_years_required = experience_years_required
    if outcome == "edited":
        existing.edited = True
    # application_status and first_seen intentionally untouched

    return outcome


def upsert_jobs(session: Session, jobs: Iterable[tuple[JobPosting, Optional[str], Optional[float]]]) -> dict[str, int]:
    """Upsert a batch of (job, rejection_rule, experience_years_required)
    triples in one transaction. rejection_rule is None for a job that
    passed filtering. Returns counts per outcome:
    {"new": n, "unchanged": n, "edited": n}."""
    outcomes = {"new": 0, "unchanged": 0, "edited": 0}
    for job, rejection_rule, experience_years_required in jobs:
        outcomes[upsert_job(session, job, rejection_rule, experience_years_required)] += 1
    session.commit()
    return outcomes
