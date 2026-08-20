"""Run control and SSE.

No test here starts the real orchestrator. Every one injects a fake runner
that emits a scripted sequence of pipeline.ProgressEvents - so the tests
exercise the manager, the fan-out and the SSE framing without a network
call, an LLM call, or a minutes-long wait.
"""

import json
import time

import pytest

from api.services.run_manager import RunManager
from pipeline import AGENT_ANALYST, ProgressEvent


def _script(on_progress) -> dict:
    """A miniature run: fetch -> filter -> stage1 -> stage2."""
    on_progress(ProgressEvent(stage="fetch", message="Fetching from 2 companies...", total=2))
    on_progress(
        ProgressEvent(
            stage="fetch", message="fetch 1/2", current=1, total=2, extra={"company": "Acme", "jobs_found": 3}
        )
    )
    on_progress(
        ProgressEvent(
            stage="fetch", message="fetch 2/2", current=2, total=2, extra={"company": "Globex", "jobs_found": 1}
        )
    )
    on_progress(ProgressEvent(stage="filter", message="Filtering...", extra={"kept": 2, "total": 4}))
    on_progress(
        ProgressEvent(
            stage="stage1",
            message="Stage 1: analyzing 2 job(s)...",
            total=2,
            agent=AGENT_ANALYST,
            extra={"model": "gemini-3.5-flash-lite"},
        )
    )
    on_progress(
        ProgressEvent(
            stage="stage1",
            message="[1/2] Acme | SWE",
            current=1,
            total=2,
            agent=AGENT_ANALYST,
            extra={"source": "fresh", "model": "gemini-3.5-flash-lite", "tokens": 1200, "call_count": 1},
        )
    )
    on_progress(
        ProgressEvent(
            stage="stage1",
            message="[2/2] Globex | Backend",
            current=2,
            total=2,
            agent=AGENT_ANALYST,
            extra={"source": "cache", "model": "gemini-3.5-flash-lite"},
        )
    )
    return {"stage1_ranked_hashes": ["a", "b"], "stage2_hashes": []}


def fake_runner(on_progress=None, thread_id=None):
    if on_progress:
        return _script(on_progress)
    return {}


def failing_runner(on_progress=None, thread_id=None):
    if on_progress:
        on_progress(ProgressEvent(stage="fetch", message="Fetching...", total=1))
    raise RuntimeError("simulated quota exhaustion")


def _wait_for_completion(manager: RunManager, timeout: float = 5.0) -> None:
    manager.join(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline and manager.is_running:
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# RunManager - the machinery, tested directly
# ---------------------------------------------------------------------------


def test_run_manager_records_every_event():
    manager = RunManager()
    manager.start(runner=fake_runner)
    _wait_for_completion(manager)

    status = manager.status()
    assert status["status"] == "completed"
    assert len(status["events"]) == 7
    assert [e["seq"] for e in status["events"]] == list(range(1, 8))


def test_run_manager_marks_agent_only_on_analyst_stages():
    """fetch and filter are pipeline stages, not agents - their events must
    carry agent=None rather than being labelled with an agent that didn't
    run."""
    manager = RunManager()
    manager.start(runner=fake_runner)
    _wait_for_completion(manager)

    by_stage = {}
    for event in manager.status()["events"]:
        by_stage.setdefault(event["stage"], set()).add(event["agent"])

    assert by_stage["fetch"] == {None}
    assert by_stage["filter"] == {None}
    assert by_stage["stage1"] == {AGENT_ANALYST}


def test_run_manager_refuses_a_concurrent_run():
    """Two orchestrators at once would double-spend the same free-tier
    quota and interleave writes to one SQLite file."""
    started = []
    release = []

    def slow_runner(on_progress=None, thread_id=None):
        started.append(True)
        while not release:
            time.sleep(0.01)
        return {}

    manager = RunManager()
    first_id, already = manager.start(runner=slow_runner)
    assert already is False
    while not started:
        time.sleep(0.01)

    second_id, already_running = manager.start(runner=slow_runner)
    assert already_running is True
    assert second_id == first_id

    release.append(True)
    _wait_for_completion(manager)


def test_run_manager_records_failure_without_raising():
    manager = RunManager()
    manager.start(runner=failing_runner)
    _wait_for_completion(manager)

    status = manager.status()
    assert status["status"] == "failed"
    assert "simulated quota exhaustion" in status["error"]


def test_run_manager_starts_fresh_events_each_run():
    manager = RunManager()
    manager.start(runner=fake_runner)
    _wait_for_completion(manager)
    first_run_id = manager.status()["run_id"]

    manager.start(runner=fake_runner)
    _wait_for_completion(manager)
    status = manager.status()

    assert status["run_id"] != first_run_id
    assert len(status["events"]) == 7  # not 14 - the buffer is per-run


# ---------------------------------------------------------------------------
# The property the user asked to be proven: a broken SSE layer cannot
# affect the run it is observing.
# ---------------------------------------------------------------------------


def test_a_raising_subscriber_callback_cannot_break_the_run():
    """publish() is called on the worker thread, mid-pipeline. If a bug in
    the SSE/publish path could propagate, it would kill a real nightly run
    partway through - the exact failure mode _emit's try/except and
    publish()'s own guard exist to prevent.

    This drives the failure at the publish boundary specifically, rather
    than trusting that _emit alone covers it, so the guarantee holds even
    if a future refactor moves who calls whom."""
    manager = RunManager()

    boom_count = {"n": 0}
    original_dispatch = manager._dispatch

    def exploding_dispatch(*args, **kwargs):
        boom_count["n"] += 1
        raise RuntimeError("simulated SSE layer bug")

    manager._dispatch = exploding_dispatch

    manager.start(runner=fake_runner)
    _wait_for_completion(manager)

    status = manager.status()
    assert boom_count["n"] > 0, "the exploding dispatch never ran - the test proved nothing"
    assert status["status"] == "completed", "a broken SSE layer took down the run"
    assert len(status["events"]) == 7, "events were lost when the subscriber raised"

    manager._dispatch = original_dispatch


def test_a_raising_dispatch_during_finish_does_not_escape_the_worker_thread():
    """Regression: _finish() notifies subscribers that the run ended. An
    earlier version called _dispatch there unguarded, so a broken
    subscriber raised *out of _worker's finally block* and surfaced as an
    unhandled thread exception. The run was still recorded correctly, but
    only because the state write happened to come first - the exception
    escaping at all is the bug.

    Detection is via threading.excepthook, not pytest's recwarn: pytest
    reports unhandled thread exceptions as a warning raised at *teardown*,
    which recwarn inside the test body never sees. A first version of this
    test used recwarn and passed with the bug deliberately reintroduced -
    i.e. it proved nothing. Verified the other way too: with the guard in
    _finish removed, this version fails."""
    import threading as threading_module

    escaped: list = []
    original_hook = threading_module.excepthook
    threading_module.excepthook = lambda args: escaped.append(args)
    try:
        manager = RunManager()
        manager._dispatch = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated SSE layer bug"))

        manager.start(runner=fake_runner)
        _wait_for_completion(manager)

        assert manager.status()["status"] == "completed"
    finally:
        threading_module.excepthook = original_hook

    assert not escaped, f"exception escaped the worker thread: {[str(a.exc_value) for a in escaped]}"


def test_start_does_not_clear_a_loop_registered_by_lifespan():
    """Regression, and the most dangerous bug found in this build: live SSE
    delivery was silently dead in production while every test passed.

    FastAPI runs `def` (non-async) endpoints in a threadpool. POST /api/run
    is one, so asyncio.get_running_loop() raises inside start(). An earlier
    version assigned `self._loop = None` in that case, wiping the loop the
    app's lifespan had registered - after which _dispatch had no loop to
    marshal onto and dropped every live event. Replay from the buffer still
    worked, so /api/run/status and a post-run stream both looked correct;
    only a browser watching a live run would have seen nothing.

    Simulates the threadpool condition by calling start() from a plain
    thread with no running loop, after a loop has been registered."""
    import threading as threading_module

    class _SentinelLoop:
        def call_soon_threadsafe(self, fn, *args):
            fn(*args)

    manager = RunManager()
    sentinel = _SentinelLoop()
    manager.set_loop(sentinel)

    result = {}

    def start_off_the_loop():
        result["ids"] = manager.start(runner=fake_runner)

    thread = threading_module.Thread(target=start_off_the_loop)
    thread.start()
    thread.join()
    _wait_for_completion(manager)

    assert manager._loop is sentinel, "start() wiped the loop registered by lifespan"


def test_a_raising_metrics_callback_cannot_break_the_run():
    """Same guarantee for the other composed callback."""

    def exploding_metrics(event):
        raise RuntimeError("simulated metrics bug")

    manager = RunManager()
    manager.start(runner=fake_runner, on_progress_extra=exploding_metrics)
    _wait_for_completion(manager)

    status = manager.status()
    assert status["status"] == "completed"
    assert len(status["events"]) == 7


def test_emit_swallows_a_raising_callback_at_the_pipeline_boundary():
    """The other half of the same guarantee, at pipeline._emit itself."""
    import pipeline

    def exploding(event):
        raise RuntimeError("boom")

    pipeline._emit(exploding, ProgressEvent(stage="fetch", message="x"))  # must not raise


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def test_run_status_is_idle_before_any_run(client):
    body = client.get("/api/run/status").json()

    assert body["status"] == "idle"
    assert body["run_id"] is None
    assert body["events"] == []


def test_post_run_starts_a_run(client, monkeypatch):
    import api.routers.run as run_router

    monkeypatch.setattr(run_router.run_manager, "start", lambda **kw: ("test-run-id", False))

    body = client.post("/api/run").json()

    assert body["run_id"] == "test-run-id"
    assert body["status"] == "running"
    assert body["already_running"] is False


def test_post_run_accepts_no_body(client, monkeypatch):
    """The body is optional - a bare POST must keep working, since that's
    the normal path and what every existing caller sends."""
    import api.routers.run as run_router

    monkeypatch.setattr(run_router.run_manager, "start", lambda **kw: ("bare-run", False))

    response = client.post("/api/run")

    assert response.status_code == 200
    assert response.json()["thread_id"] is None


def test_post_run_without_thread_id_uses_the_default_runner(client, monkeypatch):
    """runner=None lets run_manager.start do its own lazy import of
    orchestrator.run_nightly, which is what keeps LangGraph out of the
    import path for every other endpoint."""
    import api.routers.run as run_router

    captured: dict = {}

    def _fake_start(**kwargs):
        captured.update(kwargs)
        return ("no-thread-run", False)

    monkeypatch.setattr(run_router.run_manager, "start", _fake_start)

    client.post("/api/run", json={"thread_id": None})

    assert captured["runner"] is None


def test_post_run_with_thread_id_forwards_it_to_the_orchestrator(client, monkeypatch):
    """A thread_id must reach run_nightly as a keyword argument - that's
    the whole escape hatch for re-running a day whose date-based thread
    already completed (see api/schemas/run.py's RunStartRequest). Asserted
    on the partial's keywords rather than by running anything, so this
    test never touches the real orchestrator."""
    import api.routers.run as run_router

    captured: dict = {}

    def _fake_start(**kwargs):
        captured.update(kwargs)
        return ("threaded-run", False)

    monkeypatch.setattr(run_router.run_manager, "start", _fake_start)

    body = client.post("/api/run", json={"thread_id": "manual-2026-08-06-test"}).json()

    runner = captured["runner"]
    assert runner is not None
    assert runner.keywords == {"thread_id": "manual-2026-08-06-test"}
    assert body["thread_id"] == "manual-2026-08-06-test"


def test_post_run_while_running_returns_existing_run(client, monkeypatch):
    import api.routers.run as run_router

    monkeypatch.setattr(type(run_router.run_manager), "is_running", property(lambda self: True))
    monkeypatch.setattr(
        run_router.run_manager,
        "status",
        lambda: {
            "run_id": "already-going",
            "status": "running",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "current_stage": None,
            "current_agent": None,
            "last_message": None,
            "current": None,
            "total": None,
            "events": [],
        },
    )

    body = client.post("/api/run").json()

    assert body["already_running"] is True
    assert body["run_id"] == "already-going"


def test_run_status_replays_events_after_a_completed_run(client):
    from api.services.run_manager import run_manager

    run_manager.start(runner=fake_runner)
    _wait_for_completion(run_manager)

    body = client.get("/api/run/status").json()

    assert body["status"] == "completed"
    assert len(body["events"]) == 7
    assert body["events"][0]["stage"] == "fetch"
    assert body["last_message"] == "[2/2] Globex | Backend"


def test_sse_stream_replays_a_finished_run_then_closes(client):
    """A client connecting after the run finished still gets the whole
    history, then a terminal done event - not an open connection hanging
    on a queue nothing will ever push to."""
    from api.services.run_manager import run_manager

    run_manager.start(runner=fake_runner)
    _wait_for_completion(run_manager)

    with client.stream("GET", "/api/run/stream") as response:
        assert response.status_code == 200
        raw = "".join(response.iter_text())

    progress_payloads = [
        json.loads(line[len("data: ") :])
        for line in raw.splitlines()
        if line.startswith("data: ") and "seq" in line
    ]
    assert len(progress_payloads) == 7
    assert [p["seq"] for p in progress_payloads] == list(range(1, 8))
    assert "event: done" in raw


def test_sse_stream_on_idle_server_closes_immediately(client):
    with client.stream("GET", "/api/run/stream") as response:
        raw = "".join(response.iter_text())

    assert "event: done" in raw


def test_sse_stream_reports_failure_in_the_done_event(client):
    from api.services.run_manager import run_manager

    run_manager.start(runner=failing_runner)
    _wait_for_completion(run_manager)

    with client.stream("GET", "/api/run/stream") as response:
        raw = "".join(response.iter_text())

    done_line = next(
        line for line in raw.splitlines() if line.startswith("data: ") and "status" in line and "seq" not in line
    )
    payload = json.loads(done_line[len("data: ") :])
    assert payload["status"] == "failed"
    assert "simulated quota exhaustion" in payload["error"]
