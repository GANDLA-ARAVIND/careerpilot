from config import GEMINI_MODEL_STAGE1, GEMINI_MODEL_STAGE2
from tests.conftest import add_analyst_result


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /api/jobs
# ---------------------------------------------------------------------------


def test_list_jobs_empty_database(client):
    response = client.get("/api/jobs")
    assert response.status_code == 200
    assert response.json() == []


def test_list_jobs_returns_only_analyzed_survivors(client, seeded, temp_env):
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1, fit_score=80)

    body = client.get("/api/jobs").json()

    assert [j["company"] for j in body] == ["Acme Corp"]
    assert body[0]["fit_score"] == 80
    assert body[0]["matched_skills"] == ["Python"]


def test_list_jobs_excludes_rule_filter_rejects(client, seeded, temp_env):
    """The rejected job is a real row with a real score - it must still not
    appear, because it never survived rule filtering."""
    add_analyst_result(temp_env["engine"], seeded["rejected"], model=GEMINI_MODEL_STAGE1, fit_score=95)

    body = client.get("/api/jobs").json()

    assert all(j["company"] != "Initech" for j in body)


def test_list_jobs_sorted_by_fit_score_descending(client, seeded, temp_env):
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1, fit_score=40)
    add_analyst_result(temp_env["engine"], seeded["survivor_b"], model=GEMINI_MODEL_STAGE1, fit_score=90)

    scores = [j["fit_score"] for j in client.get("/api/jobs").json()]

    assert scores == sorted(scores, reverse=True)


def test_list_jobs_prefers_stage2_over_stage1(client, seeded, temp_env):
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1, fit_score=40)
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE2, fit_score=88)

    body = client.get("/api/jobs").json()

    assert body[0]["fit_score"] == 88
    assert body[0]["model"] == GEMINI_MODEL_STAGE2


def test_list_jobs_unscored_has_null_fit_score_not_zero(client, seeded, temp_env):
    """A posting with no concrete requirements must serve fit_score=null.
    A 0 would read as "measured, terrible" when the truth is "no
    comparison was possible"."""
    add_analyst_result(
        temp_env["engine"],
        seeded["survivor_a"],
        model=GEMINI_MODEL_STAGE1,
        fit_score=50,
        matched=[],
        missing=[],
        verdict="unscored",
    )

    body = client.get("/api/jobs").json()

    assert body[0]["fit_score"] is None
    assert body[0]["is_unscored"] is True


def test_list_jobs_can_exclude_unscored(client, seeded, temp_env):
    add_analyst_result(
        temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1, matched=[], missing=[], verdict="unscored"
    )

    assert client.get("/api/jobs", params={"include_unscored": False}).json() == []


def test_list_jobs_filters_by_status(client, seeded, temp_env):
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1)
    add_analyst_result(temp_env["engine"], seeded["survivor_b"], model=GEMINI_MODEL_STAGE1)
    client.post(f"/api/jobs/{seeded['survivor_a'].content_hash}/status", json={"status": "applied"})

    body = client.get("/api/jobs", params={"status": "applied"}).json()

    assert [j["company"] for j in body] == ["Acme Corp"]


# ---------------------------------------------------------------------------
# GET /api/jobs/{content_hash}
# ---------------------------------------------------------------------------


def test_get_job_detail_includes_description(client, seeded, temp_env):
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1)

    body = client.get(f"/api/jobs/{seeded['survivor_a'].content_hash}").json()

    assert "Requirements" in body["description"]
    assert body["content_hash"] == seeded["survivor_a"].content_hash


def test_get_job_detail_unknown_hash_404s(client):
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_get_job_detail_for_unanalyzed_job_has_null_score(client, seeded):
    """A stored-but-never-analyzed job is served with its real data and
    nulls where a judgment would be - never a fabricated score."""
    body = client.get(f"/api/jobs/{seeded['survivor_a'].content_hash}").json()

    assert body["fit_score"] is None
    assert body["verdict"] == "unanalyzed"
    assert body["description"]


def test_rejected_route_is_not_captured_as_a_content_hash(client, seeded):
    """/api/jobs/rejected must reach the rejected endpoint, not be parsed
    as a content_hash - route declaration order guarantees this."""
    response = client.get("/api/jobs/rejected")

    assert response.status_code == 200
    assert "items" in response.json()


# ---------------------------------------------------------------------------
# POST /api/jobs/{content_hash}/status
# ---------------------------------------------------------------------------


def test_set_status_applied_records_applied_at(client, seeded):
    content_hash = seeded["survivor_a"].content_hash

    body = client.post(f"/api/jobs/{content_hash}/status", json={"status": "applied"}).json()

    assert body["application_status"] == "applied"
    assert body["applied_at"] is not None


def test_set_status_non_applied_leaves_applied_at_null(client, seeded):
    content_hash = seeded["survivor_a"].content_hash

    body = client.post(f"/api/jobs/{content_hash}/status", json={"status": "interviewing"}).json()

    assert body["applied_at"] is None


def test_set_status_away_from_applied_keeps_applied_at(client, seeded):
    content_hash = seeded["survivor_a"].content_hash
    client.post(f"/api/jobs/{content_hash}/status", json={"status": "applied"})

    body = client.post(f"/api/jobs/{content_hash}/status", json={"status": "rejected"}).json()

    assert body["application_status"] == "rejected"
    assert body["applied_at"] is not None  # applying is history, not a toggle


def test_set_status_rejects_unknown_status(client, seeded):
    content_hash = seeded["survivor_a"].content_hash

    assert client.post(f"/api/jobs/{content_hash}/status", json={"status": "hired"}).status_code == 422


def test_set_status_unknown_hash_404s(client):
    assert client.post("/api/jobs/nope/status", json={"status": "applied"}).status_code == 404


# ---------------------------------------------------------------------------
# GET /api/jobs/rejected
# ---------------------------------------------------------------------------


def test_rejected_lists_rule_filter_rejects(client, seeded):
    body = client.get("/api/jobs/rejected").json()

    assert body["total"] >= 1
    assert any(item["company"] == "Initech" for item in body["items"])
    assert "seniority" in body["rules"]


def test_rejected_filterable_by_rule(client, seeded):
    body = client.get("/api/jobs/rejected", params={"rule": "seniority"}).json()

    assert all(item["reason"] == "seniority" for item in body["items"])


def test_rejected_pagination(client, seeded):
    body = client.get("/api/jobs/rejected", params={"page": 1, "page_size": 1}).json()

    assert len(body["items"]) <= 1
    assert body["page"] == 1
    assert body["page_size"] == 1


def test_rejected_unknown_rule_returns_empty_not_error(client, seeded):
    body = client.get("/api/jobs/rejected", params={"rule": "no_such_rule"}).json()

    assert body["items"] == []
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# GET /api/stats
# ---------------------------------------------------------------------------


def test_stats_reports_the_funnel(client, seeded, temp_env):
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1)

    body = client.get("/api/stats").json()

    assert body["total_fetched"] == 3
    assert body["survived"] == 2
    assert body["analyzed"] == 1
    assert body["pending"] == 1  # survivor_b has no analyst result yet
    assert body["rejected_by"]["seniority"] == 1


def test_stats_on_empty_database(client):
    body = client.get("/api/stats").json()

    assert body["total_fetched"] == 0
    assert body["survived"] == 0
    assert body["rejected_by"] == {}


# ---------------------------------------------------------------------------
# Unscored jobs that meet a stated experience requirement lead the list.
# They previously sat below every scored job, which buried a real 0-years
# match at the bottom. See app.partition_unscored_by_experience.
# ---------------------------------------------------------------------------


def _dashboard_job(content_hash, *, fit, unscored, years, meets):
    from datetime import datetime

    from api.services.dashboard import DashboardJob
    from tests.conftest import make_posting

    job = make_posting(source_job_id=content_hash, title=f"Engineer {content_hash}")
    return DashboardJob(
        job=job,
        content_hash=content_hash,
        fit_score=fit,
        verdict="unscored" if unscored else "possible",
        matched_skills=[] if unscored else ["Python"],
        missing_skills=[],
        years_required=years,
        resume_meets_it=meets,
        reasoning="",
        model="m",
        application_status="new",
        first_seen=datetime(2026, 8, 1),
        is_new=False,
        is_unscored=unscored,
    )


def test_unscored_job_meeting_a_stated_requirement_is_promoted_above_scored_jobs():
    from api.services.dashboard import partition_unscored_by_experience

    qualifies = _dashboard_job("q", fit=None, unscored=True, years=0.0, meets=True)
    no_figure = _dashboard_job("n", fit=None, unscored=True, years=None, meets=True)
    not_met = _dashboard_job("x", fit=None, unscored=True, years=4.0, meets=False)

    promoted, rest = partition_unscored_by_experience([no_figure, not_met, qualifies])

    assert [dj.content_hash for dj in promoted] == ["q"]
    assert {dj.content_hash for dj in rest} == {"n", "x"}


def test_promoted_unscored_jobs_are_ordered_by_the_lowest_requirement_first():
    from api.services.dashboard import partition_unscored_by_experience

    three = _dashboard_job("three", fit=None, unscored=True, years=3.0, meets=True)
    zero = _dashboard_job("zero", fit=None, unscored=True, years=0.0, meets=True)

    promoted, _ = partition_unscored_by_experience([three, zero])

    assert [dj.content_hash for dj in promoted] == ["zero", "three"]


def test_a_job_with_no_stated_requirement_is_never_promoted():
    """years_required None means 'not stated', never zero - reading it as a
    met requirement would promote jobs on absent evidence."""
    from api.services.dashboard import partition_unscored_by_experience

    promoted, rest = partition_unscored_by_experience(
        [_dashboard_job("n", fit=None, unscored=True, years=None, meets=True)]
    )

    assert promoted == []
    assert len(rest) == 1


def test_include_unscored_false_still_drops_promoted_jobs(client, seeded):
    """Promoted jobs are still unscored jobs - the flag means what it says."""
    body = client.get("/api/jobs?include_unscored=false").json()

    assert all(not j["is_unscored"] for j in body)
