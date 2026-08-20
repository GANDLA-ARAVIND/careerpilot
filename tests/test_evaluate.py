import pytest
from sqlalchemy.orm import Session

from db import JobPostingRow, get_engine, upsert_job
from evaluate import (
    FilterError,
    Label,
    expected_mrr_random,
    expected_recall_at_k_random,
    find_filter_errors,
    match_labels_to_db,
    mean_reciprocal_rank,
    rank_positions,
    recall_at_k,
)
from models import ATSSource, JobPosting


def _make_posting(**overrides):
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        location="Bangalore, Karnataka",
        description="We need a backend engineer with Python experience.",
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# rank_positions
# ---------------------------------------------------------------------------


def test_rank_positions_are_one_indexed_in_ranked_order():
    a = _make_posting(source_job_id="1", title="A")
    b = _make_posting(source_job_id="2", title="B")
    c = _make_posting(source_job_id="3", title="C")

    positions = rank_positions([(a, 0.9), (b, 0.5), (c, 0.1)])

    assert positions == {a.content_hash: 1, b.content_hash: 2, c.content_hash: 3}


# ---------------------------------------------------------------------------
# metrics - pure math, no model needed
# ---------------------------------------------------------------------------


def test_mean_reciprocal_rank():
    assert mean_reciprocal_rank([1, 2, 4]) == pytest.approx((1 + 0.5 + 0.25) / 3)


def test_recall_at_k():
    positions = [1, 5, 15]
    assert recall_at_k(positions, k=10, total_relevant=3) == pytest.approx(2 / 3)
    assert recall_at_k(positions, k=20, total_relevant=3) == pytest.approx(3 / 3)
    assert recall_at_k(positions, k=1, total_relevant=3) == pytest.approx(1 / 3)


def test_expected_recall_at_k_random_is_k_over_n():
    assert expected_recall_at_k_random(k=10, n=20) == pytest.approx(0.5)


def test_expected_recall_at_k_random_caps_at_one_when_k_exceeds_n():
    assert expected_recall_at_k_random(k=30, n=20) == pytest.approx(1.0)


def test_expected_mrr_random_matches_manual_harmonic_calc():
    # H_3 = 1 + 1/2 + 1/3 = 1.8333...; expected MRR = H_3 / 3
    assert expected_mrr_random(3) == pytest.approx((1 + 1 / 2 + 1 / 3) / 3)


def test_expected_mrr_random_decreases_as_pool_grows():
    """A 'good' job's expected reciprocal rank should get worse (lower) in a
    bigger random pool - sanity check on the direction of the formula."""
    assert expected_mrr_random(100) < expected_mrr_random(10)


# ---------------------------------------------------------------------------
# match_labels_to_db - found / missing, exact content_hash match
# ---------------------------------------------------------------------------


def test_match_found_by_exact_hash(session):
    job = _make_posting(title="Backend Engineer")
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    label = Label(content_hash=job.content_hash, company="Acme", title="Backend Engineer", label="good")
    found, missing = match_labels_to_db(session, [label])

    assert not missing
    assert label.content_hash in found
    assert found[label.content_hash].content_hash == job.content_hash


def test_match_reports_missing_when_no_row_matches(session):
    label = Label(content_hash="0" * 64, company="Nowhere Corp", title="Ghost Role", label="good")
    found, missing = match_labels_to_db(session, [label])

    assert found == {}
    assert missing == [label]


# ---------------------------------------------------------------------------
# find_filter_errors - cross-checking human labels against the filter's verdict
# ---------------------------------------------------------------------------


def test_false_reject_when_labeled_good_but_filter_rejected(session):
    job = _make_posting(title="Backend Engineer")
    upsert_job(session, job, rejection_rule="not_allowlisted", experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    found = {job.content_hash: row}
    label = Label(content_hash=job.content_hash, company="Acme", title="Backend Engineer", label="good")

    false_rejects, false_accepts = find_filter_errors(found, [label])

    assert false_rejects == [FilterError(label, "Acme", "Backend Engineer", "not_allowlisted")]
    assert false_accepts == []


def test_false_accept_when_labeled_no_but_filter_passed(session):
    job = _make_posting(title="Backend Engineer")
    upsert_job(session, job, rejection_rule=None, experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    found = {job.content_hash: row}
    label = Label(content_hash=job.content_hash, company="Acme", title="Backend Engineer", label="no")

    false_rejects, false_accepts = find_filter_errors(found, [label])

    assert false_accepts == [FilterError(label, "Acme", "Backend Engineer", None)]
    assert false_rejects == []


def test_filter_errors_survive_the_db_session_closing(session, tmp_path):
    """Regression test for a real bug: find_filter_errors used to hand back
    the live JobPostingRow ORM object. rank_jobs' embedding cache commits
    mid-session, which expires tracked attributes, and by the time the
    caller prints filter errors after the session block has exited, reading
    row.company raised DetachedInstanceError. find_filter_errors must copy
    the fields out into a plain FilterError instead of leaking the ORM row."""
    job = _make_posting(title="Backend Engineer")
    upsert_job(session, job, rejection_rule="not_allowlisted", experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    found = {job.content_hash: row}
    label = Label(content_hash=job.content_hash, company="Acme", title="Backend Engineer", label="good")

    false_rejects, _ = find_filter_errors(found, [label])

    session.close()  # simulates the `with Session(...)` block having exited

    # must not raise DetachedInstanceError - these are plain values now, not
    # live attribute access on a detached ORM instance
    error = false_rejects[0]
    assert error.company == "Acme"
    assert error.title == "Backend Engineer"
    assert error.rejection_rule == "not_allowlisted"


def test_no_filter_error_when_label_agrees_with_filter(session):
    passed_job = _make_posting(source_job_id="1", title="A")
    rejected_job = _make_posting(source_job_id="2", title="B")
    upsert_job(session, passed_job, rejection_rule=None, experience_years_required=None)
    upsert_job(session, rejected_job, rejection_rule="seniority", experience_years_required=None)
    session.commit()

    found = {
        passed_job.content_hash: session.get(JobPostingRow, passed_job.content_hash),
        rejected_job.content_hash: session.get(JobPostingRow, rejected_job.content_hash),
    }
    labels = [
        Label(content_hash=passed_job.content_hash, company="Acme", title="A", label="good"),  # passed, labeled good - agrees
        Label(content_hash=rejected_job.content_hash, company="Acme", title="B", label="no"),  # rejected, labeled no - agrees
    ]

    false_rejects, false_accepts = find_filter_errors(found, labels)

    assert false_rejects == []
    assert false_accepts == []


def test_weak_label_on_rejected_job_also_counts_as_false_reject(session):
    """"weak" still means the human thought it was a real candidate role -
    a filter rejection on a weak-labeled job is still worth surfacing."""
    job = _make_posting(title="Backend Engineer")
    upsert_job(session, job, rejection_rule="not_india", experience_years_required=None)
    session.commit()

    row = session.get(JobPostingRow, job.content_hash)
    found = {job.content_hash: row}
    label = Label(content_hash=job.content_hash, company="Acme", title="Backend Engineer", label="weak")

    false_rejects, _ = find_filter_errors(found, [label])
    assert false_rejects == [FilterError(label, "Acme", "Backend Engineer", "not_india")]
