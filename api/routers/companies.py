"""The company roster, Scout, and adding to companies.yaml.

Scout and "add to companies.yaml" are separate endpoints on purpose. Scout
spends real requests probing ATS boards and can legitimately come back
with "found a real board that currently has zero jobs" - a result worth
seeing before deciding whether the company earns a place in the nightly
fetch. Auto-writing every success would also mean a Scout call silently
changes what the pipeline does tonight.
"""

from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.deps import get_session
from api.schemas.companies import (
    AddCompanyRequest,
    AddCompanyResponse,
    CompaniesResponse,
    CompanyStats,
    ScoutEmptyBoard,
    ScoutRequest,
    ScoutResponse,
)
from api.services.dashboard import compute_company_stats, run_filter_pass
from config import GEMINI_MODEL_STAGE1, GEMINI_RATE_LIMITS, load_companies
from models import ATSSource, CompanyConfig

router = APIRouter(prefix="/api/companies", tags=["companies"])

COMPANIES_PATH = Path("companies.yaml")


@router.get("", response_model=CompaniesResponse)
def list_companies(session: Session = Depends(get_session)) -> CompaniesResponse:
    filter_result = run_filter_pass(session)
    stats = compute_company_stats(load_companies(), filter_result.fetched_by_company, filter_result.kept)
    return CompaniesResponse(
        companies=[
            CompanyStats(
                name=s.name, ats=s.ats, token=s.token, jobs_fetched=s.jobs_fetched, survivors=s.survivors
            )
            for s in stats
        ],
        zero_survivor_companies=[s.name for s in stats if s.jobs_fetched > 0 and s.survivors == 0],
    )


@router.post("/scout", response_model=ScoutResponse)
def run_scout(payload: ScoutRequest) -> ScoutResponse:
    """Runs the Scout agent against one company name.

    Blocking and potentially slow - Scout probes up to MAX_TOTAL_ATTEMPTS
    board endpoints and may consult the LLM once when mechanical candidates
    are exhausted. Kept synchronous rather than pushed through the run
    manager because it's bounded, single-company, and the caller wants the
    answer, not a stream. Writes nothing."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name cannot be empty")

    # Imported lazily: agents.scout pulls the adapters and the LLM client,
    # which the rest of the API has no reason to load at import time.
    from agents.scout import scout
    from llm import GeminiClient

    rpm = GEMINI_RATE_LIMITS.get(GEMINI_MODEL_STAGE1, {}).get("rpm")
    client = GeminiClient(model=GEMINI_MODEL_STAGE1, requests_per_minute=rpm)
    result = scout(name, llm_client=client)

    return ScoutResponse(
        company_name=result.company_name,
        success=result.success,
        ats=result.config.ats.value if result.config else None,
        token=result.config.token if result.config else None,
        job_count=next((a.job_count for a in result.attempts if a.outcome == "found"), None),
        conclusion=result.conclusion,
        requests_used=len(result.attempts),
        empty_boards=[ScoutEmptyBoard(token=a.token, source=a.source.value) for a in result.empty_boards],
    )


@router.post("", response_model=AddCompanyResponse, status_code=201)
def add_company(payload: AddCompanyRequest, session: Session = Depends(get_session)) -> AddCompanyResponse:
    """Appends to companies.yaml.

    Written with yaml.safe_dump over the parsed list rather than appending
    a hand-built string, so a company name containing a colon or a quote
    can't produce a file that no longer parses - the same reasoning
    agents/scout.py's _found_as_yaml already applies."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name cannot be empty")
    try:
        ats = ATSSource(payload.ats)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"ats must be one of {[s.value for s in ATSSource]}"
        ) from exc

    # AddCompanyRequest only carries `token` - the shape every other source
    # uses. Workday needs workday_tenant/workday_wd/workday_site instead
    # (see models.CompanyConfig's validator) and this endpoint has nowhere
    # to collect them, so it's rejected explicitly here with a clear reason
    # rather than falling through to CompanyConfig's validator, which would
    # raise a pydantic ValidationError FastAPI doesn't auto-convert to a
    # 422 for a manually-constructed model - that would 500 instead of
    # explaining the real problem. A Workday entry is added by hand to
    # companies.yaml (see docs/decisions.md) until this endpoint grows the
    # fields to support it.
    if ats == ATSSource.WORKDAY:
        raise HTTPException(
            status_code=422,
            detail=(
                "workday isn't supported via this endpoint yet - it needs workday_tenant/"
                "workday_wd/workday_site, not token. Add it directly to companies.yaml."
            ),
        )

    existing = load_companies()
    if any(c.name.strip().lower() == name.lower() for c in existing):
        raise HTTPException(status_code=409, detail=f"{name!r} is already in companies.yaml")

    try:
        entry = CompanyConfig(name=name, ats=ats, token=payload.token.strip(), notes=payload.notes)
    except ValidationError as exc:
        # Defense in depth for any future source with its own cross-field
        # requirement like Workday's - same reasoning as the explicit
        # check above, generalized instead of hardcoded to one ats value.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raw = yaml.safe_load(COMPANIES_PATH.read_text(encoding="utf-8")) or []
    new_entry = {"name": entry.name, "ats": entry.ats.value, "token": entry.token}
    if entry.notes:
        new_entry["notes"] = entry.notes
    raw.append(new_entry)
    COMPANIES_PATH.write_text(
        yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True), encoding="utf-8"
    )

    filter_result = run_filter_pass(session)
    return AddCompanyResponse(
        added=True,
        company=CompanyStats(
            name=entry.name,
            ats=entry.ats.value,
            token=entry.token,
            jobs_fetched=filter_result.fetched_by_company.get(entry.name, 0),
            survivors=sum(1 for job in filter_result.kept if job.company == entry.name),
        ),
        total_companies=len(raw),
    )
