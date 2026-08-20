"""LangGraph orchestration for the nightly run: fetch -> persist -> filter
-> stage-1 analyze -> stage-2 on top STAGE2_TOP_N. This is the scheduled
path (cron/APScheduler at 2am, see CLAUDE.md) - pipeline.py's CLI keeps
working unchanged for manual/diagnostic commands (--filtered, --ranked,
--analyze --stage 1, --no-fetch, ...). Every real step here calls straight
into pipeline.py's/filters.py's existing functions rather than
reimplementing them, so the two entry points never drift into two different
behaviors for the same operation.

Node structure was decided against three things verified live against the
installed langgraph (1.2.10), not assumed from memory - see
docs/decisions.md for the full experiments:

1. Graph state must be plain-typed (str/int/float/bool/dict/list only).
   Pydantic models (JobPosting, CompanyConfig, AnalystResult) fail to
   serialize through the checkpointer at all. State here carries
   content_hash identifiers, never job objects - every node re-derives the
   real JobPosting from the database when it needs one. This isn't a
   workaround for a limitation; the database is already the real state
   store (every posting and every Analyst verdict is already persisted
   there), so graph state only needs to be coordination metadata.

2. fetch + persist + filter are bundled into ONE node, not three. All three
   are fast, idempotent, and carry zero LLM-quota risk - splitting them
   would only buy checkpoint granularity that isn't worth anything (redoing
   a fetch after a persist failure costs a few lightweight API calls, not
   real time or money), while costing real complexity for no benefit
   (fetched jobs would have to cross a node boundary, hitting problem #1
   above for nothing).

3. "Just run the script again" after a crash must pass None to invoke(),
   never fresh input - passing real input on a thread with any existing
   history replays the entry node from scratch even if it already
   succeeded (verified live). run_nightly() checks existing checkpoint
   state before deciding which to pass.
"""

import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agents.analyst import is_unscored
from config import load_companies
from db import JobPostingRow, database_url, get_engine, job_posting_from_row
from filters import filter_jobs
from models import JobPosting
from pipeline import ProgressCallback, ProgressEvent, fetch_all, persist_jobs, print_analyst_stage1, print_analyst_stage2

CHECKPOINT_DB_PATH = Path("data/orchestrator_checkpoints.db")

# How far back to search for an incomplete previous thread before starting
# today fresh. A same-day-only thread_id silently refetches everything the
# moment midnight passes after a crash - "died at 2am, noticed the next
# morning" is a one-day gap in the realistic case, but a longer gap
# (missed a few days) shouldn't permanently orphan a crashed run either.
# Bounded rather than unbounded so this stays a handful of cheap
# get_state() calls, not an ever-growing scan. See docs/decisions.md.
LOOKBACK_DAYS = 7


class PipelineState(TypedDict, total=False):
    persist_outcomes: dict[str, int]
    fetch_failure_count: int
    kept_hashes: list[str]
    rejected_by: dict[str, int]
    stage1_ranked_hashes: list[str]
    stage2_hashes: list[str]


def _jobs_from_hashes(hashes: list[str]) -> list[JobPosting]:
    """The inverse of storing content_hash in state - re-derive real
    JobPostings from the database, same reconstruction db.py already
    provides for pipeline.py's --no-fetch mode."""
    engine = get_engine()
    with Session(engine) as session:
        rows = [session.get(JobPostingRow, content_hash) for content_hash in hashes]
        return [job_posting_from_row(row) for row in rows if row is not None]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def fetch_persist_filter(state: PipelineState, on_progress: Optional[ProgressCallback] = None) -> dict:
    companies = load_companies()
    print(f"Fetching jobs for {len(companies)} companies...")

    # An engine, deliberately not an open Session. fetch_all opens its own
    # short sessions around the fetch loop and holds none across it - see
    # its docstring for the idle-in-transaction timeout that made this
    # non-negotiable. Passing a live Session here is what caused a real
    # nightly run to be killed by Neon after 44 minutes.
    jobs, failures, skipped = fetch_all(companies, on_progress=on_progress, engine=get_engine())

    if failures:
        print(f"FAILURES ({len(failures)}):")
        for company, reason in failures:
            print(f"  ! {company.name} ({company.ats.value}): {reason}")

    if skipped:
        print(f"SKIPPED ({len(skipped)}, weekly cadence not yet due):")
        for company, reason in skipped:
            print(f"  - {company.name}: {reason}")

    outcomes = persist_jobs(jobs)
    print(f"Persisted: {outcomes['new']} new, {outcomes['unchanged']} unchanged, {outcomes['edited']} edited")

    if on_progress is not None:
        on_progress(ProgressEvent(stage="filter", message="Filtering..."))

    engine = get_engine()
    with Session(engine) as session:
        rows = session.query(JobPostingRow).all()
        all_jobs = [job_posting_from_row(row) for row in rows]
    kept, rejected_by = filter_jobs(all_jobs)
    print(f"Survived filters: {len(kept)} / {len(all_jobs)}")
    if on_progress is not None:
        on_progress(
            ProgressEvent(
                stage="filter",
                message=f"Filtering... {len(kept)} of {len(all_jobs)} survived.",
                extra={"kept": len(kept), "total": len(all_jobs)},
            )
        )

    return {
        "persist_outcomes": outcomes,
        "fetch_failure_count": len(failures),
        "kept_hashes": [job.content_hash for job in kept],
        "rejected_by": dict(rejected_by),
    }


def stage1_analyze(state: PipelineState, on_progress: Optional[ProgressCallback] = None) -> dict:
    kept_jobs = _jobs_from_hashes(state["kept_hashes"])
    results = print_analyst_stage1(kept_jobs, on_progress=on_progress)
    # Unscored jobs (empty matched AND empty missing - see
    # agents/analyst.py's is_unscored) have no real fit_score to rank by,
    # so they're excluded here too - route_after_stage1 checks this list to
    # decide whether stage 2 has anything worth re-checking, and an
    # all-unscored batch must not look like a real, rankable stage-1 pass.
    rankable = [(job, result) for job, result in results if not is_unscored(result)]
    ranked = sorted(rankable, key=lambda pair: pair[1].fit_score, reverse=True)
    return {"stage1_ranked_hashes": [job.content_hash for job, _ in ranked]}


def stage2_analyze(state: PipelineState, on_progress: Optional[ProgressCallback] = None) -> dict:
    kept_jobs = _jobs_from_hashes(state["kept_hashes"])
    results = print_analyst_stage2(kept_jobs, on_progress=on_progress)
    return {"stage2_hashes": [job.content_hash for job, _ in results]}


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------


def route_after_fetch(state: PipelineState) -> str:
    outcomes = state["persist_outcomes"]
    if outcomes["new"] + outcomes["edited"] == 0:
        print()
        print("No new or edited jobs since the last run - skipping analysis.")
        return END
    if not state["kept_hashes"]:
        print()
        print("New/edited jobs arrived, but none survived rule filtering - skipping analysis.")
        return END
    return "stage1_analyze"


def route_after_stage1(state: PipelineState) -> str:
    if not state.get("stage1_ranked_hashes"):
        print()
        print("Stage 1 produced no results - skipping stage 2.")
        return END
    return "stage2_analyze"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


# LangGraph's own default predicate, read off RetryPolicy rather than
# imported from langgraph._internal._retry - it lives in a private module
# whose path has already moved between versions, and the default of a
# public class is the stable way to reach it.
_LANGGRAPH_DEFAULT_RETRY_ON = RetryPolicy().retry_on


def _retry_fetch_on(exc: Exception) -> bool:
    """Retry predicate for the fetch node specifically.

    Retrying this node is not cheap the way retrying stage1/stage2 is: it
    re-issues every ATS request for every company, thousands of them when
    the large Workday tenants are due, against other people's
    infrastructure. LangGraph's default predicate returns True for anything
    it does not explicitly exclude - which includes psycopg's
    OperationalError, verified directly. That is how one Neon
    idle-in-transaction kill turned into a complete second fetch of all 67
    companies in a real run: the log showed "Fetching jobs for 67
    companies..." twice.

    So: never retry a database error here. A DB failure is either
    configuration (which a retry cannot fix) or connection-level (which the
    engine's pool_pre_ping already handles at a much lower level, without
    redoing the network work). The node already survives per-company
    adapter failures internally by collecting them into `failures` rather
    than raising, so a retry is not what protects against a flaky board
    either - it is reserved for a genuinely unexpected in-process error
    during the cheap persist/filter phase.

    The run stays recoverable regardless: state is checkpointed in
    Postgres, so a failed run resumes rather than restarting."""
    if isinstance(exc, SQLAlchemyError) or type(exc).__module__.split(".")[0] == "psycopg":
        return False
    return _LANGGRAPH_DEFAULT_RETRY_ON(exc)


def build_graph(on_progress: Optional[ProgressCallback] = None) -> StateGraph:
    """on_progress: forwarded into every node via a closure, not by
    registering the node functions directly - LangGraph calls a node with
    just `state` (it has no way to know to supply a second `on_progress`
    argument on its own), so the only way a real callback actually reaches
    fetch_persist_filter/stage1_analyze/stage2_analyze is to bind it here
    before add_node ever sees the function. build_graph() with no argument
    (every existing caller: the CLI below, every test) binds on_progress=
    None into each closure, which is exactly the original no-callback
    behavior - print() stays the only output, unchanged."""
    graph = StateGraph(PipelineState)

    def _fetch_persist_filter(state: PipelineState) -> dict:
        return fetch_persist_filter(state, on_progress)

    def _stage1_analyze(state: PipelineState) -> dict:
        return stage1_analyze(state, on_progress)

    def _stage2_analyze(state: PipelineState) -> dict:
        return stage2_analyze(state, on_progress)

    # Retry policy is a safety net for a genuinely unexpected in-process
    # failure (a DB hiccup, a bug) - NOT what handles LLM quota exhaustion.
    # That's already handled inside _run_analyst_over_jobs (pipeline.py) by
    # catching per-job and stopping the batch cleanly without raising -
    # letting that propagate here instead would mean retrying immediately
    # after a daily-quota 429 just hits the same cap again. Default
    # retry_on (verified live) retries our own ATSAdapterError/LLMError but
    # not plain bugs (TypeError/ValueError/...), which is exactly right -
    # no custom retry_on needed.
    graph.add_node(
        "fetch_persist_filter", _fetch_persist_filter, retry_policy=RetryPolicy(max_attempts=3, retry_on=_retry_fetch_on)
    )
    graph.add_node("stage1_analyze", _stage1_analyze, retry_policy=RetryPolicy(max_attempts=2))
    graph.add_node("stage2_analyze", _stage2_analyze, retry_policy=RetryPolicy(max_attempts=2))

    graph.set_entry_point("fetch_persist_filter")
    # Explicit path_map (not strictly required - route_after_fetch already
    # returns real node names/END directly) so the graph's possible
    # destinations are declared statically, not just discoverable by
    # reading the routing function's body - matters for get_graph()-based
    # visualization/introspection and for anyone reading this file cold.
    graph.add_conditional_edges(
        "fetch_persist_filter", route_after_fetch, {"stage1_analyze": "stage1_analyze", END: END}
    )
    graph.add_conditional_edges("stage1_analyze", route_after_stage1, {"stage2_analyze": "stage2_analyze", END: END})
    graph.add_edge("stage2_analyze", END)

    return graph


# ---------------------------------------------------------------------------
# Thread selection and entry point
# ---------------------------------------------------------------------------


def _thread_id_for(day: date) -> str:
    return f"nightly-{day.isoformat()}"


def _find_resumable_thread(app, today: date, lookback_days: int = LOOKBACK_DAYS) -> Optional[str]:
    """Scan the last `lookback_days` calendar days, most recent first, for a
    thread that has run before but hasn't reached END - i.e. crashed or was
    killed mid-graph. A thread that completed normally (whether the full
    cascade or an early conditional skip) always shows next == () - both
    look identical to "nothing more to do", which is exactly what should
    NOT be resumed. Returns the thread_id of the first incomplete one
    found, else None."""
    for offset in range(lookback_days):
        candidate = _thread_id_for(today - timedelta(days=offset))
        snapshot = app.get_state({"configurable": {"thread_id": candidate}})
        if snapshot.values and snapshot.next:
            return candidate
    return None


@contextmanager
def _checkpointer():
    """Postgres when DATABASE_URL is set, SQLite otherwise - the same split
    db.get_engine makes, for the same reason but a different consequence.

    The checkpointer is what gives the nightly run its two load-bearing
    properties: same-day idempotency (a second run on an already-completed
    thread executes nothing) and crash recovery (a run that died at job 22
    of 30 resumes rather than refetching). Both depend entirely on the
    checkpoint store outliving the process.

    On a local machine, data/orchestrator_checkpoints.db does that. On
    GitHub Actions it does not: the runner's filesystem is destroyed when
    the job ends, so a SQLite checkpointer would start empty on every run
    and both properties would silently stop holding - no error, no warning,
    just an orchestrator whose stated justification (see this module's
    docstring, and CLAUDE.md) quietly stopped being true in the one
    environment that ships. Postgres is where that state has to live once
    the runner is ephemeral.

    PostgresSaver.setup() is idempotent and creates LangGraph's own tables
    (separate from the six in db.py) on first use."""
    url = database_url()
    if url is None:
        CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(CHECKPOINT_DB_PATH)) as saver:
            yield saver
        return

    # PostgresSaver takes a psycopg connection string, not a SQLAlchemy
    # URL - the "+psycopg" driver marker db.database_url() adds for
    # SQLAlchemy is not valid here and has to come back off.
    with PostgresSaver.from_conn_string(url.replace("postgresql+psycopg://", "postgresql://", 1)) as saver:
        saver.setup()
        yield saver


def run_nightly(thread_id: Optional[str] = None, on_progress: Optional[ProgressCallback] = None) -> dict:
    graph = build_graph(on_progress)

    with _checkpointer() as saver:
        app = graph.compile(checkpointer=saver)

        today = date.today()
        if thread_id is None:
            resumable = _find_resumable_thread(app, today)
            if resumable:
                print(f"Found an incomplete run on thread {resumable!r} - resuming it instead of starting today fresh.")
            thread_id = resumable or _thread_id_for(today)

        config = {"configurable": {"thread_id": thread_id}}
        print(f"Running nightly pipeline (thread_id={thread_id!r})")
        print()

        existing = app.get_state(config)
        if existing.values:
            # This thread has history - either crashed partway (resume from
            # the pending node) or already completed (a true no-op,
            # verified live). Passing fresh input instead would replay the
            # entry node even in the completed case - see module docstring.
            result = app.invoke(None, config=config)
        else:
            result = app.invoke({}, config=config)  # genuinely first run on this thread

        print()
        print(f"Done. Final state: {result}")
        return result


if __name__ == "__main__":
    import argparse

    # Same fix as pipeline.py/evaluate.py/agents/*.py - job text isn't
    # guaranteed ASCII, and Windows defaults stdout to cp1252 off-console.
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--thread-id",
        default=None,
        help="override automatic thread selection (date-based, with lookback resume of an incomplete prior thread)",
    )
    args = parser.parse_args()

    run_nightly(thread_id=args.thread_id)
