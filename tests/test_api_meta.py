"""Recruiter-mode metadata endpoints.

The assertions here are as much about honesty as correctness: that
unmeasured things report null rather than 0, that a missing evaluation
snapshot reports available=False rather than zeros, and that the caveats
which qualify each number are actually present in the response.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from api.services.metrics import MetricsCollector
from db import RunAgentMetricsRow, RunMetricsRow
from pipeline import AGENT_ANALYST, ProgressEvent


# ---------------------------------------------------------------------------
# GET /api/meta/agents
# ---------------------------------------------------------------------------


def test_agents_empty_before_any_run(client):
    body = client.get("/api/meta/agents").json()

    assert body["run_id"] is None
    assert body["stages"] == []
    assert body["notes"]


def _run_collector(engine, run_id="run-1"):
    collector = MetricsCollector(run_id=run_id, engine=engine, trigger="api")
    collector.start_run()
    collector(ProgressEvent(stage="fetch", message="", total=2))
    collector(ProgressEvent(stage="fetch", message="", current=1, total=2, extra={"jobs_found": 3}))
    collector(ProgressEvent(stage="fetch", message="", current=2, total=2, extra={"jobs_found": 1}))
    collector(ProgressEvent(stage="filter", message="", extra={"kept": 2, "total": 4}))
    collector(
        ProgressEvent(
            stage="stage1",
            message="",
            current=1,
            total=2,
            agent=AGENT_ANALYST,
            extra={"source": "fresh", "model": "gemini-3.5-flash-lite", "tokens": 1000},
        )
    )
    collector(
        ProgressEvent(
            stage="stage1",
            message="",
            current=2,
            total=2,
            agent=AGENT_ANALYST,
            extra={"source": "cache", "model": "gemini-3.5-flash-lite"},
        )
    )
    collector.finish_run(status="completed")
    return collector


def test_agents_reports_per_stage_metrics(client, temp_env):
    _run_collector(temp_env["engine"])

    body = client.get("/api/meta/agents").json()
    by_stage = {s["stage"]: s for s in body["stages"]}

    assert by_stage["fetch"]["companies_checked"] == 2
    assert by_stage["fetch"]["jobs_retrieved"] == 4
    assert by_stage["filter"]["jobs_processed"] == 2
    assert by_stage["stage1"]["llm_calls"] == 1
    assert by_stage["stage1"]["cache_hits"] == 1
    assert by_stage["stage1"]["cache_hit_rate"] == 0.5


def test_agents_labels_only_analyst_stages_with_an_agent(client, temp_env):
    """fetch and filter are pipeline stages. Claiming an agent ran them
    would misrepresent what the system does."""
    _run_collector(temp_env["engine"])

    body = client.get("/api/meta/agents").json()
    by_stage = {s["stage"]: s for s in body["stages"]}

    assert by_stage["fetch"]["agent"] is None
    assert by_stage["filter"]["agent"] is None
    assert by_stage["stage1"]["agent"] == AGENT_ANALYST


def test_agents_reports_retries_as_null_not_zero(client, temp_env):
    """LangGraph retries silently; nothing counts them. Null means
    unmeasured - a 0 would be a claim nobody verified."""
    _run_collector(temp_env["engine"])

    body = client.get("/api/meta/agents").json()

    assert all(stage["retries"] is None for stage in body["stages"])
    assert any("null" in note.lower() and "retr" in note.lower() for note in body["notes"])


def test_agents_returns_the_most_recent_run(client, temp_env):
    _run_collector(temp_env["engine"], run_id="older")
    _run_collector(temp_env["engine"], run_id="newer")

    with Session(temp_env["engine"]) as session:
        older = session.get(RunMetricsRow, "older")
        older.started_at = datetime(2020, 1, 1)
        session.commit()

    body = client.get("/api/meta/agents").json()

    assert body["run_id"] == "newer"


# ---------------------------------------------------------------------------
# /api/meta/runs - per-run history
# ---------------------------------------------------------------------------


def test_runs_empty_before_any_run(client):
    body = client.get("/api/meta/runs").json()

    assert body["runs"] == []
    assert body["notes"]


def test_runs_returns_history_most_recent_first(client, temp_env):
    _run_collector(temp_env["engine"], run_id="older")
    _run_collector(temp_env["engine"], run_id="newer")

    with Session(temp_env["engine"]) as session:
        session.get(RunMetricsRow, "older").started_at = datetime(2020, 1, 1)
        session.commit()

    body = client.get("/api/meta/runs").json()

    assert [r["run_id"] for r in body["runs"]] == ["newer", "older"]


def test_runs_include_per_stage_metrics(client, temp_env):
    _run_collector(temp_env["engine"])

    run = client.get("/api/meta/runs").json()["runs"][0]
    by_stage = {s["stage"]: s for s in run["stages"]}

    assert by_stage["stage1"]["llm_calls"] == 1
    assert by_stage["stage1"]["cache_hit_rate"] == 0.5
    assert run["total_llm_calls"] == 1


def test_runs_report_retries_as_null_not_zero(client, temp_env):
    """Same honesty rule as /agents - LangGraph retries silently, so the
    history must not imply it measured zero retries."""
    _run_collector(temp_env["engine"])

    run = client.get("/api/meta/runs").json()["runs"][0]

    assert all(stage["retries"] is None for stage in run["stages"])


def test_runs_duration_is_null_for_an_unfinished_run(client, temp_env):
    """A run with no finished_at crashed or is still going. Duration must
    be null rather than 0, which would read as 'took no time'."""
    collector = MetricsCollector(run_id="unfinished", engine=temp_env["engine"], trigger="api")
    collector.start_run()

    run = client.get("/api/meta/runs").json()["runs"][0]

    assert run["run_id"] == "unfinished"
    assert run["status"] == "running"
    assert run["duration_seconds"] is None


def test_runs_respects_limit(client, temp_env):
    for i in range(3):
        _run_collector(temp_env["engine"], run_id=f"run-{i}")

    body = client.get("/api/meta/runs?limit=2").json()

    assert len(body["runs"]) == 2


def test_metrics_collector_records_tokens_only_for_fresh_calls(client, temp_env):
    _run_collector(temp_env["engine"])

    with Session(temp_env["engine"]) as session:
        row = session.query(RunAgentMetricsRow).filter_by(stage="stage1").one()

    assert row.total_tokens == 1000  # the cache hit contributed nothing


def test_metrics_collector_never_raises_on_a_malformed_event(temp_env):
    """Metrics are observability, not the product - a bad event must not
    take down the run being observed."""
    collector = MetricsCollector(run_id="bad", engine=temp_env["engine"])

    class Broken:
        stage = "stage1"

        @property
        def agent(self):
            raise RuntimeError("boom")

    collector(Broken())  # must not raise


# ---------------------------------------------------------------------------
# GET /api/meta/evaluation
# ---------------------------------------------------------------------------


def test_evaluation_reports_unavailable_when_no_snapshot(client, temp_env, monkeypatch):
    """Not zeros. A metrics page showing 0.000 across the board reads as
    "measured, and terrible" when the truth is "not yet run"."""
    import api.routers.meta as meta_router

    monkeypatch.setattr(meta_router, "read_snapshot", lambda: None)

    body = client.get("/api/meta/evaluation").json()

    assert body["available"] is False
    assert body["mrr"] == {}
    assert any("evaluate_stage1.py --json" in c for c in body["caveats"])


def test_evaluation_serves_a_snapshot(client, temp_env, monkeypatch):
    import api.routers.meta as meta_router

    snapshot = {
        "generated_at": "2026-08-06T10:00:00",
        "n": 34,
        "total_labels": 122,
        "label_counts": {"good": 37, "weak": 7, "no": 78},
        "overlap_counts": {"good": 29, "weak": 2, "no": 3},
        "is_minority_of_label_set": True,
        "mrr": {"Embedding": 0.126, "Stage-1": 0.132, "Random (expected)": 0.121},
        "recall_at_10": {"Embedding": 0.31, "Stage-1": 0.31, "Random (expected)": 0.294},
        "recall_at_20": {"Embedding": 0.655, "Stage-1": 0.621, "Random (expected)": 0.588},
        "good_positions": [
            {"company": "ElevenLabs", "title": "FDE", "embedding_position": 30, "stage1_position": 1}
        ],
        "top_stage1_fit_score": 85,
        "top_stage1_company": "ElevenLabs",
        "top_stage1_title": "FDE",
        "top_stage1_label": "good",
    }
    monkeypatch.setattr(meta_router, "read_snapshot", lambda: snapshot)

    body = client.get("/api/meta/evaluation").json()

    assert body["available"] is True
    assert body["n"] == 34
    assert body["mrr"]["Stage-1"] == 0.132
    assert body["mrr"]["Random (expected)"] == 0.121
    assert body["caveats"]  # the limitations ship with the numbers


def test_evaluation_snapshot_roundtrips(tmp_path):
    from api.services.evaluation import read_snapshot, write_snapshot
    from evaluate_stage1 import EvaluationReport

    report = EvaluationReport(
        label_counts={"good": 1},
        total_labels=1,
        overlap_counts={"good": 1},
        n=1,
        is_minority_of_label_set=False,
    )
    path = tmp_path / "eval.json"
    write_snapshot(report, path)

    loaded = read_snapshot(path)

    assert loaded["n"] == 1
    assert "generated_at" in loaded


def test_read_snapshot_of_corrupt_json_returns_none(tmp_path):
    from api.services.evaluation import read_snapshot

    path = tmp_path / "eval.json"
    path.write_text("{not json", encoding="utf-8")

    assert read_snapshot(path) is None


# ---------------------------------------------------------------------------
# GET /api/meta/architecture
# ---------------------------------------------------------------------------


def test_architecture_describes_stages_agents_and_edges(client):
    body = client.get("/api/meta/architecture").json()

    stage_keys = {s["key"] for s in body["stages"]}
    assert {"discovery", "filter", "stage1", "stage2", "persist"} <= stage_keys
    assert {a["name"] for a in body["agents"]} == {"Scout", "Analyst", "Coach"}
    assert body["edges"]
    assert body["principles"]


def test_architecture_marks_which_agents_run_nightly(client):
    body = client.get("/api/meta/architecture").json()
    by_name = {a["name"]: a for a in body["agents"]}

    assert by_name["Analyst"]["runs_in_nightly_pipeline"] is True
    assert by_name["Scout"]["runs_in_nightly_pipeline"] is False
    assert by_name["Coach"]["runs_in_nightly_pipeline"] is False


def test_architecture_marks_which_stages_use_an_llm(client):
    body = client.get("/api/meta/architecture").json()
    by_key = {s["key"]: s for s in body["stages"]}

    assert by_key["filter"]["uses_llm"] is False
    assert by_key["stage1"]["uses_llm"] is True


# ---------------------------------------------------------------------------
# GET /api/meta/runtime
# ---------------------------------------------------------------------------


def test_runtime_reports_counts_and_caveats(client, seeded):
    body = client.get("/api/meta/runtime").json()

    assert body["test_count"] > 0
    assert body["job_count"] == 3
    assert body["quota"]
    assert any("floor" in c.lower() for c in body["caveats"])
    assert any("pass/fail" in c.lower() for c in body["caveats"])


def test_runtime_quota_counts_recorded_calls(client, temp_env):
    _run_collector(temp_env["engine"])

    body = client.get("/api/meta/runtime").json()
    by_model = {q["model"]: q for q in body["quota"]}

    assert by_model["gemini-3.5-flash-lite"]["calls_today"] == 1
    assert by_model["gemini-3.5-flash-lite"]["daily_limit"] == 500


def test_runtime_token_total_reflects_recorded_runs(client, temp_env):
    _run_collector(temp_env["engine"])

    body = client.get("/api/meta/runtime").json()

    assert body["total_tokens_recorded"] == 1000


def test_count_test_functions_finds_this_file(client):
    from api.services.runtime import count_test_functions

    assert count_test_functions() > 50  # the suite is well past this


# ---------------------------------------------------------------------------
# Postgres migration: db_backend replaces db_path, and last_successful_run
# gives the frontend a freshness signal that comes from the data rather
# than from a local file's mtime (there is no shared filesystem once the
# writer is a GitHub Actions runner). See docs/decisions.md.
# ---------------------------------------------------------------------------


def test_runtime_reports_the_live_backend_not_a_file_path(client):
    """db_path was a local filesystem path - meaningless once deployed,
    and substituting DATABASE_URL would publish the database password
    through an unauthenticated endpoint. The backend name is the useful,
    safe fact."""
    body = client.get("/api/meta/runtime").json()

    assert body["db_backend"] == "sqlite"  # tests always run on SQLite, even with DATABASE_URL set
    assert "db_path" not in body


def test_runtime_never_exposes_the_connection_string(client, monkeypatch):
    """A credential leak through a public endpoint would be the worst
    possible outcome of this migration - asserted directly rather than
    assumed from the absence of a field."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@example.neon.tech/neondb")

    raw = client.get("/api/meta/runtime").text

    assert "hunter2" not in raw
    assert "neon.tech" not in raw


def test_runtime_last_successful_run_is_null_before_any_run_completes(client):
    """Null means "no completed run on record" - a different fact from
    stale data, and never rendered as an epoch date."""
    body = client.get("/api/meta/runtime").json()

    assert body["last_successful_run"] is None


def test_runtime_last_successful_run_reports_a_completed_run(client, temp_env):
    engine = temp_env["engine"]
    finished = datetime(2026, 8, 19, 2, 30, 0)
    with Session(engine) as session:
        session.add(
            RunMetricsRow(
                run_id="done-1",
                started_at=datetime(2026, 8, 19, 2, 0, 0),
                finished_at=finished,
                status="completed",
                trigger="cli",
            )
        )
        session.commit()

    body = client.get("/api/meta/runtime").json()

    assert body["last_successful_run"].startswith("2026-08-19T02:30:00")


def test_runtime_last_successful_run_ignores_a_still_running_run(client, temp_env):
    """A crashed or in-flight run has no finished_at and status 'running'.
    Counting it would claim the data is fresher than it is - the same
    "don't overstate a measurement" rule the metrics panel already follows
    for retries."""
    engine = temp_env["engine"]
    with Session(engine) as session:
        session.add(
            RunMetricsRow(
                run_id="done-1",
                started_at=datetime(2026, 8, 18, 2, 0, 0),
                finished_at=datetime(2026, 8, 18, 2, 30, 0),
                status="completed",
                trigger="cli",
            )
        )
        session.add(
            RunMetricsRow(
                run_id="crashed-1",
                started_at=datetime(2026, 8, 19, 2, 0, 0),
                finished_at=None,
                status="running",
                trigger="cli",
            )
        )
        session.commit()

    body = client.get("/api/meta/runtime").json()

    assert body["last_successful_run"].startswith("2026-08-18T02:30:00")  # the completed one, not the crashed one


def test_runtime_db_size_is_measured_on_sqlite(client):
    """The null-on-Postgres path is asserted in tests/test_db.py against a
    real dialect check; here the point is that SQLite still reports a real
    measured number rather than having been made null everywhere."""
    body = client.get("/api/meta/runtime").json()

    assert body["db_size_bytes"] is not None
    assert any("wal" in c.lower() for c in body["caveats"])
