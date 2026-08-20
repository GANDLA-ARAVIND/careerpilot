from datetime import datetime
from typing import Optional

from adapters.base import BoardNotFoundError, infer_remote_type, request_with_backoff, strip_html
from models import ATSSource, JobPosting

BASE_URL = "https://boards-api.greenhouse.io/v1/boards"


def _parse_job(raw: dict, company: str) -> Optional[JobPosting]:
    description = strip_html(raw.get("content") or "")
    if not description:
        return None

    location = (raw.get("location") or {}).get("name") or None
    posted_at = datetime.fromisoformat(raw["first_published"]) if raw.get("first_published") else None

    return JobPosting(
        source=ATSSource.GREENHOUSE,
        source_job_id=str(raw["id"]),
        company=company,
        title=raw["title"],
        location=location,
        remote_type=infer_remote_type(location),
        description=description,
        url=raw["absolute_url"],
        posted_at=posted_at,
    )


def fetch_jobs(company: str, board_token: str) -> list[JobPosting]:
    """Fetch all jobs for a Greenhouse board and return normalized JobPostings.
    Postings whose description is empty after stripping HTML are dropped."""
    url = f"{BASE_URL}/{board_token}/jobs"
    response = request_with_backoff(url, params={"content": "true"})

    if response.status_code == 404:
        raise BoardNotFoundError(source=ATSSource.GREENHOUSE.value, company=company, board_token=board_token)

    payload = response.json()
    postings = [_parse_job(raw, company) for raw in payload.get("jobs", [])]
    return [posting for posting in postings if posting is not None]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: python -m adapters.greenhouse <company_name> <board_token>")
        raise SystemExit(1)

    company_arg, token_arg = sys.argv[1], sys.argv[2]
    jobs = fetch_jobs(company_arg, token_arg)
    print(f"{len(jobs)} jobs fetched for {company_arg} ({token_arg})")
    for job in jobs[:5]:
        print(f"- [{job.remote_type.value}] {job.title} - {job.location} - {job.url}")
