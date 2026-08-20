from datetime import datetime
from typing import Optional

from adapters.base import BoardNotFoundError, request_with_backoff, resolve_remote_type
from models import ATSSource, JobPosting, RemoteType

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"

# Ashby capitalizes workplaceType ("OnSite", "Hybrid", "Remote"); resolve_remote_type
# lowercases before lookup so this only needs lowercase keys, not a per-source branch.
_WORKPLACE_TYPE_MAP = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "onsite": RemoteType.ONSITE,
}


def _parse_job(raw: dict, company: str) -> Optional[JobPosting]:
    # descriptionPlain is already plain text (verified against live boards, no
    # tags survive), unlike Greenhouse's HTML blob - no strip_html needed here.
    description = (raw.get("descriptionPlain") or "").strip()
    if not description:
        return None

    location = raw.get("location") or None
    published_at = raw.get("publishedAt")
    posted_at = datetime.fromisoformat(published_at) if published_at else None

    return JobPosting(
        source=ATSSource.ASHBY,
        source_job_id=str(raw["id"]),
        company=company,
        title=raw["title"],
        location=location,
        remote_type=resolve_remote_type(raw.get("workplaceType"), _WORKPLACE_TYPE_MAP, location),
        description=description,
        url=raw["jobUrl"],
        posted_at=posted_at,
    )


def fetch_jobs(company: str, board_token: str) -> list[JobPosting]:
    """Fetch all postings for an Ashby job board and return normalized JobPostings.
    Postings whose description is empty are dropped. isListed and isRemote from
    the raw payload are intentionally unused: isListed was true for every job
    observed on the public endpoint, and isRemote conflates hybrid into remote
    (and was seen null even when workplaceType was set) so it adds nothing over
    workplaceType."""
    url = f"{BASE_URL}/{board_token}"
    response = request_with_backoff(url, params={})

    if response.status_code == 404:
        raise BoardNotFoundError(source=ATSSource.ASHBY.value, company=company, board_token=board_token)

    payload = response.json()
    postings = [_parse_job(raw, company) for raw in payload.get("jobs", [])]
    return [posting for posting in postings if posting is not None]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: python -m adapters.ashby <company_name> <board_token>")
        raise SystemExit(1)

    company_arg, token_arg = sys.argv[1], sys.argv[2]
    jobs = fetch_jobs(company_arg, token_arg)
    print(f"{len(jobs)} jobs fetched for {company_arg} ({token_arg})")
    for job in jobs[:5]:
        print(f"- [{job.remote_type.value}] {job.title} - {job.location} - {job.url}")
