"""adapters/workday.py - the one adapter that calls requests.request
directly (not adapters.base.request_with_backoff - see the module
docstring for why), so it's the one adapter that needs its own request
mocking rather than reuse of an existing shared fixture. No test here
hits a real tenant; the real-tenant behavior this locks in was verified
live first (see docs/decisions.md) and is restated in the fakes below.

time.sleep is patched to a no-op everywhere pacing/backoff would
otherwise run real wall-clock delays (pagination pacing, the N+1 detail
loop, and retry backoff) - these tests assert on request counts and
ordering, not on timing.
"""

import urllib.robotparser

import pytest
import requests

import adapters.workday as workday
from adapters.base import ATSAdapterError, BoardNotFoundError, RobotsDisallowedError
from models import RemoteType

# Captured before any test monkeypatches workday._robots_allow (the
# robots_allow_by_default autouse fixture below replaces that name on every
# test) - the two tests that exercise the real function need this to call
# past their own fixture's patch.
_REAL_ROBOTS_ALLOW = workday._robots_allow


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(workday.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def robots_allow_by_default(monkeypatch):
    """Every test except the robots-specific ones below assumes the tenant
    is fetchable - matching the real allow-most-tenants shape found during
    discovery, so the pagination/detail tests aren't also implicitly
    testing robots.txt."""
    monkeypatch.setattr(workday, "_robots_allow", lambda host, path: True)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else ("" if payload is None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _listing_payload(postings: list[dict], total: int) -> dict:
    return {"total": total, "jobPostings": postings}


def _posting(external_path: str) -> dict:
    return {"externalPath": external_path}


def _detail_payload(**overrides) -> dict:
    defaults = dict(
        jobReqId="JR100",
        title="Software Engineer I",
        location="Bengaluru, India",
        jobDescription="<p>Build things.</p>",
        externalUrl="https://tenant.wd1.myworkdayjobs.com/en-US/site/job/JR100",
        startDate="2026-08-01",
    )
    defaults.update(overrides)
    return {"jobPostingInfo": defaults}


# ---------------------------------------------------------------------------
# _normalize_location - per-tenant format, only the plain 2-part dash shape
# gets flipped (see module docstring, point 3)
# ---------------------------------------------------------------------------


def test_normalize_location_flips_country_dash_city_to_city_comma_country():
    assert workday._normalize_location("India - Bangalore") == "Bangalore, India"


def test_normalize_location_leaves_comma_shaped_strings_unchanged():
    assert workday._normalize_location("Bengaluru, India") == "Bengaluru, India"


def test_normalize_location_leaves_a_bare_city_unchanged():
    assert workday._normalize_location("Bengaluru") == "Bengaluru"


def test_normalize_location_passes_none_through():
    assert workday._normalize_location(None) is None


def test_normalize_location_leaves_a_three_part_dash_string_unchanged():
    """Only a plain 2-part split is trusted enough to reorder - anything
    more complex is not guessed at, per the module docstring."""
    raw = "APAC - India - Bangalore"
    assert workday._normalize_location(raw) == raw


# ---------------------------------------------------------------------------
# _parse_posted_date
# ---------------------------------------------------------------------------


def test_parse_posted_date_accepts_a_real_iso_date():
    parsed = workday._parse_posted_date("2026-08-01")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 8 and parsed.day == 1


def test_parse_posted_date_returns_none_for_garbage():
    assert workday._parse_posted_date("Posted 30+ Days Ago") is None


def test_parse_posted_date_returns_none_for_none():
    assert workday._parse_posted_date(None) is None


# ---------------------------------------------------------------------------
# _to_job_posting
# ---------------------------------------------------------------------------


def test_to_job_posting_uses_jobreqid_not_the_opaque_id():
    detail = _detail_payload(jobReqId="JR900", id="ghost-opaque-hash")["jobPostingInfo"]
    job = workday._to_job_posting("Acme India", "acme", "acmecareers", detail)
    assert job.source_job_id == "JR900"


def test_to_job_posting_returns_none_when_description_is_empty():
    """A detail payload with no real description is skipped rather than
    stored as an empty-string JobPosting - matches the other adapters'
    treatment of unusable postings."""
    detail = _detail_payload(jobDescription="")["jobPostingInfo"]
    assert workday._to_job_posting("Acme India", "acme", "acmecareers", detail) is None


def test_to_job_posting_infers_remote_type_from_location_text_not_a_structured_field():
    """`remote` was seen null on every real posting checked (see module
    docstring) - this locks in that _to_job_posting never even looks for
    that field, only infer_remote_type(location)."""
    detail = _detail_payload(location="Remote - India")["jobPostingInfo"]
    job = workday._to_job_posting("Acme India", "acme", "acmecareers", detail)
    assert job.remote_type == RemoteType.REMOTE


def test_to_job_posting_normalizes_dash_location_before_storing():
    detail = _detail_payload(location="India - Hyderabad")["jobPostingInfo"]
    job = workday._to_job_posting("Acme India", "acme", "acmecareers", detail)
    assert job.location == "Hyderabad, India"


# ---------------------------------------------------------------------------
# fetch_jobs - robots.txt gate (checked once, before any real request)
# ---------------------------------------------------------------------------


def test_fetch_jobs_raises_before_any_request_when_robots_disallows(monkeypatch):
    monkeypatch.setattr(workday, "_robots_allow", lambda host, path: False)
    calls = []
    monkeypatch.setattr(workday.requests, "request", lambda *a, **k: calls.append(1) or _FakeResponse(200))

    with pytest.raises(RobotsDisallowedError):
        workday.fetch_jobs("Acme India", "acme", "wd1", "acmecareers")

    assert calls == []


def test_robots_allow_delegates_to_a_real_robotparser_and_caches_by_host(monkeypatch):
    """Not a hand-rolled check - urllib.robotparser.RobotFileParser does the
    real parsing (verified live before use, see module docstring); this
    locks in that _robots_allow just wires into it and reuses the parsed
    object for a second call against the same host."""
    read_calls = []

    class _FakeParser:
        def set_url(self, url):
            self.url = url

        def read(self):
            read_calls.append(self.url)

        def can_fetch(self, agent, url):
            return "/blocked/" not in url

    monkeypatch.setattr(urllib.robotparser, "RobotFileParser", _FakeParser)
    monkeypatch.setattr(workday, "_robots_allow", _REAL_ROBOTS_ALLOW)  # bypass this file's default-allow fixture
    workday._ROBOTS_PARSER_CACHE.clear()

    assert workday._robots_allow("tenant.wd1.myworkdayjobs.com", "/wday/cxs/tenant/site/jobs") is True
    assert workday._robots_allow("tenant.wd1.myworkdayjobs.com", "/blocked/path") is False
    assert len(read_calls) == 1  # second call reused the cached parser, no re-fetch


def test_robots_allow_treats_an_unreadable_robots_txt_as_allow(monkeypatch):
    """A tenant with no robots.txt (or a network hiccup fetching it) must
    not silently block every real board - matches the "an unreadable
    robots.txt must not block a real board" comment in the source."""

    class _ExplodingParser:
        def set_url(self, url):
            pass

        def read(self):
            raise OSError("no such host")

    monkeypatch.setattr(urllib.robotparser, "RobotFileParser", _ExplodingParser)
    monkeypatch.setattr(workday, "_robots_allow", _REAL_ROBOTS_ALLOW)  # bypass this file's default-allow fixture
    workday._ROBOTS_PARSER_CACHE.clear()

    assert workday._robots_allow("nonexistent.wd1.myworkdayjobs.com", "/wday/cxs/x/y/jobs") is True


# ---------------------------------------------------------------------------
# fetch_jobs - the two verified failure shapes on the CXS JSON API (module
# docstring, point 4) - both valid JSON, neither ever fed to response.json()
# unguarded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 422])
def test_fetch_jobs_treats_listing_failure_statuses_as_board_not_found(monkeypatch, status):
    monkeypatch.setattr(
        workday.requests, "request", lambda *a, **k: _FakeResponse(status, {"error": "not found"})
    )

    with pytest.raises(BoardNotFoundError):
        workday.fetch_jobs("Ghost Co", "ghost", "wd1", "ghostcareers")


def test_fetch_jobs_raises_ats_adapter_error_on_an_unexpected_status_without_calling_json_unguarded(monkeypatch):
    """A response.json() call that isn't guarded would throw ValueError on
    an HTML WAF page - _request_with_backoff catches that and passes
    payload=None through instead, and the listing code turns that into an
    ATSAdapterError rather than letting the raw ValueError escape."""
    monkeypatch.setattr(
        workday.requests, "request", lambda *a, **k: _FakeResponse(403, None, text="<html>blocked</html>")
    )

    with pytest.raises(ATSAdapterError):
        workday.fetch_jobs("Acme India", "acme", "wd1", "acmecareers")


# ---------------------------------------------------------------------------
# fetch_jobs - pagination: short-page stop, wrap detection, and the capped-
# total fix (total from page 1 is never trusted to decide when to stop -
# see module docstring, point 3, and docs/decisions.md for the live tenant
# that surfaced this)
# ---------------------------------------------------------------------------


def test_fetch_jobs_stops_on_a_short_page_with_no_reliable_total(monkeypatch):
    """total=None (or 0, per the "unreliable past page 1" finding) plus a
    page shorter than MAX_PAGE_SIZE is treated as the real end."""
    page = [_posting(f"/job-{i}") for i in range(3)]
    responses = iter(
        [
            _FakeResponse(200, _listing_payload(page, total=0)),
            *[_FakeResponse(200, _detail_payload(jobReqId=f"JR{i}")) for i in range(3)],
        ]
    )
    monkeypatch.setattr(workday.requests, "request", lambda *a, **k: next(responses))

    jobs = workday.fetch_jobs("Acme India", "acme", "wd1", "acmecareers")

    assert len(jobs) == 3


def test_fetch_jobs_ignores_total_and_stops_on_the_short_page_instead(monkeypatch):
    """total from page 1 is diagnostic only, never the stop condition (see
    module docstring, point 3) - two full pages then a short third page
    stops pagination on the short page, even though page 1's (wrong) total
    said the board was done after page 1 alone."""
    page1 = [_posting(f"/p1-{i}") for i in range(workday.MAX_PAGE_SIZE)]
    page2 = [_posting(f"/p2-{i}") for i in range(workday.MAX_PAGE_SIZE)]
    page3 = [_posting(f"/p3-{i}") for i in range(5)]
    listing_calls = []

    def fake_request(method, url, json=None, timeout=None, headers=None):
        if "/jobs" in url and method == "POST":
            offset = json["offset"]
            listing_calls.append(offset)
            if offset == 0:
                # Deliberately wrong: claims the board ends after page 1,
                # but two more real pages follow. A version of this loop
                # that trusted `offset >= total` would stop here and never
                # see page2/page3 - exactly the silent-truncation bug this
                # locks in as fixed.
                return _FakeResponse(200, _listing_payload(page1, total=workday.MAX_PAGE_SIZE))
            if offset == workday.MAX_PAGE_SIZE:
                return _FakeResponse(200, _listing_payload(page2, total=0))
            return _FakeResponse(200, _listing_payload(page3, total=0))
        return _FakeResponse(200, _detail_payload(jobReqId=url))

    monkeypatch.setattr(workday.requests, "request", fake_request)

    jobs = workday.fetch_jobs("Acme India", "acme", "wd1", "acmecareers")

    assert listing_calls == [0, workday.MAX_PAGE_SIZE, workday.MAX_PAGE_SIZE * 2]  # all three pages fetched
    assert len(jobs) == workday.MAX_PAGE_SIZE * 2 + 5  # nothing lost to the wrong page-1 total


def test_fetch_jobs_detects_a_wrap_back_to_page_one_and_stops(monkeypatch):
    """offset wraps back to page-1 content instead of erroring past the end
    (module docstring, point 3) - a repeated first-job externalPath at a
    later offset must stop pagination even if `total` didn't already do it."""
    page1 = [_posting(f"/p1-{i}") for i in range(workday.MAX_PAGE_SIZE)]
    listing_calls = []

    def fake_request(method, url, json=None, timeout=None, headers=None):
        if "/jobs" in url and method == "POST":
            listing_calls.append(json["offset"])
            # total lies (unreliable past page 1) and every page after the
            # first wraps straight back to page-1 content.
            return _FakeResponse(200, _listing_payload(page1, total=999999))
        return _FakeResponse(200, _detail_payload(jobReqId=url))

    monkeypatch.setattr(workday.requests, "request", fake_request)

    jobs = workday.fetch_jobs("Acme India", "acme", "wd1", "acmecareers")

    assert listing_calls == [0, workday.MAX_PAGE_SIZE]  # wrap caught on the second page, no endless loop
    assert len(jobs) == workday.MAX_PAGE_SIZE


def test_fetch_jobs_hard_stops_at_offset_hard_cap_as_a_circuit_breaker(monkeypatch):
    """A pathological tenant whose total this code never trusts and whose
    listing never repeats a first id (so wrap-detection never fires) must
    still terminate - OFFSET_HARD_CAP is the backstop, not the primary
    stop condition (module docstring, point 3)."""
    listing_calls = []

    def fake_request(method, url, json=None, timeout=None, headers=None):
        if "/jobs" in url and method == "POST":
            offset = json["offset"]
            listing_calls.append(offset)
            # Every page is full-size and unique (never repeats a first id,
            # never reports a trustworthy total) - the only thing that can
            # end this is the hard cap.
            page = [_posting(f"/o{offset}-{i}") for i in range(workday.MAX_PAGE_SIZE)]
            # total=None on every page, including page 1 - the pathological
            # case where even the one normally-trustworthy read never gives
            # this code anything to stop on.
            return _FakeResponse(200, _listing_payload(page, total=None))
        return _FakeResponse(200, _detail_payload(jobReqId=url))

    monkeypatch.setattr(workday.requests, "request", fake_request)

    workday.fetch_jobs("Runaway Co", "runaway", "wd1", "runawaycareers")

    assert max(listing_calls) < workday.OFFSET_HARD_CAP
    assert listing_calls[-1] + workday.MAX_PAGE_SIZE >= workday.OFFSET_HARD_CAP  # stopped right at the cap, not short of it


# ---------------------------------------------------------------------------
# fetch_jobs - the N+1 detail loop: pacing, per-job resilience, request count
# ---------------------------------------------------------------------------


def test_fetch_jobs_skips_a_single_bad_job_detail_without_losing_the_rest(monkeypatch):
    """One bad job in a multi-job tenant must not lose the others - the
    same "keep going" policy pipeline.py's fetch_all already applies at
    the company level, applied here one level down at the per-job level."""
    page = [_posting(f"/job-{i}") for i in range(3)]

    def fake_request(method, url, json=None, timeout=None, headers=None):
        if "/jobs" in url and method == "POST":
            return _FakeResponse(200, _listing_payload(page, total=0))
        if "/job-1" in url:
            return _FakeResponse(500)  # exhausts retries -> _fetch_job_detail returns None
        return _FakeResponse(200, _detail_payload(jobReqId=url))

    monkeypatch.setattr(workday.requests, "request", fake_request)

    jobs = workday.fetch_jobs("Acme India", "acme", "wd1", "acmecareers")

    assert len(jobs) == 2  # job-0 and job-2 survived; job-1's failure didn't take down the batch


def test_fetch_jobs_paces_between_every_request_not_just_detail_requests(monkeypatch):
    """REQUEST_PACING_SECONDS applies to the listing loop too, per the
    module docstring ("use the same pacing for simplicity") - this counts
    sleep calls rather than asserting on wall-clock time."""
    page1 = [_posting(f"/p1-{i}") for i in range(workday.MAX_PAGE_SIZE)]
    page2 = [_posting(f"/p2-{i}") for i in range(2)]
    sleep_calls = []

    def fake_request(method, url, json=None, timeout=None, headers=None):
        if "/jobs" in url and method == "POST":
            offset = json["offset"]
            if offset == 0:
                return _FakeResponse(200, _listing_payload(page1, total=workday.MAX_PAGE_SIZE + 2))
            return _FakeResponse(200, _listing_payload(page2, total=0))
        return _FakeResponse(200, _detail_payload(jobReqId=url))

    workday_requests = workday.requests
    monkeypatch.setattr(workday_requests, "request", fake_request)
    monkeypatch.setattr(workday.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    workday.fetch_jobs("Acme India", "acme", "wd1", "acmecareers")

    # 1 pacing sleep before the 2nd listing page + 1 per detail request
    # (MAX_PAGE_SIZE + 2 jobs) = MAX_PAGE_SIZE + 3. No sleep before the very
    # first request of the whole call.
    assert len(sleep_calls) == workday.MAX_PAGE_SIZE + 3
    assert all(s == workday.REQUEST_PACING_SECONDS for s in sleep_calls)


# ---------------------------------------------------------------------------
# _request_with_backoff - retries transient failures, doesn't retry a
# definitive not-found
# ---------------------------------------------------------------------------


def test_request_with_backoff_retries_5xx_then_succeeds(monkeypatch):
    responses = iter([_FakeResponse(503), _FakeResponse(200, {"ok": True})])
    monkeypatch.setattr(workday.requests, "request", lambda *a, **k: next(responses))

    status, payload, _text = workday._request_with_backoff("GET", "https://example.com/x")

    assert status == 200
    assert payload == {"ok": True}


def test_request_with_backoff_does_not_retry_a_404():
    """404 is a definitive answer (module docstring, point 4) - retrying it
    would just waste MAX_ATTEMPTS requests against a board that will never
    exist regardless of how many times it's asked."""
    calls = []

    def fake_request(method, url, json=None, timeout=None, headers=None):
        calls.append(1)
        return _FakeResponse(404, {"error": "not found"})

    import adapters.workday as wd_module

    orig = wd_module.requests.request
    wd_module.requests.request = fake_request
    try:
        status, payload, _text = wd_module._request_with_backoff("POST", "https://example.com/x")
    finally:
        wd_module.requests.request = orig

    assert status == 404
    assert len(calls) == 1


def test_request_with_backoff_raises_after_exhausting_attempts_on_repeated_connection_errors(monkeypatch):
    def always_fails(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(workday.requests, "request", always_fails)

    with pytest.raises(ATSAdapterError):
        workday._request_with_backoff("GET", "https://example.com/x")
