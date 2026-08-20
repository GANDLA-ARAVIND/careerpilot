from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class RunEvent(BaseModel):
    """One progress event, as served over SSE and replayed by
    /api/run/status. Mirrors pipeline.ProgressEvent plus a sequence number
    and timestamp the API adds.

    `seq` is monotonic within a run so a client that reconnects can tell
    whether it missed anything and in what order things happened - SSE
    gives no ordering guarantee across a reconnect on its own.

    `agent` is null for fetch/filter on purpose: those are pipeline stages,
    not agents. See pipeline.ProgressEvent.agent."""

    seq: int
    ts: datetime
    stage: str
    agent: Optional[str] = None
    message: str
    current: Optional[int] = None
    total: Optional[int] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RunStartRequest(BaseModel):
    """Optional body for POST /api/run.

    thread_id overrides orchestrator.run_nightly's automatic date-based
    thread selection. It exists because that automatic selection is
    deliberately idempotent: once `nightly-YYYY-MM-DD` has reached END,
    re-invoking it resumes a completed graph, which LangGraph correctly
    answers by returning the checkpointed final state without executing a
    single node. That is the right behaviour for a 2am cron that might
    fire twice - but from a UI it looks like "Start run" did nothing, with
    an empty timeline and no explanation.

    Passing an unused thread_id forces a genuinely fresh run. The UI sends
    one only when the user explicitly asks to re-run a day that already
    completed, so the ordinary path keeps the crash-resume behaviour the
    date-based ids give it."""

    thread_id: Optional[str] = None


class RunStartResponse(BaseModel):
    run_id: str
    status: str
    already_running: bool = False
    # True when the orchestrator returned without executing any node
    # because this thread had already completed - see RunStartRequest.
    # Reported rather than hidden: a silent no-op is indistinguishable
    # from a broken run from the outside.
    no_op: bool = False
    thread_id: Optional[str] = None


class RunStatus(BaseModel):
    """Everything a client needs to render Mission Control from a cold
    connect, without having watched the stream from the beginning.

    `events` carries the full replay buffer for the current/last run, so a
    browser refresh at job 30 of 41 shows the whole run rather than a blank
    pane until the next event happens to fire."""

    run_id: Optional[str] = None
    status: str  # idle | running | completed | failed
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

    current_stage: Optional[str] = None
    current_agent: Optional[str] = None
    last_message: Optional[str] = None
    current: Optional[int] = None
    total: Optional[int] = None

    events: list[RunEvent] = Field(default_factory=list)
