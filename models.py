import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator


class ATSSource(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"


class Cadence(str, Enum):
    """How often pipeline.fetch_all should actually fetch a company, not
    just how often the nightly job runs. NIGHTLY (the default - every
    existing companies.yaml entry is unaffected) means every run; WEEKLY
    means fetch_all skips it unless 7+ days have passed since its last
    successful fetch (see pipeline.WEEKLY_CADENCE_DAYS). Exists because a
    Workday tenant's request cost is enormous next to Greenhouse/Lever/
    Ashby's (see docs/decisions.md) and an enterprise board doesn't turn
    over daily enough to justify paying that cost every night."""

    NIGHTLY = "nightly"
    WEEKLY = "weekly"


class RemoteType(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Same city, different name. Covers the Indian metros most likely to show up
# across these company boards; extend as new aliases turn up in live data.
_CITY_ALIASES = {
    "bengaluru": "bangalore",
    "bombay": "mumbai",
    "calcutta": "kolkata",
    "madras": "chennai",
    "gurugram": "gurgaon",
    "cochin": "kochi",
    "trivandrum": "thiruvananthapuram",
    "mysuru": "mysore",
    "baroda": "vadodara",
    "puducherry": "pondicherry",
    "poona": "pune",
}


def normalize_location(location: Optional[str]) -> str:
    """Canonical city token for content_hash identity. The city is always the
    first comma-delimited segment across every source seen so far
    ("Hyderabad, India", "Bangalore, Karnataka, India", "Bengaluru, India"),
    so state/country are dropped by taking only that first segment, then
    running it through the same lowercase/punctuation/whitespace normalization
    as company and title. Known aliases (Bengaluru/Bangalore, Bombay/Mumbai,
    etc.) collapse to one canonical form so the same city posted under a
    different name doesn't fork into a separate identity.

    Returns "" for a missing location - jobs with no location still share one
    identity bucket per (company, title), same as before this changed.

    Does not attempt to parse compound descriptors like "Remote - Bangalore"
    or "APAC (India, Singapore)" - only the comma-delimited, city-first
    convention actually observed across Greenhouse/Lever/Ashby postings.
    """
    if not location:
        return ""
    city = _normalize(location.split(",")[0])
    return _CITY_ALIASES.get(city, city)


def compute_content_hash(company: str, title: str, location: Optional[str]) -> str:
    """Identity hash: company + title + normalized location. Location is
    included, not excluded - two postings for the same title in different
    cities are different jobs, not duplicates (an earlier version of this
    function excluded location on the theory that raw location text was too
    unnormalized to hash reliably; that theory was wrong to act on, since it
    meant "Software Engineer" in Bangalore and Hyderabad silently collapsed
    into one row). normalize_location() handles the actual unnormalized-text
    problem instead of using it as a reason to drop the field. Description is
    still excluded - JD edits must not change identity or dedupe would break
    on every minor update. Use description_hash to detect those edits
    instead."""
    key = "|".join([_normalize(company), _normalize(title), normalize_location(location)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def compute_description_hash(description: str) -> str:
    """Edit-detection signal only, never a cross-source identity signal: each
    adapter assembles `description` differently (Greenhouse: one HTML blob;
    Lever: plaintext fields plus stripped `lists` with injected headings), so
    the same job via two sources will not produce the same description_hash
    even though it produces the same content_hash. Only comparable within a
    single source's history of the same posting."""
    return hashlib.sha256(_normalize(description).encode("utf-8")).hexdigest()


class JobPosting(BaseModel):
    source: ATSSource
    source_job_id: str
    company: str
    title: str
    location: Optional[str] = None
    remote_type: RemoteType = RemoteType.UNKNOWN
    description: str  # stripped plain text; HTML stripping happens in the adapter
    url: HttpUrl
    posted_at: Optional[datetime] = None
    content_hash: str = ""
    description_hash: str = ""

    @model_validator(mode="after")
    def _compute_hashes(self) -> "JobPosting":
        self.content_hash = compute_content_hash(self.company, self.title, self.location)
        self.description_hash = compute_description_hash(self.description)
        return self


class CompanyConfig(BaseModel):
    """One companies.yaml entry. extra="forbid" so a misspelled field name
    (e.g. "toekn") fails loudly instead of being silently dropped while the
    real `token` stays unset - the whole point of typing this config.

    `token` identifies the board for Greenhouse/Lever/Ashby - one string,
    tested directly against a fixed per-source base URL. Workday has no
    equivalent single token: a tenant's full base URL is
    "{tenant}.{wd}.myworkdayjobs.com/{site}", and `wd` varies per company
    with no derivable pattern (wd1, wd3, wd5, wd12, wd501, wd503 all seen
    across real tenants - see docs/decisions.md) - it has to be looked up
    and recorded per company, the same as tenant and site. So Workday
    entries leave `token` unset and fill in workday_tenant/workday_wd/
    workday_site instead; the validator below enforces that exactly one of
    the two shapes is present, matching the ats value, so a companies.yaml
    typo (e.g. a workday entry missing workday_site) fails at load time
    (see config.load_companies) rather than surfacing as a confusing
    AttributeError deep inside fetch_all."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ats: ATSSource
    token: Optional[str] = None
    notes: Optional[str] = None
    cadence: Cadence = Cadence.NIGHTLY

    # Workday-only. All None for every other source.
    workday_tenant: Optional[str] = None
    workday_wd: Optional[str] = None
    workday_site: Optional[str] = None

    @model_validator(mode="after")
    def _validate_source_specific_fields(self) -> "CompanyConfig":
        workday_fields = {
            "workday_tenant": self.workday_tenant,
            "workday_wd": self.workday_wd,
            "workday_site": self.workday_site,
        }
        if self.ats == ATSSource.WORKDAY:
            missing = [name for name, value in workday_fields.items() if value is None]
            if missing:
                raise ValueError(
                    f"{self.name!r}: ats=workday requires {', '.join(missing)} in companies.yaml"
                )
            if self.token is not None:
                raise ValueError(
                    f"{self.name!r}: ats=workday doesn't use `token` - "
                    "set workday_tenant/workday_wd/workday_site instead"
                )
        else:
            if self.token is None:
                raise ValueError(f"{self.name!r}: ats={self.ats.value} requires `token`")
            set_fields = [name for name, value in workday_fields.items() if value is not None]
            if set_fields:
                raise ValueError(
                    f"{self.name!r}: ats={self.ats.value} doesn't use {', '.join(set_fields)} "
                    "(those are workday-only)"
                )
        return self


class Preferences(BaseModel):
    """The editable rule-filter keyword lists filters.py runs against
    (title allowlist, seniority/non-engineering rejects, India location
    keywords) - one unit of both persistence (data/preferences.json, via
    config.load_preferences/save_preferences) and preview (a candidate
    ruleset app.py's Roles tab can evaluate before saving, via an optional
    override on filters.reject_reason_for/run_filter_pass, without touching
    the live config.* values every other session or call reads).

    extra="forbid" for the same reason as CompanyConfig: a typo'd key in a
    hand-edited preferences.json (e.g. "tittle_allowlist") must fail
    validation loudly - triggering config.py's defaults fallback - rather
    than silently dropping the real title_allowlist and leaving the field
    unset."""

    model_config = ConfigDict(extra="forbid")

    title_allowlist: list[str]
    seniority_keywords: list[str]
    non_engineering_keywords: list[str]
    india_location_keywords: list[str]
