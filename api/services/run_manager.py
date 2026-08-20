"""Runs the orchestrator in a worker thread and fans its progress events
out to any number of SSE subscribers.

Why a thread at all: orchestrator.run_nightly() is synchronous and
blocking - LangGraph's invoke(), HTTP fetches, and the Analyst's
rate-limit sleeps - and takes minutes. Calling it from an async endpoint
would block the event loop and freeze every other request, including the
SSE stream meant to report on it. So it runs on a worker thread, and its
progress callback fires *on that thread*.

Getting an event from that thread into an asyncio queue safely is the whole
job of this file:

    [worker thread]                        [event loop thread]
    pipeline._emit(...)
      -> RunManager.publish(event)
           append to replay buffer  (under a lock)
           loop.call_soon_threadsafe(q.put_nowait, event)  ──> per-subscriber
                                                               asyncio.Queue
                                                                 |
                                                          SSE generator yields

call_soon_threadsafe is the only supported way to touch an asyncio object
from another thread; asyncio.Queue is not itself thread-safe. The loop
reference is captured in start(), which runs on the event loop, because the
worker thread cannot call get_running_loop() to find it.

Two properties this has to guarantee, both tested:

1. A subscriber that connects mid-run sees the whole run. Every event is
   kept in a replay buffer for the current run; a new subscriber gets the
   buffer first, then live events, deduplicated by sequence number.

2. A broken subscriber cannot affect the run. publish() never raises into
   the caller, and pipeline._emit already swallows callback exceptions on
   top of that - so a bug in the SSE layer degrades to "the UI stops
   updating", never "the nightly run dies at job 22 of 41".
"""

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# Sentinel pushed to every subscriber queue when a run ends, so SSE
# generators can close cleanly instead of hanging on an empty queue.
DONE = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RunManager:
    """One per process. Holds the state of the current (or last) run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "idle"  # idle | running | completed | failed
        self._run_id: Optional[str] = None
        self._started_at: Optional[datetime] = None
        self._finished_at: Optional[datetime] = None
        self._error: Optional[str] = None

        self._events: list[dict] = []
        self._seq = 0
        self._latest: dict[str, Any] = {}

        self._subscribers: set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    # -- state -------------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._state == "running"

    def status(self) -> dict:
        with self._lock:
            return {
                "run_id": self._run_id,
                "status": self._state,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "error": self._error,
                "current_stage": self._latest.get("stage"),
                "current_agent": self._latest.get("agent"),
                "last_message": self._latest.get("message"),
                "current": self._latest.get("current"),
                "total": self._latest.get("total"),
                "events": list(self._events),
            }

    def snapshot_events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    # -- publishing (called from the worker thread) ------------------

    def publish(self, event) -> None:
        """Record a pipeline.ProgressEvent and fan it out. Called on the
        worker thread. Never raises - a failure here must not propagate
        into the pipeline that is mid-run."""
        try:
            with self._lock:
                self._seq += 1
                payload = {
                    "seq": self._seq,
                    "ts": _utcnow(),
                    "stage": event.stage,
                    "agent": event.agent,
                    "message": event.message,
                    "current": event.current,
                    "total": event.total,
                    "extra": dict(event.extra or {}),
                }
                self._events.append(payload)
                self._latest = payload
                subscribers = list(self._subscribers)
                loop = self._loop

            self._dispatch(loop, subscribers, payload)
        except Exception:  # noqa: BLE001 - see module docstring, property 2
            pass

    def _dispatch(self, loop, subscribers, item) -> None:
        """Never raises. One subscriber's failure must not stop the others
        from being notified, and no subscriber's failure may reach the
        worker thread running the pipeline.

        The catch is deliberately broad rather than just RuntimeError (the
        "loop is closed" case): this runs on the thread executing a real
        nightly run, and there is no exception type from the notification
        path worth killing that run over."""
        if loop is None:
            return
        for queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except Exception:  # noqa: BLE001 - see docstring
                pass

    # -- subscription (called on the event loop) ---------------------

    def set_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Registers the event loop that publish() should marshal onto.

        Called from the app's lifespan startup, which is the only place
        guaranteed to be running ON the loop. This exists because
        start()'s own get_running_loop() cannot be relied on: FastAPI runs
        `def` (non-async) endpoints in a threadpool, and POST /api/run is
        one - so get_running_loop() raises there, leaving the loop unset
        and every live dispatch a silent no-op. Replay still worked, which
        is what made this invisible to tests that only checked the buffer;
        it took driving a real uvicorn server to see that live SSE events
        never arrived."""
        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.add(queue)
            if self._loop is None:
                # Belt and braces: subscribe() always runs on the event
                # loop (SSE generators do), so this recovers the loop even
                # if lifespan startup never ran - e.g. an ASGI harness that
                # skips it.
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    # -- lifecycle ---------------------------------------------------

    def start(
        self,
        runner: Optional[Callable] = None,
        on_progress_extra: Optional[Callable] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> tuple[str, bool]:
        """Begin a run. Returns (run_id, already_running).

        Only one run at a time: a second POST /api/run while one is active
        returns the existing run_id with already_running=True rather than
        starting a concurrent orchestrator. Two runs at once would
        double-spend the same Gemini free-tier quota and have both writing
        the same SQLite rows - there is no version of that worth allowing.

        `runner` defaults to orchestrator.run_nightly, imported lazily so
        that importing this module (or the whole API) doesn't pull in
        LangGraph - and so tests can inject a fake runner and never touch
        the real pipeline.

        `on_progress_extra` is composed with this manager's own publish, so
        the metrics collector rides the same callback without either
        knowing about the other."""
        with self._lock:
            if self._state == "running":
                return self._run_id, True

            run_id = uuid.uuid4().hex[:12]
            self._run_id = run_id
            self._state = "running"
            self._started_at = _utcnow()
            self._finished_at = None
            self._error = None
            self._events = []
            self._seq = 0
            self._latest = {}
            # Never clears an already-registered loop. start() frequently
            # runs OFF the event loop (FastAPI dispatches `def` endpoints to
            # a threadpool), where get_running_loop() raises - overwriting
            # the loop set by lifespan startup with None there would break
            # live delivery for every subscriber. See set_loop.
            if loop is not None:
                self._loop = loop
            elif self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    self._loop = None  # no loop anywhere yet - subscribe() will recover it

        if runner is None:
            from orchestrator import run_nightly as runner  # noqa: PLC0415 - deliberate lazy import

        def _on_progress(event) -> None:
            self.publish(event)
            if on_progress_extra is not None:
                try:
                    on_progress_extra(event)
                except Exception:  # noqa: BLE001 - metrics must not break a run either
                    pass

        def _worker() -> None:
            error: Optional[str] = None
            try:
                runner(on_progress=_on_progress)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
                error = f"{type(exc).__name__}: {exc}"
            finally:
                self._finish(error)

        thread = threading.Thread(target=_worker, name=f"careerpilot-run-{run_id}", daemon=True)
        self._thread = thread
        thread.start()
        return run_id, False

    def _finish(self, error: Optional[str]) -> None:
        """Records the terminal state, then notifies subscribers.

        State is committed under the lock BEFORE any notification, and the
        notification is separately guarded: a failure while telling
        subscribers the run ended must not prevent the run from being
        recorded as ended. Without the guard here, an exception from
        _dispatch escapes _worker's `finally` and surfaces as an unhandled
        thread exception - which a test injecting a broken dispatch caught,
        and which is exactly the "the observer breaks the observed"
        failure this whole design is meant to rule out."""
        with self._lock:
            self._state = "failed" if error else "completed"
            self._finished_at = _utcnow()
            self._error = error
            subscribers = list(self._subscribers)
            loop = self._loop
        try:
            self._dispatch(loop, subscribers, DONE)
        except Exception:  # noqa: BLE001 - belt and braces; _dispatch already guards internally
            pass

    def join(self, timeout: Optional[float] = None) -> None:
        """Test helper - wait for the worker thread. Not used by the app."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def reset(self) -> None:
        """Test helper - clear state between tests."""
        with self._lock:
            self._state = "idle"
            self._run_id = None
            self._started_at = None
            self._finished_at = None
            self._error = None
            self._events = []
            self._seq = 0
            self._latest = {}
            self._subscribers.clear()
            self._loop = None
            self._thread = None


# Module-level singleton - the API has exactly one orchestrator to manage.
run_manager = RunManager()
