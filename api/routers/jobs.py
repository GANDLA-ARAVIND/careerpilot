"""Jobs, rejections, and the funnel stats.

Route ordering note: /api/jobs/rejected is declared before
/api/jobs/{content_hash}. FastAPI matches in declaration order, so with
the reverse ordering "rejected" would be captured as a content_hash and
the rejected endpoint would be unreachable - a 404 that looks like a bug
in the frontend rather than a routing mistake here.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.deps import get_app_engine, get_session
from api.schemas.jobs import (
    JobDetail,
    JobSummary,
    RejectedJob,
    RejectedPage,
    StatsResponse,
    StatusUpdateRequest,
    StatusUpdateResponse,
)
from api.services.dashboard import load_dashboard_jobs, partition_unscored_by_experience, run_filter_pass
from app import STATUSES, read_last_viewed, set_application_status
from db import JobPostingRow

router = APIRouter(prefix="/api", tags=["jobs"])


def _to_summary(dj, *, promoted: bool = False) -> JobSummary:
    return JobSummary(
        content_hash=dj.content_hash,
        company=dj.job.company,
        title=dj.job.title,
        location=dj.job.location,
        url=str(dj.job.url),
        remote_type=dj.job.remote_type.value,
        fit_score=dj.fit_score,
        verdict=dj.verdict,
        is_unscored=dj.is_unscored,
        is_promoted_unscored=promoted,
        matched_skills=dj.matched_skills,
        missing_skills=dj.missing_skills,
        reasoning=dj.reasoning,
        model=dj.model,
        years_required=dj.years_required,
        resume_meets_experience=dj.resume_meets_it,
        application_status=dj.application_status,
        applied_at=None,  # filled by the caller from the row - see list_jobs
        first_seen=dj.first_seen,
        is_new=dj.is_new,
    )


def _applied_at_by_hash(session: Session, hashes: list[str]) -> dict[str, Optional[datetime]]:
    """One batched query rather than a per-job lookup - the same N+1 fix
    load_dashboard_jobs itself already applies to analyst results."""
    if not hashes:
        return {}
    rows = (
        session.query(JobPostingRow.content_hash, JobPostingRow.applied_at)
        .filter(JobPostingRow.content_hash.in_(hashes))
        .all()
    )
    return {content_hash: applied_at for content_hash, applied_at in rows}


@router.get("/jobs", response_model=list[JobSummary])
def list_jobs(
    status: Optional[list[str]] = Query(default=None, description="filter by application_status; repeatable"),
    include_unscored: bool = Query(default=True),
    session: Session = Depends(get_session),
) -> list[JobSummary]:
    """Ranked survivors, best fit first, with one deliberate exception.

    Unscored jobs (the Analyst found no concrete requirements to compare
    against) still never receive a fit_score and are never sorted among the
    scored ones - their score is not a real comparison. But "unscored" is a
    statement about the SKILLS comparison alone. An unscored job that states
    an experience requirement the resume meets carries real, independent
    evidence, and appending it below every scored job threw that away: a
    Cisco posting stating 0 years, met, sat dead last.

    So those specific jobs lead the list, still labelled "could not
    evaluate" - see app.partition_unscored_by_experience. Ordering here
    says "worth your attention", not "scored highest". Everything else is
    unchanged: scored jobs by fit descending, remaining unscored jobs last.

    include_unscored=False drops all of them, promoted ones included - they
    are unscored jobs, and the flag means what it says."""
    filter_result = run_filter_pass(session)
    cutoff = read_last_viewed()
    scored, unscored, _pending = load_dashboard_jobs(session, filter_result.kept, cutoff)

    promoted, remaining_unscored = partition_unscored_by_experience(unscored)
    selected = (
        list(promoted) + list(scored) + list(remaining_unscored) if include_unscored else list(scored)
    )
    if status:
        allowed = set(status)
        selected = [dj for dj in selected if dj.application_status in allowed]

    applied = _applied_at_by_hash(session, [dj.content_hash for dj in selected])
    promoted_hashes = {dj.content_hash for dj in promoted}
    summaries = []
    for dj in selected:
        summary = _to_summary(dj, promoted=dj.content_hash in promoted_hashes)
        summary.applied_at = applied.get(dj.content_hash)
        summaries.append(summary)
    return summaries


@router.get("/jobs/rejected", response_model=RejectedPage)
def list_rejected(
    rule: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> RejectedPage:
    filter_result = run_filter_pass(session)
    rejected = filter_result.rejected
    rules = sorted({r.reason for r in rejected})

    if rule:
        rejected = [r for r in rejected if r.reason == rule]

    total = len(rejected)
    start = (page - 1) * page_size
    window = rejected[start : start + page_size]

    return RejectedPage(
        items=[RejectedJob(company=r.company, title=r.title, location=r.location, reason=r.reason) for r in window],
        total=total,
        page=page,
        page_size=page_size,
        rules=rules,
    )


@router.get("/stats", response_model=StatsResponse)
def get_stats(session: Session = Depends(get_session)) -> StatsResponse:
    filter_result = run_filter_pass(session)
    cutoff = read_last_viewed()
    scored, unscored, pending = load_dashboard_jobs(session, filter_result.kept, cutoff)
    last_run = session.query(func.max(JobPostingRow.last_seen)).scalar()

    return StatsResponse(
        total_fetched=filter_result.total_fetched,
        survived=len(filter_result.kept),
        analyzed=len(scored) + len(unscored),
        pending=pending,
        rejected_by=dict(filter_result.rejected_by),
        last_run=last_run,
    )


@router.get("/jobs/{content_hash}", response_model=JobDetail)
def get_job(content_hash: str, session: Session = Depends(get_session)) -> JobDetail:
    row = session.get(JobPostingRow, content_hash)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No job with content_hash {content_hash!r}")

    filter_result = run_filter_pass(session)
    cutoff = read_last_viewed()
    scored, unscored, _pending = load_dashboard_jobs(session, filter_result.kept, cutoff)
    match = next((dj for dj in list(scored) + list(unscored) if dj.content_hash == content_hash), None)

    if match is None:
        # A real, stored job that has no Analyst result - either rejected by
        # a rule filter or not yet analyzed. Served with its real data and
        # nulls where a judgment would be, never a fabricated score.
        return JobDetail(
            content_hash=row.content_hash,
            company=row.company,
            title=row.title,
            location=row.location,
            url=row.url,
            remote_type=row.remote_type,
            fit_score=None,
            verdict="unanalyzed",
            is_unscored=True,
            matched_skills=[],
            missing_skills=[],
            reasoning="",
            model="",
            years_required=row.experience_years_required,
            resume_meets_experience=False,
            application_status=row.application_status,
            applied_at=row.applied_at,
            first_seen=row.first_seen,
            is_new=False,
            description=row.description,
        )

    summary = _to_summary(match)
    summary.applied_at = row.applied_at
    return JobDetail(**summary.model_dump(), description=match.job.description)


@router.post("/jobs/{content_hash}/status", response_model=StatusUpdateResponse)
def update_status(
    content_hash: str,
    payload: StatusUpdateRequest,
    session: Session = Depends(get_session),
) -> StatusUpdateResponse:
    """Sets application_status, and applied_at the first time a job is
    marked "applied". This records that the human applied; it never applies
    on their behalf - there is no code path from here to an employer."""
    if payload.status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {STATUSES}")

    row = session.get(JobPostingRow, content_hash)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No job with content_hash {content_hash!r}")

    set_application_status(get_app_engine(), content_hash, payload.status)
    session.expire_all()
    refreshed = session.get(JobPostingRow, content_hash)
    return StatusUpdateResponse(
        content_hash=content_hash,
        application_status=refreshed.application_status,
        applied_at=refreshed.applied_at,
    )
