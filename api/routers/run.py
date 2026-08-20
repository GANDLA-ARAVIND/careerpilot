"""Start a run, watch it over SSE, or ask where it got to.

The SSE endpoint is what Mission Control subscribes to. Its two
non-obvious requirements, both tested:

  Mid-run connect. A client that opens the stream at job 30 of 41 gets the
  whole run replayed first, then live events. Without that, a browser
  refresh mid-run shows an empty panel until the next event happens to
  fire, which for a rate-limited Analyst can be seconds of apparent
  nothing.

  No missed events across the subscribe/replay boundary. The subscription
  is registered BEFORE the replay snapshot is taken, so an event arriving
  between the two lands in the live queue rather than vanishing; the
  generator then drops anything already replayed by sequence number. The
  reverse order would have a genuine, rare race that only shows up under
  load - the worst kind to debug later.
"""

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from api.deps import get_app_engine, get_session
from api.schemas.run import RunEvent, RunStartRequest, RunStartResponse, RunStatus
from api.services.metrics import MetricsCollector
from api.services.run_manager import DONE, run_manager

router = APIRouter(prefix="/api/run", tags=["run"])

# How often to emit an SSE comment when nothing is happening. Without it,
# an idle proxy or browser can drop a quiet connection - and a run that is
# waiting on the Analyst's rate limiter is legitimately quiet for seconds
# at a time.
HEARTBEAT_SECONDS = 15.0


def _serialize(payload: dict) -> str:
    return json.dumps(payload, default=str)


@router.post("", response_model=RunStartResponse)
def start_run(
    body: Optional[RunStartRequest] = None, session: Session = Depends(get_session)
) -> RunStartResponse:
    """Starts the orchestrator in a background thread.

    One at a time: a second call while a run is active returns the running
    run's id with already_running=True rather than starting a concurrent
    one. Two orchestrators would double-spend the same Gemini free-tier
    quota and interleave writes to the same SQLite database.

    An optional body.thread_id forces a fresh graph thread - see
    RunStartRequest for why that escape hatch has to exist."""
    if run_manager.is_running:
        status = run_manager.status()
        return RunStartResponse(run_id=status["run_id"], status="running", already_running=True)

    engine = get_app_engine()
    collector_holder: dict[str, MetricsCollector] = {}

    def _metrics_callback(event) -> None:
        collector = collector_holder.get("collector")
        if collector is not None:
            collector(event)

    thread_id = body.thread_id if body else None
    runner = None
    if thread_id:
        # Lazy import kept inside the branch for the same reason
        # run_manager.start defers it: importing LangGraph is not something
        # every API process should pay for.
        from functools import partial  # noqa: PLC0415

        from orchestrator import run_nightly  # noqa: PLC0415

        runner = partial(run_nightly, thread_id=thread_id)

    run_id, already_running = run_manager.start(runner=runner, on_progress_extra=_metrics_callback)

    collector = MetricsCollector(run_id=run_id, engine=engine, trigger="api")
    collector_holder["collector"] = collector
    collector.start_run()

    def _finalize() -> None:
        run_manager.join()
        status = run_manager.status()
        collector.finish_run(
            status="failed" if status["status"] == "failed" else "completed",
            error=status["error"],
        )

    # Finalizing needs the worker thread to have finished, so it waits on
    # its own thread rather than blocking this request.
    import threading

    threading.Thread(target=_finalize, name=f"careerpilot-metrics-{run_id}", daemon=True).start()

    return RunStartResponse(
        run_id=run_id, status="running", already_running=already_running, thread_id=thread_id
    )


@router.get("/status", response_model=RunStatus)
def get_run_status() -> RunStatus:
    status = run_manager.status()
    return RunStatus(
        run_id=status["run_id"],
        status=status["status"],
        started_at=status["started_at"],
        finished_at=status["finished_at"],
        error=status["error"],
        current_stage=status["current_stage"],
        current_agent=status["current_agent"],
        last_message=status["last_message"],
        current=status["current"],
        total=status["total"],
        events=[RunEvent(**event) for event in status["events"]],
    )


@router.get("/stream")
async def stream_run() -> EventSourceResponse:
    async def event_generator():
        # Subscribe first, snapshot second - see module docstring.
        queue = run_manager.subscribe()
        try:
            replayed = run_manager.snapshot_events()
            last_seq = 0
            for payload in replayed:
                last_seq = max(last_seq, payload["seq"])
                yield {"event": "progress", "data": _serialize(payload)}

            status = run_manager.status()
            if status["status"] not in ("running",):
                # Nothing live to wait for - report the terminal state and
                # close, rather than holding a connection open forever on
                # an idle server.
                yield {"event": "done", "data": _serialize({"status": status["status"], "error": status["error"]})}
                return

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
                    continue

                if item is DONE:
                    final = run_manager.status()
                    yield {"event": "done", "data": _serialize({"status": final["status"], "error": final["error"]})}
                    return

                if item["seq"] <= last_seq:
                    continue  # already replayed
                last_seq = item["seq"]
                yield {"event": "progress", "data": _serialize(item)}
        finally:
            run_manager.unsubscribe(queue)

    return EventSourceResponse(event_generator())
