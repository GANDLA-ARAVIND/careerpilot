import csv
import random

import pytest
from sqlalchemy.orm import Session

from db import get_engine, upsert_job
from export_labels import REJECTED_SAMPLE_TARGETS, export, sample_rejected
from models import ATSSource, JobPosting


def _make_posting(**overrides):
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        location="Bangalore, Karnataka",
        description="x" * 500,
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    with Session(engine) as session:
        yield session


def _seed(session, rule_counts: dict) -> None:
    """Insert `n` rejected rows per rule (rule=None means "passed"), plus a
    unique source_job_id/title per row so each gets a distinct content_hash."""
    i = 0
    for rule, n in rule_counts.items():
        for _ in range(n):
            i += 1
            job = _make_posting(source_job_id=str(i), title=f"Title {i}")
            upsert_job(session, job, rejection_rule=rule, experience_years_required=None)
    session.commit()


def test_all_passed_jobs_included_and_rejected_sample_is_capped(session, tmp_path):
    _seed(session, {None: 5, "not_allowlisted": 200, "seniority": 200, "not_india": 5})

    out_path = tmp_path / "labels_todo.csv"
    count = export(session, path=out_path, rng=random.Random(0))

    with out_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert count == len(rows)
    assert sum(1 for r in rows if r["rejected_by"] == "") == 5  # every passed job included

    not_allowlisted_rows = sum(1 for r in rows if r["rejected_by"] == "not_allowlisted")
    seniority_rows = sum(1 for r in rows if r["rejected_by"] == "seniority")
    not_india_rows = sum(1 for r in rows if r["rejected_by"] == "not_india")

    assert not_allowlisted_rows == REJECTED_SAMPLE_TARGETS["not_allowlisted"]
    assert seniority_rows == REJECTED_SAMPLE_TARGETS["seniority"]
    assert not_india_rows == 5  # pool smaller than target (10) - takes all of it, not more


def test_sample_rejected_weights_toward_not_allowlisted_over_seniority(session):
    """The whole point of the weighting: a uniform draw across a pool
    dominated by seniority rejects would mostly return seniority rows. This
    checks the actual output composition, not just that sampling ran."""
    _seed(session, {"not_allowlisted": 500, "seniority": 500})

    sampled = sample_rejected(session, rng=random.Random(0))
    rules = [row.rejection_rule for row in sampled]

    assert rules.count("not_allowlisted") == REJECTED_SAMPLE_TARGETS["not_allowlisted"]
    assert rules.count("seniority") == REJECTED_SAMPLE_TARGETS["seniority"]
    assert rules.count("not_allowlisted") > rules.count("seniority")


def test_description_truncated_to_300_chars(session, tmp_path):
    _seed(session, {None: 1})

    out_path = tmp_path / "labels_todo.csv"
    export(session, path=out_path, rng=random.Random(0))

    with out_path.open(encoding="utf-8") as f:
        row = next(csv.DictReader(f))

    assert len(row["description_excerpt"]) == 300


def test_content_hash_is_full_length_and_label_column_blank(session, tmp_path):
    _seed(session, {None: 1})

    out_path = tmp_path / "labels_todo.csv"
    export(session, path=out_path, rng=random.Random(0))

    with out_path.open(encoding="utf-8") as f:
        row = next(csv.DictReader(f))

    assert len(row["content_hash"]) == 64  # full sha256 hex digest, not a truncated prefix
    assert row["label"] == ""


def test_passed_job_has_blank_rejected_by(session, tmp_path):
    _seed(session, {None: 1})

    out_path = tmp_path / "labels_todo.csv"
    export(session, path=out_path, rng=random.Random(0))

    with out_path.open(encoding="utf-8") as f:
        row = next(csv.DictReader(f))

    assert row["rejected_by"] == ""
