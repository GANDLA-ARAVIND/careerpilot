from datetime import datetime, timezone
from typing import Optional

from adapters.base import BoardNotFoundError, request_with_backoff, resolve_remote_type, strip_html
from models import ATSSource, JobPosting, RemoteType

BASE_URL = "https://api.lever.co/v0/postings"

# Lever's own docs say "on-site"; live data (Palantir, WeRide) actually returns
# "onsite". Mapping both in case it varies by account/API version.
_WORKPLACE_TYPE_MAP = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "onsite": RemoteType.ONSITE,
    "on-site": RemoteType.ONSITE,
    "unspecified": RemoteType.UNKNOWN,
}


def _assemble_description(raw: dict) -> str:
    """descriptionPlain already combines opening+body per Lever's docs, so it
    isn't reassembled from the separate opening/descriptionBody fields (that
    would duplicate text). lists is the one part with no plain twin - it's
    where responsibilities/requirements actually live - so it still needs
    strip_html, with each list's heading kept so structure isn't lost."""
    parts = [raw.get("descriptionPlain") or ""]
    for item in raw.get("lists") or []:
        heading = item.get("text") or ""
        body = strip_html(item.get("content") or "")
        if body:
            parts.append(f"{heading}: {body}" if heading else body)
    parts.append(raw.get("additionalPlain") or "")
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _parse_job(raw: dict, company: str) -> Optional[JobPosting]:
    description = _assemble_description(raw)
    if not description:
        return None

    location = (raw.get("categories") or {}).get("location") or None
    created_at_ms = raw.get("createdAt")
    posted_at = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc) if created_at_ms else None

    return JobPosting(
        source=ATSSource.LEVER,
        source_job_id=str(raw["id"]),
        company=company,
        title=raw["text"],
        location=location,
        remote_type=resolve_remote_type(raw.get("workplaceType"), _WORKPLACE_TYPE_MAP, location),
        description=description,
        url=raw["hostedUrl"],
        posted_at=posted_at,
    )


def fetch_jobs(company: str, board_token: str) -> list[JobPosting]:
    """Fetch all postings for a Lever site and return normalized JobPostings.
    Postings whose assembled description is empty are dropped."""
    url = f"{BASE_URL}/{board_token}"
    response = request_with_backoff(url, params={"mode": "json"})

    if response.status_code == 404:
        raise BoardNotFoundError(source=ATSSource.LEVER.value, company=company, board_token=board_token)

    payload = response.json()
    postings = [_parse_job(raw, company) for raw in payload]
    return [posting for posting in postings if posting is not None]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: python -m adapters.lever <company_name> <board_token>")
        raise SystemExit(1)

    company_arg, token_arg = sys.argv[1], sys.argv[2]
    jobs = fetch_jobs(company_arg, token_arg)
    print(f"{len(jobs)} jobs fetched for {company_arg} ({token_arg})")
    for job in jobs[:5]:
        print(f"- [{job.remote_type.value}] {job.title} - {job.location} - {job.url}")
