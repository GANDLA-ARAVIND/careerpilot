"""Shared fixtures for the API tests.

Every API test runs against a temp SQLite database and temp
resume/preferences/companies files - never the real data/careerpilot.db,
data/resume.txt, data/preferences.json or companies.yaml. A test suite that
mutates the user's actual job archive or overwrites their resume would be
worse than no test suite.

No test here starts the real orchestrator, calls Gemini, or hits an ATS
API. The run tests inject a fake runner (see fake_runner); the endpoints
that would spend LLM quota (POST /api/coach/ask, POST /api/companies/scout)
are tested with their agent function monkeypatched.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import api.deps as deps
import app as app_module
import config as config_module
from api.services.run_manager import run_manager
from db import get_engine, upsert_job
from models import ATSSource, JobPosting


def make_posting(**overrides) -> JobPosting:
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="1",
        company="Acme Corp",
        title="Software Engineer I",
        location="Bangalore, Karnataka",
        description="Requirements: Python, FastAPI, SQL. 0-2 years of experience.",
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Redirects every on-disk artifact the API can touch into tmp_path.

    Patching app.RESUME_PATH only redirects readers that look the attribute
    up on the module at call time. api/routers/resume.py does exactly that
    (see its _resume_path) - an earlier version used
    `from app import RESUME_PATH`, which binds a copy at import time and
    silently ignored this patch, writing to the real data/resume.txt during
    tests. Caught by test_confirm_saves_the_text; worth knowing before
    adding any new path constant here."""
    db_path = tmp_path / "test.db"
    resume_path = tmp_path / "resume.txt"
    preferences_path = tmp_path / "preferences.json"
    companies_path = tmp_path / "companies.yaml"
    last_viewed_path = tmp_path / "last_viewed.txt"

    resume_path.write_text(
        "Technical Skills\nPython, FastAPI, SQL, React\n\nProjects\nBuilt a job pipeline.\n",
        encoding="utf-8",
    )
    companies_path.write_text(
        "- name: Acme Corp\n  ats: greenhouse\n  token: acme\n", encoding="utf-8"
    )

    monkeypatch.setattr(app_module, "RESUME_PATH", resume_path)
    monkeypatch.setattr(app_module, "LAST_VIEWED_PATH", last_viewed_path)
    monkeypatch.setattr(config_module, "PREFERENCES_PATH", preferences_path)

    # Safety net, not redundancy. Patching the path constants only helps
    # for callers that resolve them at call time; a caller that binds one
    # as a default argument (config.save_preferences does exactly this)
    # ignores the patch entirely and writes to the real file. That already
    # happened once here - a preferences test overwrote the user's real
    # data/preferences.json before the router was fixed to pass the path
    # explicitly. Wrapping the writer so it *cannot* address anything
    # outside tmp_path means the next such mistake fails loudly in the
    # test that caused it, instead of quietly editing real user data.
    real_save_preferences = config_module.save_preferences

    def guarded_save_preferences(preferences, path=preferences_path):
        resolved = Path(path)
        if tmp_path not in resolved.resolve().parents:
            raise AssertionError(
                f"test tried to write preferences outside tmp_path: {resolved} - "
                f"pass the path explicitly rather than relying on the default argument"
            )
        return real_save_preferences(preferences, resolved)

    monkeypatch.setattr(config_module, "save_preferences", guarded_save_preferences)

    # agents.analyst.prepare_resume_text reads its own module-level
    # RESUME_PATH - patched too, so cache-key derivation in
    # load_dashboard_jobs uses the temp resume rather than the real one.
    import agents.analyst as analyst_module

    monkeypatch.setattr(analyst_module, "RESUME_PATH", resume_path)

    engine = get_engine(db_path)
    deps.reset_app_engine(engine)

    yield {
        "engine": engine,
        "db_path": db_path,
        "resume_path": resume_path,
        "preferences_path": preferences_path,
        "companies_path": companies_path,
        "tmp_path": tmp_path,
    }

    deps.reset_app_engine(None)
    run_manager.reset()


@pytest.fixture
def client(temp_env):
    """TestClient wired to the temp database via a dependency override -
    scoped to this client, so it cannot leak into another test."""
    from api.main import app as fastapi_app

    engine = temp_env["engine"]

    def _override_session():
        with Session(engine) as session:
            yield session

    fastapi_app.dependency_overrides[deps.get_session] = _override_session
    with TestClient(fastapi_app) as test_client:
        test_client.temp_env = temp_env
        yield test_client
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def seeded(temp_env):
    """A few real rows: two survivors and one rule-filter reject."""
    engine = temp_env["engine"]
    jobs = {
        "survivor_a": make_posting(source_job_id="1", company="Acme Corp", title="Software Engineer I"),
        "survivor_b": make_posting(
            source_job_id="2",
            company="Globex",
            title="Backend Developer",
            description="Requirements: Go, Kubernetes. 1-3 years of experience.",
        ),
        "rejected": make_posting(
            source_job_id="3",
            company="Initech",
            title="Senior Staff Engineer",
            description="Requirements: 10+ years of experience.",
        ),
    }
    with Session(engine) as session:
        for key, job in jobs.items():
            upsert_job(
                session,
                job,
                rejection_rule="seniority" if key == "rejected" else None,
                experience_years_required=None,
            )
        session.commit()
    return jobs


def add_analyst_result(
    engine,
    job: JobPosting,
    *,
    model: str,
    fit_score: int = 70,
    matched=None,
    missing=None,
    verdict: str = "possible",
) -> str:
    """Writes an AnalystResultRow under the exact cache key
    load_dashboard_jobs will look for - recomputed the same way
    agents/analyst.py does, never guessed."""
    import json

    from agents.analyst import SYSTEM_INSTRUCTION, _text_hash, prepare_resume_text
    from db import AnalystResultRow
    from extraction import extract_jd_requirements

    resume_text = prepare_resume_text()
    requirements_text, _ = extract_jd_requirements(job.description)
    text_hash = _text_hash(model, SYSTEM_INSTRUCTION, resume_text, requirements_text)

    with Session(engine) as session:
        session.merge(
            AnalystResultRow(
                text_hash=text_hash,
                model=model,
                fit_score=fit_score,
                matched_skills=json.dumps(matched if matched is not None else ["Python"]),
                missing_skills=json.dumps(missing if missing is not None else ["Go"]),
                experience_years_required=2.0,
                resume_meets_experience=True,
                verdict=verdict,
                reasoning="Test reasoning.",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()
    return text_hash
