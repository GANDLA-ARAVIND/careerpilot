from typing import Optional

from pydantic import BaseModel, Field


class CompanyStats(BaseModel):
    """One companies.yaml entry plus what it actually contributed.
    fetched > 0 with survivors == 0 means the company is costing a nightly
    fetch and returning nothing usable - a removal candidate."""

    name: str
    ats: str
    # Optional because Workday entries have no single token - they're
    # identified by workday_tenant/workday_wd/workday_site instead (see
    # models.CompanyConfig). This was `str` until a real request with
    # Workday companies in companies.yaml returned a 500: the test fixture
    # only ever had a Greenhouse entry, so nothing covered the null.
    token: Optional[str] = None
    jobs_fetched: int
    survivors: int


class CompaniesResponse(BaseModel):
    companies: list[CompanyStats]
    zero_survivor_companies: list[str]


class ScoutRequest(BaseModel):
    name: str


class ScoutEmptyBoard(BaseModel):
    token: str
    source: str


class ScoutResponse(BaseModel):
    """What Scout concluded. `success=False` with a non-empty
    `empty_boards` is a distinct, useful outcome from a flat miss: a real
    board exists, it just has zero open jobs right now, so it's worth
    re-scouting later rather than writing the company off.

    Nothing here is written to companies.yaml - POST /api/companies does
    that, deliberately as a separate, explicit step."""

    company_name: str
    success: bool
    ats: Optional[str] = None
    token: Optional[str] = None
    job_count: Optional[int] = None
    conclusion: str
    requests_used: int
    empty_boards: list[ScoutEmptyBoard] = Field(default_factory=list)


class AddCompanyRequest(BaseModel):
    name: str
    ats: str
    token: str
    notes: Optional[str] = None


class AddCompanyResponse(BaseModel):
    added: bool
    company: CompanyStats
    total_companies: int
