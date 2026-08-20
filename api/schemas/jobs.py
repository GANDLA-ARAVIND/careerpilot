from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class JobSummary(BaseModel):
    """One ranked survivor. No description - see JobDetail. fit_score and
    verdict are Optional because an "unscored" job (no concrete technical
    requirements in the posting, see agents/analyst.py's is_unscored) has
    no real score, and serving a 0 or a 50 there would be inventing a
    judgment the Analyst explicitly declined to make. is_unscored says
    which case a null is."""

    content_hash: str
    company: str
    title: str
    location: Optional[str] = None
    url: str
    remote_type: str

    fit_score: Optional[int] = None
    verdict: str
    is_unscored: bool
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reasoning: str
    model: str

    years_required: Optional[float] = None  # null = not stated in the posting, never "0 years"
    resume_meets_experience: bool

    application_status: str
    applied_at: Optional[datetime] = None
    first_seen: datetime
    is_new: bool


class JobDetail(JobSummary):
    description: str


class StatusUpdateRequest(BaseModel):
    status: str


class StatusUpdateResponse(BaseModel):
    content_hash: str
    application_status: str
    applied_at: Optional[datetime] = None


class RejectedJob(BaseModel):
    company: str
    title: str
    location: Optional[str] = None
    reason: str


class RejectedPage(BaseModel):
    items: list[RejectedJob]
    total: int
    page: int
    page_size: int
    rules: list[str]  # every rule present in the full (unpaginated, unfiltered) set


class StatsResponse(BaseModel):
    """The funnel. `pending` is survivors with no Analyst result yet -
    surfaced as its own number rather than folded into `analyzed`, because
    "the orchestrator hasn't reached it" and "the Analyst looked and had
    nothing to compare" are different states with different fixes."""

    total_fetched: int
    survived: int
    analyzed: int
    pending: int
    rejected_by: dict[str, int]
    last_run: Optional[datetime] = None
