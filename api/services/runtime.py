"""Runtime facts for the technical-showcase page.

Every number here is either measured directly or explicitly caveated. The
temptation on a page like this is to present derived figures as exact;
where that isn't true, the response says so in `caveats` rather than
quietly rounding the truth off.

Specifically:
  test_count      a static count of `def test_` across tests/. That is the
                  number of test functions defined, NOT a pass/fail result -
                  running pytest per request would be slow and, worse, would
                  make an HTTP endpoint capable of reporting green when the
                  suite is red.
  quota           summed from run_agent_metrics, which only records runs
                  started through this API. A CLI run spends the same Gemini
                  free-tier quota and writes no row, so these are a floor.
"""

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from config import GEMINI_RATE_LIMITS
from db import AnalystResultRow, JobPostingRow, RunAgentMetricsRow, RunMetricsRow

TESTS_DIR = Path("tests")
_TEST_DEF_RE = re.compile(r"^\s*def (test_\w+)", re.MULTILINE)


def count_test_functions(tests_dir: Path = TESTS_DIR) -> int:
    """Static count of test functions defined under tests/. Parametrized
    tests count once here even though pytest expands them into several
    cases, so this is a floor on the real case count - stated in the
    endpoint's caveats rather than presented as the collected total."""
    if not tests_dir.exists():
        return 0
    total = 0
    for path in tests_dir.rglob("test_*.py"):
        try:
            total += len(_TEST_DEF_RE.findall(path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return total


def database_size_bytes(db_path: Path, backend: str) -> Optional[int]:
    """On-disk size of the SQLite database, including the -wal sidecar.
    With WAL enabled (see db.py) a meaningful amount of recently written
    data can be sitting in careerpilot.db-wal rather than the main file, so
    reporting only the main file would understate the real footprint.

    Returns None on Postgres - not 0. There is no local file to stat, and
    the honest answer is "not measured from here", the same distinction
    this codebase already draws for retries and cache_hit_rate. A 0 would
    render as "0 B" on the Agent Metrics page and read as a measurement.
    Postgres can report its own size via pg_database_size(), but that's a
    privileged query against a hosted database to fill in a decorative
    stat, so it isn't asked.

    `backend` is passed in from the live connection (see
    database_backend), NOT read from DATABASE_URL. An earlier version
    checked the environment variable and got this wrong in exactly the way
    that matters: with DATABASE_URL set in a developer's .env, a session
    genuinely connected to SQLite still reported "no file to measure".
    Config describes intent; the connection is the fact."""
    if backend == "postgresql":
        return None
    total = 0
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            total += candidate.stat().st_size
    return total


def database_backend(session: Session) -> str:
    """"postgresql" or "sqlite" - what the API is actually talking to,
    read off the live connection rather than inferred from config, so it
    can't disagree with reality."""
    return session.get_bind().dialect.name


def quota_usage_today(session: Session) -> list[dict]:
    """LLM calls per model recorded today, against each model's documented
    daily ceiling."""
    today = date.today()
    start = datetime(today.year, today.month, today.day)

    rows = (
        session.query(
            RunAgentMetricsRow.model,
            func.sum(RunAgentMetricsRow.llm_calls),
        )
        .join(RunMetricsRow, RunMetricsRow.run_id == RunAgentMetricsRow.run_id)
        .filter(RunMetricsRow.started_at >= start)
        .filter(RunAgentMetricsRow.model.isnot(None))
        .group_by(RunAgentMetricsRow.model)
        .all()
    )
    recorded = {model: int(calls or 0) for model, calls in rows}

    usage = []
    for model, limits in GEMINI_RATE_LIMITS.items():
        usage.append(
            {
                "model": model,
                "calls_today": recorded.get(model, 0),
                "daily_limit": limits.get("rpd"),
                "requests_per_minute": limits.get("rpm"),
            }
        )
    return usage


def total_tokens_recorded(session: Session) -> int:
    return int(session.query(func.coalesce(func.sum(RunAgentMetricsRow.total_tokens), 0)).scalar() or 0)


def row_counts(session: Session) -> tuple[int, int]:
    jobs = int(session.query(func.count(JobPostingRow.content_hash)).scalar() or 0)
    analyses = int(session.query(func.count(AnalystResultRow.text_hash)).scalar() or 0)
    return jobs, analyses


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
