"""Turns a run's progress events into rows in run_metrics /
run_agent_metrics.

MetricsCollector is *itself* a pipeline.ProgressCallback. That's the whole
design: orchestrator.py and pipeline.py gain no knowledge that metrics
exist - api/routers/run.py composes this with the SSE publisher and hands
the pair to the callback parameter that already existed. Nothing in the
nightly path had to change to make per-run metrics possible.

What each stage's numbers come from:

  fetch    total -> companies_checked; sum of extra["jobs_found"] ->
           jobs_retrieved. Failed companies still emit (with jobs_found 0),
           so companies_checked reflects every company attempted.
  filter   extra["kept"] / extra["total"] -> jobs_processed / jobs_retrieved
  stage1   one event per job. source == "cache" -> cache_hits;
  stage2   source == "fresh" -> cache_misses and one llm_call. Tokens are
           summed only from fresh events (see pipeline.py - last_usage is
           stale on a cache hit).

Deliberately NOT derived: retries. LangGraph retries a node silently, so
there is nothing to count. The column stays NULL - "not measured" - rather
than 0, which would be a confident claim nobody verified.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from db import RunAgentMetricsRow, RunMetricsRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _StageAccumulator:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.agent: Optional[str] = None
        self.model: Optional[str] = None
        self.companies_checked: Optional[int] = None
        self.jobs_retrieved: Optional[int] = None
        self.jobs_processed: Optional[int] = None
        self.llm_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.total_tokens = 0
        self.started_at = _utcnow()
        self.finished_at = _utcnow()


class MetricsCollector:
    """Accumulates in memory during the run, writes once at the end.

    In-memory rather than a row per event on purpose: a 41-job stage-1 pass
    would otherwise be 41 write transactions competing with the pipeline's
    own writes on the same SQLite file, to record something nobody reads
    until the run is over.

    Every public method swallows its own exceptions. Metrics are
    observability, not the product - a failure to record them must never
    take down the run being observed."""

    def __init__(self, run_id: str, engine, trigger: str = "api") -> None:
        self.run_id = run_id
        self._engine = engine
        self._trigger = trigger
        self._stages: dict[str, _StageAccumulator] = {}

    def __call__(self, event) -> None:
        """The ProgressCallback entry point."""
        try:
            self._record(event)
        except Exception:  # noqa: BLE001 - see class docstring
            pass

    def _record(self, event) -> None:
        acc = self._stages.get(event.stage)
        if acc is None:
            acc = _StageAccumulator(event.stage)
            self._stages[event.stage] = acc
        acc.finished_at = _utcnow()

        if event.agent:
            acc.agent = event.agent
        extra = event.extra or {}
        if extra.get("model"):
            acc.model = extra["model"]

        if event.stage == "fetch":
            if event.total is not None:
                acc.companies_checked = event.total
            if "jobs_found" in extra:
                acc.jobs_retrieved = (acc.jobs_retrieved or 0) + int(extra.get("jobs_found") or 0)
            return

        if event.stage == "filter":
            if "kept" in extra:
                acc.jobs_processed = extra.get("kept")
                acc.jobs_retrieved = extra.get("total")
            return

        # Analyst stages
        if event.total is not None:
            acc.jobs_retrieved = event.total
        if event.current is not None:
            acc.jobs_processed = event.current

        source = extra.get("source")
        if source == "cache":
            acc.cache_hits += 1
        elif source == "fresh":
            acc.cache_misses += 1
            acc.llm_calls += 1
            acc.total_tokens += int(extra.get("tokens") or 0)

    def start_run(self, thread_id: Optional[str] = None) -> None:
        try:
            with Session(self._engine) as session:
                session.add(
                    RunMetricsRow(
                        run_id=self.run_id,
                        thread_id=thread_id,
                        started_at=_utcnow(),
                        status="running",
                        trigger=self._trigger,
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    def finish_run(self, status: str, error: Optional[str] = None) -> None:
        try:
            with Session(self._engine) as session:
                row = session.get(RunMetricsRow, self.run_id)
                if row is not None:
                    row.status = status
                    row.error = error
                    row.finished_at = _utcnow()

                for acc in self._stages.values():
                    session.add(
                        RunAgentMetricsRow(
                            run_id=self.run_id,
                            stage=acc.stage,
                            agent=acc.agent,
                            model=acc.model,
                            companies_checked=acc.companies_checked,
                            jobs_retrieved=acc.jobs_retrieved,
                            jobs_processed=acc.jobs_processed,
                            llm_calls=acc.llm_calls,
                            cache_hits=acc.cache_hits,
                            cache_misses=acc.cache_misses,
                            total_tokens=acc.total_tokens,
                            retries=None,  # not measured - see module docstring
                            started_at=acc.started_at,
                            finished_at=acc.finished_at,
                            duration_seconds=(acc.finished_at - acc.started_at).total_seconds(),
                        )
                    )
                session.commit()
        except Exception:  # noqa: BLE001
            pass
