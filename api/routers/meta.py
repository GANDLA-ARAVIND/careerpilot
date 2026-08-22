"""Recruiter-mode metadata: what ran, how well it works, how it's built.

The governing rule for this whole module: every number is measured or
labelled as unmeasured. A showcase page is exactly where it's tempting to
round a null up to a plausible-looking figure, and exactly where doing so
is worst - these are claims about the engineer's own work being made to
someone evaluating it.

Concretely, in this file: retries are null (LangGraph retries silently, so
nothing counted them), quota is a floor (CLI runs spend the same quota and
write no metrics row), test_count is functions defined and not a pass
result, and evaluation reports available=False rather than zeros when the
snapshot hasn't been generated.
"""

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps import get_session
from api.schemas.meta import (
    AgentMetric,
    AgentsResponse,
    ArchitectureAgent,
    ArchitectureEdge,
    ArchitectureResponse,
    ArchitectureStage,
    EvaluationResponse,
    GoodJobPosition,
    QuotaModel,
    RunHistoryEntry,
    RunHistoryResponse,
    RuntimeResponse,
)
from api.services import runtime as runtime_service
from api.services.dashboard import run_filter_pass
from api.services.evaluation import CAVEATS, read_snapshot
from db import DEFAULT_DB_PATH, RunAgentMetricsRow, RunMetricsRow, last_successful_run

router = APIRouter(prefix="/api/meta", tags=["meta"])


METRIC_NOTES = [
    "fetch and filter are pipeline stages, not agents - agent is null for them.",
    "Only the Analyst runs in the nightly pipeline. Scout runs on demand via "
    "POST /api/companies/scout; Coach runs weekly over the archive.",
    "retries is null because LangGraph retries nodes silently - nothing measures them. "
    "Null means unmeasured, not zero.",
]


def _to_agent_metric(row: RunAgentMetricsRow) -> AgentMetric:
    """One mapping shared by /agents and /runs.

    cache_hit_rate is None - not 0.0 - when nothing was looked up at all,
    because "no cache lookups happened" and "every lookup missed" are
    different facts and a rate of zero would claim the second."""
    looked_up = row.cache_hits + row.cache_misses
    return AgentMetric(
        stage=row.stage,
        agent=row.agent,
        model=row.model,
        companies_checked=row.companies_checked,
        jobs_retrieved=row.jobs_retrieved,
        jobs_processed=row.jobs_processed,
        llm_calls=row.llm_calls,
        cache_hits=row.cache_hits,
        cache_misses=row.cache_misses,
        cache_hit_rate=(row.cache_hits / looked_up) if looked_up else None,
        retries=row.retries,
        duration_seconds=row.duration_seconds,
    )


@router.get("/runs", response_model=RunHistoryResponse)
def run_history(limit: int = 20, session: Session = Depends(get_session)) -> RunHistoryResponse:
    """Recorded runs, most recent first, each with its per-stage metrics.

    Only runs started through this API appear here - a CLI run spends the
    same quota but writes no metrics row (see /runtime's caveats), so this
    is a history of API-triggered runs, not of every run that happened."""
    limit = max(1, min(limit, 100))
    runs = (
        session.query(RunMetricsRow).order_by(RunMetricsRow.started_at.desc()).limit(limit).all()
    )
    if not runs:
        return RunHistoryResponse(notes=["No runs recorded yet. Start one with POST /api/run."])

    rows_by_run: dict[str, list[RunAgentMetricsRow]] = {}
    for row in (
        session.query(RunAgentMetricsRow)
        .filter(RunAgentMetricsRow.run_id.in_([r.run_id for r in runs]))
        .order_by(RunAgentMetricsRow.started_at)
        .all()
    ):
        rows_by_run.setdefault(row.run_id, []).append(row)

    entries = []
    for run in runs:
        stage_rows = rows_by_run.get(run.run_id, [])
        entries.append(
            RunHistoryEntry(
                run_id=run.run_id,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                trigger=run.trigger,
                error=run.error,
                duration_seconds=(
                    (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
                ),
                total_llm_calls=sum(row.llm_calls for row in stage_rows),
                stages=[_to_agent_metric(row) for row in stage_rows],
            )
        )

    return RunHistoryResponse(
        runs=entries,
        notes=[
            *METRIC_NOTES,
            "Only runs started through this API are recorded. A CLI run spends the same "
            "quota but writes no row here.",
        ],
    )


@router.get("/agents", response_model=AgentsResponse)
def agent_metrics(session: Session = Depends(get_session)) -> AgentsResponse:
    """Per-stage metrics from the most recent recorded run."""
    run = session.query(RunMetricsRow).order_by(RunMetricsRow.started_at.desc()).first()
    if run is None:
        return AgentsResponse(
            notes=["No runs recorded yet. Start one with POST /api/run."],
        )

    rows = (
        session.query(RunAgentMetricsRow)
        .filter(RunAgentMetricsRow.run_id == run.run_id)
        .order_by(RunAgentMetricsRow.started_at)
        .all()
    )

    stages = [_to_agent_metric(row) for row in rows]

    return AgentsResponse(
        run_id=run.run_id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        trigger=run.trigger,
        stages=stages,
        notes=METRIC_NOTES,
    )


@router.get("/evaluation", response_model=EvaluationResponse)
def evaluation() -> EvaluationResponse:
    snapshot = read_snapshot()
    if snapshot is None:
        return EvaluationResponse(
            available=False,
            caveats=["No evaluation snapshot yet. Generate one with: python evaluate_stage1.py --json"],
        )

    return EvaluationResponse(
        available=True,
        generated_at=snapshot.get("generated_at"),
        n=snapshot.get("n", 0),
        total_labels=snapshot.get("total_labels", 0),
        label_counts=snapshot.get("label_counts", {}),
        overlap_counts=snapshot.get("overlap_counts", {}),
        is_minority_of_label_set=snapshot.get("is_minority_of_label_set", False),
        mrr=snapshot.get("mrr", {}),
        recall_at_10=snapshot.get("recall_at_10", {}),
        recall_at_20=snapshot.get("recall_at_20", {}),
        good_positions=[GoodJobPosition(**p) for p in snapshot.get("good_positions", [])],
        top_stage1_fit_score=snapshot.get("top_stage1_fit_score"),
        top_stage1_company=snapshot.get("top_stage1_company"),
        top_stage1_title=snapshot.get("top_stage1_title"),
        top_stage1_label=snapshot.get("top_stage1_label"),
        caveats=CAVEATS,
    )


@router.get("/architecture", response_model=ArchitectureResponse)
def architecture(session: Session = Depends(get_session)) -> ArchitectureResponse:
    """A structured description of the pipeline, for the frontend to render
    as a diagram. The shape lives here rather than in the React app so there
    is one source of truth for what the pipeline actually is.

    Mostly static, with the funnel figures counted live - see below. Two
    accuracy failures this endpoint previously had, both worth naming since
    this is the page shown to people evaluating the work: it listed an
    "Embedding rank" stage between filter and stage 1 that had been removed
    from the live path (measured at random on the label set), and a
    principle quoting "~30 jobs a night" long after the real survivor count
    had moved. Anything countable here is now counted."""
    # Counted live. A hardcoded figure here read "~30 jobs a night" long
    # after the real number had moved, on the page whose whole purpose is
    # describing the system accurately.
    filter_result = run_filter_pass(session)
    survivors = len(filter_result.kept)
    total = sum(filter_result.fetched_by_company.values())
    return ArchitectureResponse(
        stages=[
            ArchitectureStage(
                key="discovery",
                name="ATS discovery",
                description=(
                    "Detect which applicant tracking system a company uses and fetch its public JSON "
                    "board. Greenhouse, Lever and Ashby adapters; scraping is a long-tail fallback only."
                ),
                uses_llm=False,
                module="adapters/",
                node="fetch_persist_filter",
            ),
            ArchitectureStage(
                key="normalize",
                name="Normalize + dedupe",
                description="Map every source's payload onto one JobPosting shape; dedupe on content_hash.",
                uses_llm=False,
                module="models.py, db.py",
                node="fetch_persist_filter",
            ),
            ArchitectureStage(
                key="filter",
                name="Rule filters",
                description=(
                    "Title allowlist, seniority and non-engineering rejects, India location match. "
                    "No LLM: this is what cuts thousands of postings down to dozens."
                ),
                uses_llm=False,
                module="filters.py",
                node="fetch_persist_filter",
            ),
            ArchitectureStage(
                key="stage1",
                name="Analyst screen (stage 1)",
                description="Cheap model scores every survivor: fit score, matched/missing skills, experience gap.",
                uses_llm=True,
                module="agents/analyst.py",
                node="stage1_analyze",
            ),
            ArchitectureStage(
                key="stage2",
                name="Analyst deep pass (stage 2)",
                description="Stronger model re-checks only the top candidates by stage-1 score.",
                uses_llm=True,
                module="agents/analyst.py",
                node="stage2_analyze",
            ),
            ArchitectureStage(
                key="persist",
                name="Persist",
                description="SQLite. Rejected postings are kept too - they are the RAG archive corpus.",
                uses_llm=False,
                module="db.py",
                node="fetch_persist_filter",
            ),
        ],
        agents=[
            ArchitectureAgent(
                name="Scout",
                decision="Which ATS does this company use, and what is its board token?",
                description=(
                    "Generates candidate tokens mechanically, tests each against real board APIs, and "
                    "falls back to an LLM for non-obvious shapes. Loops and retries different hypotheses - "
                    "the genuinely agentic one."
                ),
                module="agents/scout.py",
                runs_in_nightly_pipeline=False,
            ),
            ArchitectureAgent(
                name="Analyst",
                decision="How well does this candidate fit this role, and what is missing?",
                description="Structured, Pydantic-validated judgment per job. The only agent in the nightly path.",
                module="agents/analyst.py",
                runs_in_nightly_pipeline=True,
            ),
            ArchitectureAgent(
                name="Coach",
                decision="What patterns across the archive should change what I learn next?",
                description="Weekly RAG over accumulated JDs and application history.",
                module="agents/coach.py",
                runs_in_nightly_pipeline=False,
            ),
        ],
        edges=[
            ArchitectureEdge(source="discovery", target="normalize", label="raw postings"),
            ArchitectureEdge(source="normalize", target="filter", label="JobPosting"),
            ArchitectureEdge(source="filter", target="stage1", label="survivors"),
            ArchitectureEdge(source="stage1", target="stage2", label="top N by fit score"),
            ArchitectureEdge(source="stage1", target="persist", label="verdicts"),
            ArchitectureEdge(source="stage2", target="persist", label="verdicts"),
            ArchitectureEdge(source="filter", target="persist", label="rejected (RAG corpus)"),
        ],
        principles=[
            f"Cheap filters first. LLM calls happen on the {survivors} postings that survive rule "
            f"filtering, not the {total:,} fetched - this is what keeps the system inside free-tier quotas.",
            "The system finds and ranks; the human applies. Nothing auto-applies, ever.",
            "Cache on what determined the output. Analyst verdicts are keyed on a hash of model + prompt "
            "+ resume + requirements, so any change to those invalidates exactly the affected entries.",
            "An unscored job is not a zero. When the Analyst has no concrete requirements to compare "
            "against, it says so rather than inventing a number.",
            "Measured, then removed. Embedding similarity was built, evaluated against the hand-labeled "
            "set, found to rank at chance, and taken out of the live path - it is kept only as the "
            "evaluation baseline.",
        ],
        orchestrator_nodes=["fetch_persist_filter", "stage1_analyze", "stage2_analyze"],
        not_in_pipeline=[
            "Embedding rank (ranking.py) - measured at or below random on the label set, so it does not "
            "pre-select jobs for the LLM. Still runs as the evaluation baseline and behind "
            "pipeline.py --ranked.",
            "Scout (agents/scout.py) - on demand, when a new company is added. Not part of a nightly run.",
            "Coach (agents/coach.py) - weekly RAG over the archive, triggered from the Career Coach page.",
        ],
    )


@router.get("/runtime", response_model=RuntimeResponse)
def runtime(session: Session = Depends(get_session)) -> RuntimeResponse:
    db_path = Path(DEFAULT_DB_PATH)
    jobs, analyses = runtime_service.row_counts(session)
    quota = runtime_service.quota_usage_today(session)
    backend = runtime_service.database_backend(session)
    size_bytes = runtime_service.database_size_bytes(db_path, backend)

    caveats = [
        "test_count is the number of test functions defined under tests/, not a pass/fail result "
        "and not the collected case count (parametrized tests count once).",
        "Quota and token figures are summed from runs started through this API. A run launched "
        "from the CLI spends the same Gemini quota but records no metrics row, so these are a "
        "floor, not a ledger.",
        "last_successful_run is the finish time of the most recent run with status 'completed'. "
        "A crashed or still-running run is deliberately not counted - reporting its start time "
        "as freshness would overstate how current the data is.",
    ]
    caveats.append(
        "db_size_bytes is not measured on Postgres - there is no local database file to stat, "
        "so it is null rather than zero."
        if size_bytes is None
        else "db_size_bytes includes the -wal and -shm sidecar files."
    )

    return RuntimeResponse(
        test_count=runtime_service.count_test_functions(),
        db_size_bytes=size_bytes,
        db_backend=backend,
        job_count=jobs,
        analyst_result_count=analyses,
        total_tokens_recorded=runtime_service.total_tokens_recorded(session),
        last_successful_run=last_successful_run(session),
        quota=[QuotaModel(**q) for q in quota],
        caveats=caveats,
    )
