"""Workday adapter - a genuinely different shape from Greenhouse/Lever/Ashby,
not a copy of them with a new base URL. Everything below was checked live
against real tenants before being written, not assumed from documentation -
see docs/decisions.md for the discovery survey and the specific corrections
this made to the original investigation brief.

The four things that make this adapter structurally different:

1. POST for listings, GET for detail. GET on the listing endpoint returns
   HTTP 400. The listing response carries no description - every job needs
   a second request. This is a real N+1: fetching a 40-job tenant costs
   roughly 2 (paginated listing) + 40 (detail) = 42 requests, not ~1-2 like
   the other three adapters. See fetch_jobs' docstring for the measured
   count and what that implies for how often this should run.

2. `wd` (wd1, wd3, wd5, wd12, wd501, wd503, ... - all seen on real tenants)
   is per-company with no derivable pattern. It lives in companies.yaml
   next to the tenant and site (see models.CompanyConfig), found by hand,
   never guessed.

3. Pagination has three traps, all verified live, not just the first one
   the investigation flagged:
   - `limit` has a hard server-side ceiling of 20. Asking for more doesn't
     get silently capped - it's an HTTP 400. MAX_PAGE_SIZE below is not a
     tuning knob.
   - `offset` does not error past the end - it wraps back to page-1
     content. The wrap point is NOT a fixed 2000 as the original
     investigation stated; verified on a 1,461-job tenant, it wraps at
     offset ~= that tenant's own total.
   - `total` is unreliable in two separate ways, not one. Verified live: on
     a large tenant it read correctly at offset 0, then reported 0 for the
     entire middle range of genuinely distinct, still-paginating results,
     then reported the correct total again only once pagination had
     wrapped - so it's only ever read from the first response. But even
     that first-page read isn't trustworthy: verified live on a very large
     tenant, `total` came back as a suspiciously round number on every
     query tried (filtered or not) - consistent with a Workday-side display
     cap on the counter itself, not the tenant's real size. A first design
     of this adapter trusted `offset >= total` as a stop condition, which
     means a capped total would have silently truncated that tenant's
     fetch with no error at all - the worst kind of bug, caught before it
     shipped rather than after. `total` is captured from page 1 purely as
     a diagnostic value now; it is never used to decide when pagination
     ends. The only real stop conditions are a short page (fewer than
     MAX_PAGE_SIZE postings - nothing left) or a repeated first-job id
     (wrap detected directly), whichever comes first, with
     OFFSET_HARD_CAP as a circuit breaker for a tenant that manages to
     trigger neither.

4. Two distinct failure shapes on the endpoint this adapter actually calls
   (the /wday/cxs/ JSON API, not the human-facing HTML site - the original
   investigation's "500, non-JSON, response.json() throws on both" is real
   behavior, but only on that HTML surface, verified separately and not
   reachable from here): a tenant that doesn't exist returns HTTP 422 (POST)
   with a valid JSON error body; a tenant that exists but the wrong `site`
   returns HTTP 404, also valid JSON. Both are treated as BoardNotFoundError
   - both mean the companies.yaml entry is wrong - but response.json() is
   still never called unguarded, since a WAF page or network-level
   interception could still hand back HTML on either.
"""

import time
import urllib.robotparser
from datetime import datetime
from typing import Optional

import requests

from adapters.base import (
    ATSAdapterError,
    BACKOFF_BASE_SECONDS,
    BoardNotFoundError,
    MAX_ATTEMPTS,
    RobotsDisallowedError,
    TIMEOUT_SECONDS,
    infer_remote_type,
    strip_html,
)
from models import ATSSource, JobPosting

# Verified live (see module docstring, point 3) - not a tuning knob. 21+
# gets HTTP 400, not a silently-truncated page of 20.
MAX_PAGE_SIZE = 20

# Circuit breaker only - never the primary stopping condition. Real
# pagination stops on a short page (fewer than MAX_PAGE_SIZE postings) or a
# detected wrap (see module docstring, point 3); `total` is not part of the
# stop decision at all, since it can itself be capped. This cap exists so a
# tenant that somehow triggers neither a short page nor a detected wrap
# can't spin forever. Set well above any real tenant checked during
# discovery (largest seen: Micron at 2,552) without being so high a genuine
# bug loops for thousands of requests before tripping it.
OFFSET_HARD_CAP = 5000

# No published Workday-specific rate limit was found. Chosen conservatively
# for the N+1 detail-fetch loop specifically - the listing calls are few
# (one per 20 jobs) and use the same pacing for simplicity, not because
# they need it as much.
REQUEST_PACING_SECONDS = 0.3

_ROBOTS_PARSER_CACHE: dict[str, urllib.robotparser.RobotFileParser] = {}


def _robots_allow(host: str, path: str) -> bool:
    """Called once per fetch_jobs() call, before any real request - the
    cache below isn't about avoiding retries within one call (robots.txt is
    only ever fetched once per call regardless), it's about a process that
    calls fetch_jobs() more than once against the same tenant in one run
    (the test suite does this routinely) not re-fetching robots.txt every
    time. urllib.robotparser (stdlib), not a hand-rolled parser - verified
    live against a real Workday robots.txt before relying on it."""
    parser = _ROBOTS_PARSER_CACHE.get(host)
    if parser is None:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"https://{host}/robots.txt")
        try:
            parser.read()
        except Exception:  # noqa: BLE001 - an unreadable robots.txt must not block a real board; treat as allow
            return True
        _ROBOTS_PARSER_CACHE[host] = parser
    return parser.can_fetch("*", f"https://{host}{path}")


def _request_with_backoff(
    method: str, url: str, json_body: Optional[dict] = None
) -> tuple[int, Optional[dict], str]:
    """Workday-specific retry loop - not adapters.base.request_with_backoff,
    which is GET-only and treats every 404 as "return it, let the caller
    decide" (right for the other three adapters' single not-found status,
    wrong here where 400/404/422 all need to become BoardNotFoundError and
    429/5xx still need a retry). Reuses base.py's retry constants so the
    backoff policy itself doesn't drift between adapters.

    Returns (status_code, parsed_json_or_None, raw_text). Never calls
    response.json() unguarded - see module docstring, point 4."""
    last_error: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_BASE_SECONDS * 2**(attempt - 1))
        try:
            response = requests.request(
                method, url, json=json_body, timeout=TIMEOUT_SECONDS,
                headers={"Accept": "application/json"},
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            continue

        if response.status_code == 429 or response.status_code >= 500:
            last_error = requests.HTTPError(f"{response.status_code} from {url}")
            continue

        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        return response.status_code, parsed, response.text

    raise ATSAdapterError(f"Request to {url} failed after {MAX_ATTEMPTS} attempts: {last_error}") from last_error


def _normalize_location(raw: Optional[str]) -> Optional[str]:
    """Workday's location string format is set per TENANT, not fixed
    Workday-wide - verified live: one tenant gives "Bengaluru, India"
    (comma-delimited, city-first - already models.py's expected shape),
    another gives "India - Bangalore" (dash-delimited, country-first) for
    the same kind of posting. models.normalize_location() assumes
    comma-delimited city-first (its own documented limitation) and would
    read the second shape's whole string as one "city" token. Only a plain
    two-part "X - Y" with no comma already present is flipped; anything
    else (already comma-shaped, or a more complex multi-location string)
    passes through unchanged rather than being guessed at."""
    if not raw or "," in raw or " - " not in raw:
        return raw
    parts = [p.strip() for p in raw.split(" - ")]
    if len(parts) == 2:
        country, city = parts
        return f"{city}, {country}"
    return raw


def _parse_posted_date(start_date: Optional[str]) -> Optional[datetime]:
    if not start_date:
        return None
    try:
        return datetime.fromisoformat(start_date)
    except ValueError:
        return None


def _fetch_listing_page(
    company: str, host: str, tenant: str, site: str, offset: int
) -> tuple[list[dict], Optional[int]]:
    """One page of the listing endpoint. Returns (postings, total) - total
    is only meaningful when offset == 0 (see module docstring, point 3);
    callers at any other offset should ignore it. Even at offset == 0 it's
    diagnostic only, not authoritative - see fetch_jobs, which never uses
    it to decide when pagination ends."""
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    status, payload, text = _request_with_backoff(
        "POST", url, {"appliedFacets": {}, "limit": MAX_PAGE_SIZE, "offset": offset, "searchText": ""}
    )

    if status in (400, 404, 422):
        raise BoardNotFoundError(source=ATSSource.WORKDAY.value, company=company, board_token=f"{tenant}/{site}")
    if status != 200 or payload is None:
        raise ATSAdapterError(f"workday: unexpected response from {url} (HTTP {status}): {text[:200]!r}")

    return payload.get("jobPostings", []), payload.get("total")


def _fetch_job_detail(company: str, host: str, tenant: str, site: str, external_path: str) -> Optional[dict]:
    """GET for one job's description + startDate. Returns None (not raised)
    on a per-job failure after retries are exhausted - one bad job in a
    40-job tenant should not lose the other 39, the same "keep going"
    policy pipeline.py's fetch_all already applies at the company level."""
    url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    try:
        status, payload, text = _request_with_backoff("GET", url)
    except ATSAdapterError:
        return None
    if status != 200 or payload is None:
        return None
    return payload.get("jobPostingInfo")


def _to_job_posting(company: str, tenant: str, site: str, detail: dict) -> Optional[JobPosting]:
    description = strip_html(detail.get("jobDescription") or "")
    if not description:
        return None

    location = _normalize_location(detail.get("location"))
    external_url = detail.get("externalUrl") or ""

    return JobPosting(
        source=ATSSource.WORKDAY,
        # jobReqId ("JR101126"), not `id` (an opaque hash) - jobReqId is the
        # human-facing requisition number Workday itself surfaces, matching
        # the other adapters' use of the ATS's own stable posting id.
        source_job_id=detail.get("jobReqId") or detail.get("id", ""),
        company=company,
        title=detail.get("title", ""),
        location=location,
        # `remote` was seen null on every real posting checked across two
        # tenants, including postings with a clearly non-remote city
        # location - unreliable in practice, same conclusion this project
        # already reached for Ashby's isRemote. Location-text inference
        # only, not a structured-field lookup - there is no structured
        # value here worth trusting.
        remote_type=infer_remote_type(location),
        description=description,
        url=external_url,
        posted_at=_parse_posted_date(detail.get("startDate")),
    )


def fetch_jobs(company: str, tenant: str, wd: str, site: str) -> list[JobPosting]:
    """Fetch every open posting for one Workday tenant.

    Request cost is real and roughly linear: 1 listing request per 20 jobs,
    plus exactly 1 detail request per job (the N+1 - see module docstring).
    A 40-job tenant costs about 2 + 40 = 42 requests; a 800-job tenant
    (several exist among the discovered candidates) costs roughly 840.
    Measured, not estimated - see docs/decisions.md for real per-company
    counts. This is why Workday tenants should be fetched on demand or on
    a slower cadence than the nightly Greenhouse/Lever/Ashby run, not
    folded into it unconditionally as company count grows.

    robots.txt is checked once, before any real request - a disallowed
    tenant is never queried at all, not queried-then-discarded."""
    host = f"{tenant}.{wd}.myworkdayjobs.com"
    listing_path = f"/wday/cxs/{tenant}/{site}/jobs"

    if not _robots_allow(host, listing_path):
        raise RobotsDisallowedError(source=ATSSource.WORKDAY.value, company=company, url=f"https://{host}{listing_path}")

    all_postings: list[dict] = []
    first_id: Optional[str] = None
    offset = 0

    while offset < OFFSET_HARD_CAP:
        if offset > 0:
            time.sleep(REQUEST_PACING_SECONDS)
        page, _page_total = _fetch_listing_page(company, host, tenant, site, offset)
        # _page_total (only meaningful at offset == 0) is deliberately
        # unused for the stop decision - see module docstring, point 3. A
        # first version of this loop broke on `offset >= total`, which
        # silently truncated any tenant whose reported total was capped
        # below its real size. The only real stop conditions are a short
        # page and a detected wrap, both below.
        if not page:
            break

        page_first_id = page[0].get("externalPath")
        if offset > 0 and page_first_id == first_id:
            break  # wrapped back to page 1 - see module docstring, point 3
        if offset == 0:
            first_id = page_first_id

        all_postings.extend(page)
        offset += len(page)
        if len(page) < MAX_PAGE_SIZE:
            break  # short page - nothing left, the real end

    jobs: list[JobPosting] = []
    for posting in all_postings:
        external_path = posting.get("externalPath")
        if not external_path:
            continue
        time.sleep(REQUEST_PACING_SECONDS)
        detail = _fetch_job_detail(company, host, tenant, site, external_path)
        if detail is None:
            continue
        job = _to_job_posting(company, tenant, site, detail)
        if job is not None:
            jobs.append(job)

    return jobs


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 5:
        print("usage: python -m adapters.workday <company_name> <tenant> <wd> <site>")
        raise SystemExit(1)

    company_arg, tenant_arg, wd_arg, site_arg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    jobs = fetch_jobs(company_arg, tenant_arg, wd_arg, site_arg)
    print(f"{len(jobs)} jobs fetched for {company_arg} ({tenant_arg}.{wd_arg}/{site_arg})")
    for job in jobs[:5]:
        print(f"- [{job.remote_type.value}] {job.title} - {job.location} - {job.url}")
