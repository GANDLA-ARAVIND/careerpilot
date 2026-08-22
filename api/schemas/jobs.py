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
    # True for an unscored job that states an experience requirement the
    # resume meets - real evidence that survives the Analyst being unable to
    # score the skills. The backend decides this (see
    # app.partition_unscored_by_experience) rather than leaving the frontend
    # to re-derive the predicate, so the two cannot drift apart. Still
    # unscored: fit_score stays null and the card still reads "could not
    # evaluate"; this only says where it belongs in the list.
    is_promoted_unscored: bool = False
    # "meets" | "unconfirmed" | "not_met" - see app.experience_eligibility.
    # The Jobs page hides "not_met" by default and sorts "unconfirmed" below
    # "meets", so the classification lives here rather than being re-derived
    # in TypeScript where it would drift.
    eligibility: str = "meets"
    # fit_score is EXACTLY 0 - the Analyst compared the resume and found
    # nothing in common (every such job in the archive also has empty
    # matched_skills). A stronger signal than the experience check, and the
    # Jobs page hides these alongside the ineligible ones.
    #
    # Computed here rather than as `fit_score === 0` in the frontend for one
    # specific reason: an unscored job has fit_score NULL, and a JavaScript
    # falsy test (!fit_score) would catch null too - silently removing the
    # promoted "could not evaluate" job that sits at position 1. Null is not
    # zero, and this field makes that impossible to get wrong downstream.
    is_zero_fit: bool = False
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


class AppliedJobSummary(BaseModel):
    """One job you applied to.

    Deliberately NOT derived from /api/jobs. That endpoint returns current
    rule-filter survivors, so a job applied to that later fails a filter
    silently disappeared from the application record - and filters do
    change (a title-experience rule added later rejected 34 postings in one
    pass). Application history is a record of what you did; it must not be
    contingent on today's filter configuration.

    applied_at is nullable: two rows were marked applied before the column
    existed, and inventing a date for them would be worse than showing none.
    """

    content_hash: str
    company: str
    title: str
    location: Optional[str] = None
    url: str
    application_status: str
    applied_at: Optional[datetime] = None
    # Whether this job still passes the current rule filters. Purely
    # informational - it never affects whether the job is listed - but a
    # posting that would no longer survive is worth seeing as such.
    still_a_survivor: bool = True


class AppliedJobsResponse(BaseModel):
    items: list[AppliedJobSummary] = Field(default_factory=list)
    dated_count: int = 0
    undated_count: int = 0


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
