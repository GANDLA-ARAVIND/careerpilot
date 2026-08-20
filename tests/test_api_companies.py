"""Companies, Scout, and adding to companies.yaml.

No test here runs the real Scout - it probes dozens of live ATS endpoints
and can call the LLM. agents.scout.scout is monkeypatched at the router's
import site instead.
"""

import yaml

import api.routers.companies as companies_router
from models import ATSSource, CompanyConfig


class _FakeScoutAttempt:
    def __init__(self, token, source, outcome, job_count=None):
        self.token = token
        self.source = source
        self.outcome = outcome
        self.job_count = job_count


class _FakeScoutResult:
    def __init__(self, company_name, success, config=None, attempts=None, conclusion="", empty_boards=None):
        self.company_name = company_name
        self.success = success
        self.config = config
        self.attempts = attempts or []
        self.conclusion = conclusion
        self.empty_boards = empty_boards or []


def _point_at_temp_yaml(monkeypatch, temp_env):
    """companies.yaml is written by the router and read by
    config.load_companies - both must point at the temp copy."""
    path = temp_env["companies_path"]
    monkeypatch.setattr(companies_router, "COMPANIES_PATH", path)
    monkeypatch.setattr(companies_router, "load_companies", lambda: _load(path))
    return path


def _load(path):
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [CompanyConfig(**entry) for entry in raw]


# ---------------------------------------------------------------------------
# GET /api/companies
# ---------------------------------------------------------------------------


def test_list_companies_reports_fetched_and_survivors(client, seeded, temp_env, monkeypatch):
    _point_at_temp_yaml(monkeypatch, temp_env)

    body = client.get("/api/companies").json()

    acme = next(c for c in body["companies"] if c["name"] == "Acme Corp")
    assert acme["jobs_fetched"] == 1
    assert acme["survivors"] == 1
    assert acme["ats"] == "greenhouse"


def test_list_companies_handles_a_workday_entry_with_no_token(client, temp_env, monkeypatch):
    """Regression: CompanyStats.token was typed `str`, so a Workday entry
    (which has no token - it uses workday_tenant/wd/site) made this
    endpoint return a 500. Nothing caught it because the shared fixture's
    companies.yaml only ever had a Greenhouse company in it, which is
    exactly why this test writes a Workday one."""
    path = _point_at_temp_yaml(monkeypatch, temp_env)
    path.write_text(
        "- name: Cisco\n"
        "  ats: workday\n"
        "  workday_tenant: cisco\n"
        "  workday_wd: wd5\n"
        "  workday_site: Cisco_Careers\n"
        "  cadence: weekly\n",
        encoding="utf-8",
    )

    response = client.get("/api/companies")

    assert response.status_code == 200
    cisco = next(c for c in response.json()["companies"] if c["name"] == "Cisco")
    assert cisco["token"] is None  # null, not "" - it genuinely has no token
    assert cisco["ats"] == "workday"


def test_list_companies_flags_zero_survivor_companies(client, temp_env, monkeypatch):
    """A company that fetched jobs but contributed no survivors is a
    removal candidate - the whole point of this view."""
    path = _point_at_temp_yaml(monkeypatch, temp_env)
    path.write_text("- name: Initech\n  ats: greenhouse\n  token: initech\n", encoding="utf-8")

    from sqlalchemy.orm import Session

    from db import upsert_job
    from tests.conftest import make_posting

    with Session(temp_env["engine"]) as session:
        upsert_job(
            session,
            make_posting(company="Initech", title="Senior Staff Engineer", source_job_id="9"),
            rejection_rule="seniority",
            experience_years_required=None,
        )
        session.commit()

    body = client.get("/api/companies").json()

    assert "Initech" in body["zero_survivor_companies"]


# ---------------------------------------------------------------------------
# POST /api/companies/scout
# ---------------------------------------------------------------------------


def test_scout_returns_a_found_board(client, monkeypatch):
    def fake_scout(name, llm_client=None, **kwargs):
        return _FakeScoutResult(
            company_name=name,
            success=True,
            config=CompanyConfig(name=name, ats=ATSSource.GREENHOUSE, token="razorpaysoftwareprivatelimited"),
            attempts=[_FakeScoutAttempt("razorpaysoftwareprivatelimited", ATSSource.GREENHOUSE, "found", 12)],
            conclusion="Found: token='razorpaysoftwareprivatelimited' on greenhouse (12 job(s)).",
        )

    monkeypatch.setattr("agents.scout.scout", fake_scout)
    monkeypatch.setattr("llm.GeminiClient", lambda **kwargs: object())

    body = client.post("/api/companies/scout", json={"name": "Razorpay"}).json()

    assert body["success"] is True
    assert body["token"] == "razorpaysoftwareprivatelimited"
    assert body["ats"] == "greenhouse"
    assert body["job_count"] == 12


def test_scout_reports_an_empty_board_distinctly_from_a_miss(client, monkeypatch):
    """A real board with zero current jobs is not the same as "not on any
    supported ATS" - it's worth re-scouting later."""

    def fake_scout(name, llm_client=None, **kwargs):
        return _FakeScoutResult(
            company_name=name,
            success=False,
            attempts=[_FakeScoutAttempt("freshworks", ATSSource.LEVER, "empty_board", 0)],
            conclusion="No working board found; found existing but EMPTY board(s).",
            empty_boards=[_FakeScoutAttempt("freshworks", ATSSource.LEVER, "empty_board", 0)],
        )

    monkeypatch.setattr("agents.scout.scout", fake_scout)
    monkeypatch.setattr("llm.GeminiClient", lambda **kwargs: object())

    body = client.post("/api/companies/scout", json={"name": "Freshworks"}).json()

    assert body["success"] is False
    assert body["empty_boards"] == [{"token": "freshworks", "source": "lever"}]


def test_scout_writes_nothing_to_companies_yaml(client, temp_env, monkeypatch):
    """Scout reports; adding is a separate, explicit decision."""
    path = _point_at_temp_yaml(monkeypatch, temp_env)
    before = path.read_text(encoding="utf-8")

    def fake_scout(name, llm_client=None, **kwargs):
        return _FakeScoutResult(
            company_name=name,
            success=True,
            config=CompanyConfig(name=name, ats=ATSSource.ASHBY, token="newco"),
            attempts=[_FakeScoutAttempt("newco", ATSSource.ASHBY, "found", 5)],
            conclusion="Found.",
        )

    monkeypatch.setattr("agents.scout.scout", fake_scout)
    monkeypatch.setattr("llm.GeminiClient", lambda **kwargs: object())

    client.post("/api/companies/scout", json={"name": "NewCo"})

    assert path.read_text(encoding="utf-8") == before


def test_scout_rejects_an_empty_name(client):
    assert client.post("/api/companies/scout", json={"name": "   "}).status_code == 422


# ---------------------------------------------------------------------------
# POST /api/companies
# ---------------------------------------------------------------------------


def test_add_company_appends_to_yaml(client, temp_env, monkeypatch):
    path = _point_at_temp_yaml(monkeypatch, temp_env)

    response = client.post(
        "/api/companies", json={"name": "NewCo", "ats": "ashby", "token": "newco", "notes": "found by Scout"}
    )

    assert response.status_code == 201
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert {"name": "NewCo", "ats": "ashby", "token": "newco", "notes": "found by Scout"} in entries


def test_add_company_survives_a_name_with_yaml_metacharacters(client, temp_env, monkeypatch):
    """Built via safe_dump over the parsed list, not string concatenation -
    a colon in a company name must not produce an unparseable file."""
    path = _point_at_temp_yaml(monkeypatch, temp_env)

    client.post("/api/companies", json={"name": "Acme: The Sequel", "ats": "lever", "token": "acme2"})

    entries = yaml.safe_load(path.read_text(encoding="utf-8"))  # would raise if malformed
    assert any(e["name"] == "Acme: The Sequel" for e in entries)


def test_add_company_rejects_a_duplicate(client, temp_env, monkeypatch):
    _point_at_temp_yaml(monkeypatch, temp_env)

    response = client.post("/api/companies", json={"name": "acme corp", "ats": "greenhouse", "token": "acme"})

    assert response.status_code == 409


def test_add_company_rejects_an_unknown_ats(client, temp_env, monkeypatch):
    _point_at_temp_yaml(monkeypatch, temp_env)

    response = client.post("/api/companies", json={"name": "X", "ats": "not_a_real_ats", "token": "x"})

    assert response.status_code == 422


def test_add_company_rejects_workday_with_a_clear_reason(client, temp_env, monkeypatch):
    """workday is a real ATSSource now, so it must not fall through to the
    generic "unknown ats" branch - and it must not 500, either. This
    endpoint has no fields to collect workday_tenant/workday_wd/
    workday_site, so it's explicitly rejected with a 422 explaining why,
    same status as the generic case but a specific message."""
    _point_at_temp_yaml(monkeypatch, temp_env)

    response = client.post("/api/companies", json={"name": "X", "ats": "workday", "token": "x"})

    assert response.status_code == 422
    assert "workday" in response.json()["detail"].lower()


def test_add_company_rejects_an_empty_name(client, temp_env, monkeypatch):
    _point_at_temp_yaml(monkeypatch, temp_env)

    assert client.post("/api/companies", json={"name": "  ", "ats": "ashby", "token": "x"}).status_code == 422
