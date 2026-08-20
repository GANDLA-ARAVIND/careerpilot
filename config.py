import json
from pathlib import Path
from typing import Union

import yaml
from pydantic import ValidationError

from models import CompanyConfig, Preferences

# filters.py keyword lists - editable at runtime via app.py's Roles tab
# (add/remove entries, preview impact, save), persisted to
# data/preferences.json and loaded once below into the module attributes
# filters.py reads (TITLE_ALLOWLIST etc.). Deliberately NOT hardcoded
# module-level list literals any more, and filters.py deliberately does
# `import config` + `config.TITLE_ALLOWLIST` at call time rather than
# `from config import TITLE_ALLOWLIST` - the latter would bind a private
# snapshot in filters.py's own namespace at its import time, and a
# Roles-tab save rebinding config.TITLE_ALLOWLIST afterward would never be
# seen there. See apply_preferences below and filters.py's own import.
#
# DEFAULT_PREFERENCES keeps the original hand-tuned values in code, not
# just as the one-time seed for the generated file - see load_preferences
# for why: it's the fallback target whenever data/preferences.json is
# missing, malformed, or has an emptied list. An empty title_allowlist in
# particular would match nothing against TITLE_ALLOWLIST's allow-rule,
# meaning EVERY job title passes that check - filtering nothing out and
# burning a full day's LLM quota scoring unfiltered noise. That failure
# mode is silent and expensive enough that falling back to a known-safe
# default and saying so beats ever silently running with whatever's on
# disk.
PREFERENCES_PATH = Path("data/preferences.json")

DEFAULT_PREFERENCES = Preferences(
    seniority_keywords=[
        "senior",
        "lead",
        "staff",
        "principal",
        "architect",
        "manager",
        "head",
        "director",
        "vp",
        "vice president",
    ],
    non_engineering_keywords=[
        "sales",
        "business development",
        "account executive",
        "collections",
        "operations",
        "field",
        "team leader",
        "area sales",
        "marketing",
        "hr",
        "human resources",
        "finance",
        "legal",
        "customer support",
    ],
    title_allowlist=[
        "software engineer",
        "sde",
        "developer",
        "backend",
        "back end",
        "back-end",
        "frontend",
        "front end",
        "front-end",
        "full stack",
        "fullstack",
        "full-stack",
        "data engineer",
        "ml engineer",
        "machine learning engineer",
        "qa",
        "associate engineer",
        "graduate engineer",
        "trainee",
    ],
    # Matched against the raw location string anywhere it appears (city name
    # alone is enough; many postings give "Bangalore, Karnataka" with no
    # literal "India"). A bare "Remote" or "Remote - US" with no India
    # signal does not match this list and is rejected - on this roster,
    # unqualified "Remote" postings are US-only in practice (checked
    # against the real location distribution before writing this filter,
    # not guessed).
    india_location_keywords=[
        "india",
        "ind",  # some postings abbreviate the country as "IND", e.g. "Bangalore, IND"
        "bangalore",
        "bengaluru",
        "hyderabad",
        "mumbai",
        "delhi",
        "new delhi",
        "noida",
        "gurgaon",
        "gurugram",
        "pune",
        "chennai",
        "kolkata",
        "ahmedabad",
        "kochi",
        "cochin",
        "jaipur",
        "chandigarh",
        "indore",
        "bhopal",
    ],
)


def load_preferences(path: Union[str, Path] = PREFERENCES_PATH) -> tuple[Preferences, list[str]]:
    """Returns (effective preferences, warnings) - never raises. A bad
    preferences.json must degrade to DEFAULT_PREFERENCES, not crash the
    dashboard or the nightly pipeline. Falls back entirely (all 4 lists) on
    a missing or unparseable file; falls back per-list on an emptied one,
    so one bad edit doesn't discard the other three lists' real
    customization. Every fallback is recorded as a warning string, never
    applied silently - see the module comment above DEFAULT_PREFERENCES for
    why an empty list in particular is dangerous, not just wrong."""
    path = Path(path)
    warnings: list[str] = []

    if not path.exists():
        warnings.append(f"{path} not found - using built-in defaults (re-created the file from them).")
        save_preferences(DEFAULT_PREFERENCES, path)
        return DEFAULT_PREFERENCES, warnings

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        preferences = Preferences(**raw)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        warnings.append(f"{path} could not be parsed ({exc}) - using built-in defaults instead.")
        return DEFAULT_PREFERENCES, warnings

    defaults_by_field = DEFAULT_PREFERENCES.model_dump()
    effective = preferences.model_dump()
    for field_name, default_value in defaults_by_field.items():
        if not effective[field_name]:
            warnings.append(
                f"{field_name!r} in {path} is empty - falling back to the built-in default "
                f"({len(default_value)} entries) rather than matching nothing."
            )
            effective[field_name] = default_value

    return Preferences(**effective), warnings


def save_preferences(preferences: Preferences, path: Union[str, Path] = PREFERENCES_PATH) -> None:
    """Writes preferences as pretty-printed JSON - human-editable by hand if
    ever needed, matching companies.yaml's own editability. Does not
    validate emptiness here; that's load_preferences' job on the way back
    in, so a deliberate temporary save (mid-edit, about to fix the other
    field) is never blocked by this function itself."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(preferences.model_dump(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def apply_preferences(preferences: Preferences) -> None:
    """Rebinds the module-level TITLE_ALLOWLIST etc. that filters.py reads
    live via `config.X` attribute lookup (see the comment above
    DEFAULT_PREFERENCES) - the mechanism that lets a Roles-tab save or
    reset take effect immediately, in this same running process, without
    restarting Streamlit or the pipeline. Called once at import time below,
    and again by app.py's Roles tab after every save/reset."""
    global TITLE_ALLOWLIST, SENIORITY_KEYWORDS, NON_ENGINEERING_KEYWORDS, INDIA_LOCATION_KEYWORDS
    TITLE_ALLOWLIST = preferences.title_allowlist
    SENIORITY_KEYWORDS = preferences.seniority_keywords
    NON_ENGINEERING_KEYWORDS = preferences.non_engineering_keywords
    INDIA_LOCATION_KEYWORDS = preferences.india_location_keywords


# Populated once at import time so every existing caller (filters.py,
# pipeline.py, tests, ...) keeps working exactly as before with zero
# changes at their end - they still just get config.TITLE_ALLOWLIST etc.
# as a ready-to-use list the moment this module is imported, the same
# property the old hardcoded literals had. PREFERENCES_LOAD_WARNINGS is
# plain data, not surfaced here (config.py has no business depending on
# streamlit) - app.py shows it near the top of the page on every load, and
# pipeline.py's __main__ prints it before a real run, since an empty
# allowlist silently passing every job is most expensive exactly there.
_initial_preferences, PREFERENCES_LOAD_WARNINGS = load_preferences()
apply_preferences(_initial_preferences)

# extraction.py header lists - like the filter keyword lists above, these
# will need tuning as new JD formats show up. A header this doesn't recognize
# triggers an explicit, visible extraction failure (see extraction.py) rather
# than a silent bad extraction, so a gap here is loud, not invisible.
#
# "what you'll do"/"what you will do" added after a real miss: Acceldata's
# "Software Engineer - Open Data Platform" posting put its only concrete
# tech-stack requirements (years of experience, languages, distributed
# systems) under "What You'll Do:", while "What We're Looking For:" (already
# recognized) was pure soft skills for that posting. The extractor picked
# the recognized-but-wrong section instead of failing loudly, because it had
# a header to anchor on - just not the one with the signal. Since content
# headers aren't stop-bounded against each other, adding this doesn't lose
# "What We're Looking For" when both are present - it just starts earlier.

JD_CONTENT_HEADERS = [
    "requirements",
    "what we're looking for",
    "what you'll need",
    "what you need",
    "what you'll do",
    "what you will do",
    "qualifications",
    "minimum qualifications",
    "preferred qualifications",
    "must have",
    "must haves",
    "required skills",
    "skills required",
    "who you are",
    "you have",
    "what you bring",
    "bonus points",
    "nice to have",
    "nice to haves",
    "good to have",
]

JD_STOP_HEADERS = [
    "benefits",
    "perks",
    "about us",
    "about the company",
    "our culture",
    "culture",
    "compensation",
    "how to apply",
    "equal opportunity",
]

# llm.py / agents/analyst.py / pipeline.py - the model name is the one thing
# about a provider that changes on its own schedule (deprecations, new
# free-tier defaults), so it's config, not a literal.
#
# Two-stage Analyst, same cheap-filters-first principle as the rest of the
# pipeline: stage 1 screens every survivor with the cheap model, stage 2
# spends the scarce model's quota only on the jobs stage 1 says are worth a
# second look. This isn't just about cost - it's what makes a full run
# possible at all. Measured live at aistudio.google.com/rate-limit (see
# docs/decisions.md):
GEMINI_MODEL_STAGE1 = "gemini-3.5-flash-lite"
# Was gemini-2.5-flash - two generations OLDER than stage 1, which made
# "stage 2 disagrees with stage 1" meaningless as a quality signal.
# Found by pairing the two stages' cached verdicts, not by reading the
# config. Re-checked live against models.list: gemini-3.7-flash is the
# newest non-preview text model this key can reach. See docs/decisions.md.
GEMINI_MODEL_STAGE2 = "gemini-3.7-flash"

# RPM/RPD per model, read by llm.py (pacing) and pipeline.py (budget
# reporting) - not hardcoded in either, since these are provider facts that
# change on Google's schedule, not this project's. gemini-3.5-flash-lite's
# 500 RPD is what makes a full 52-job stage-1 pass survive in one day;
# gemini-2.5-flash's 20 RPD would be exhausted by fewer than half the
# survivors in a single stage.
GEMINI_RATE_LIMITS = {
    GEMINI_MODEL_STAGE1: {"rpm": 15, "rpd": 500},
    # rpd MEASURED, not assumed: the 429 body from a real exhausted run names
    # the quota directly - quotaId "GenerateRequestsPerDayPerProjectPerModel-
    # FreeTier", quotaValue 20. Same daily ceiling as the gemini-2.5-flash it
    # replaced, so STAGE2_TOP_N = 15 remains the right hard cap and this
    # model swap costs nothing in quota terms.
    # rpm is still a conservative floor rather than a measured ceiling: 8
    # consecutive calls at 5 RPM drew zero 429s, so the true limit is at
    # least that, and pacing slower than allowed only costs wall-clock time.
    # NOTE: retries consume quota too. A 503 "high demand" burst retried 5x
    # ate a quarter of one day's 20-call budget - see docs/decisions.md.
    GEMINI_MODEL_STAGE2: {"rpm": 5, "rpd": 20},
}

# HARD QUOTA CEILING, not a tuning knob. gemini-2.5-flash's entire daily
# budget is 20 requests (GEMINI_RATE_LIMITS above) - raising this past 15
# without raising GEMINI_MODEL_STAGE2's rpd first WILL exhaust the day's
# quota before stage 2 finishes, the same failure this two-stage design was
# built to avoid. 15 leaves 5 requests of headroom for a same-day retry.
STAGE2_TOP_N = 15

RESUME_SKILL_HEADERS = ["technical skills", "skills"]
RESUME_PROJECT_HEADERS = ["projects"]
RESUME_STOP_HEADERS = [
    "education",
    "certifications",
    "achievements",
    "experience",
    "work experience",
    "professional experience",
]


def load_companies(path: Union[str, Path] = "companies.yaml") -> list[CompanyConfig]:
    """Parse companies.yaml into typed CompanyConfig objects. Raises pydantic's
    ValidationError immediately on a bad ats value, a missing token, or an
    unknown field - a typo here should crash the pipeline at startup, not
    silently leave a company contributing zero jobs forever."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    return [CompanyConfig(**entry) for entry in raw]


if __name__ == "__main__":
    companies = load_companies()
    print(f"{len(companies)} companies loaded")
    for c in companies:
        suffix = f"  # {c.notes}" if c.notes else ""
        print(f"- {c.name} ({c.ats.value}): {c.token}{suffix}")
