import re
from collections import Counter
from typing import Optional

import config  # imported as a module, not `from config import X`,
# so config.TITLE_ALLOWLIST etc. are read fresh on every call below (module
# attribute lookup) rather than snapshotted once at this file's own import
# time. That's what lets app.py's Roles tab rebind config.TITLE_ALLOWLIST
# after a save/reset and have it take effect immediately, in the same
# running process - `from config import TITLE_ALLOWLIST` would have bound a
# private copy in this module's namespace that a later rebind on `config`
# itself would never touch. See config.py's apply_preferences.
from models import JobPosting, Preferences

# Explicit "no experience needed" signals. Checked before any numeric parsing
# so an employer's own fresher-friendly language always wins over a stray
# unrelated number elsewhere in the description.
_FRESHER_PATTERNS = [
    re.compile(r"\bfreshers?\b", re.IGNORECASE),
    re.compile(r"\bfresh\s+graduates?\b", re.IGNORECASE),
    re.compile(r"\brecent\s+graduates?\b", re.IGNORECASE),
    re.compile(r"\bentry[- ]level\b", re.IGNORECASE),
    # up to 3 filler words so "no prior professional experience" still matches,
    # not just literal "no experience"
    re.compile(r"\bno\s+(?:\w+\s+){0,3}experience\b", re.IGNORECASE),
]

# Bare numbers ("4 years", "4-year") are ambiguous on their own - a degree
# length, a notice period, a company's own history - so they only count as an
# experience requirement when "exp"/"experience" appears within a short
# window after the year figure. "N+ years" and "minimum/at least N years" are
# unambiguous idioms even without that word nearby, so those get no anchor
# requirement.
_RANGE_RE = re.compile(
    r"(\d+)[\s-]*(?:-|–|to)[\s-]*(\d+)\+?[\s-]*(?:years?|yrs?)\.?\s*.{0,25}?\bexp(?:erience)?\b",
    re.IGNORECASE,
)
_RANGE_REVERSE_RE = re.compile(
    r"\bexp(?:erience)?\b.{0,10}?(\d+)[\s-]*(?:-|–|to)[\s-]*(\d+)\+?[\s-]*(?:years?|yrs?)",
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(
    r"(\d+)[\s-]*(?:years?|yrs?)\.?\s*.{0,25}?\bexp(?:erience)?\b",
    re.IGNORECASE,
)
_PLUS_RE = re.compile(r"(\d+)\+\s*(?:years?|yrs?)", re.IGNORECASE)
_FLOOR_RE = re.compile(r"(?:minimum|min\.?|at least)\s*(?:of\s*)?(\d+)\+?[\s-]*(?:years?|yrs?)", re.IGNORECASE)


def parse_max_experience_years(description: str) -> Optional[float]:
    """Best-effort, rule-based extraction of the highest experience-years
    figure implied by a job description. Returns None when nothing anchored
    is found, or when an explicit fresher/no-experience signal is present -
    both cases mean "no known barrier," so callers should treat None as pass.

    Known limitation, not fixed: a company's own "N years of experience"
    history reads as anchored as a real candidate requirement, and taking the
    max of everything anchored can pick the wrong one. See
    tests/test_filters.py::test_experience_company_history_confused_with_candidate_requirement.
    """
    if any(pattern.search(description) for pattern in _FRESHER_PATTERNS):
        return None

    years: list[float] = []
    for pattern in (_RANGE_RE, _RANGE_REVERSE_RE):
        for match in pattern.finditer(description):
            years.append(float(match.group(2)))
    for match in _SINGLE_RE.finditer(description):
        years.append(float(match.group(1)))
    for match in _PLUS_RE.finditer(description):
        years.append(float(match.group(1)))
    for match in _FLOOR_RE.finditer(description):
        years.append(float(match.group(1)))

    return max(years) if years else None


def _text_contains_any(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(keyword.lower())}\b", lowered) for keyword in keywords)


# SENIORITY_KEYWORDS is a fixed word list with no concept of numbered levels
# at all - "SDE III", "Java Developer IV", "Software Development Engineer -
# II" passed it untouched (real titles observed live). This is exactly the
# gap MAX_EXPERIENCE_YEARS's hard cutoff had been catching by accident
# before it was made advisory - see docs/decisions.md.
#
# Roman numeral level indicators (II-V = levels 2-5). "I" is deliberately
# excluded - "Engineer I"/"SDE-I" read as entry level, not senior.
_ROMAN_NUMERAL_LEVEL_RE = re.compile(r"\b(II|III|IV|V)\b")

# "SDE-2" through "SDE-5" (hyphen, space, or no separator - "SDE2"). "SDE-1"
# is deliberately excluded, same entry-level reasoning as the roman numerals.
_SDE_LEVEL_RE = re.compile(r"\bSDE[\s-]?([2-5])\b", re.IGNORECASE)

# "L3" through "L7" - a common leveling scheme (L3/L4 read as mid-level,
# L6/L7 as staff/principal at many companies). L1/L2 deliberately excluded -
# those read as entry/junior in this scheme, not what this project rejects.
_L_LEVEL_RE = re.compile(r"\bL-?([3-7])\b", re.IGNORECASE)

# A bare digit 2-5 immediately after a recognized engineering role noun -
# "Software Engineer 2", "Backend Developer-3". Deliberately does NOT
# include "Tier" or "Level" as recognized prefixes: "Tier 2 Support" is a
# support-tier label, not an engineering seniority level, and matching it
# here would be a false positive for the wrong reason - it isn't in this
# list, so it's simply never considered, not special-cased.
_ROLE_NOUN_DIGIT_RE = re.compile(
    r"\b(?:engineer|developer|programmer|analyst|architect)[\s-]?([2-5])\b", re.IGNORECASE
)

# "3-5 Years", "2-4 yrs" - a digit range, not a level. Two digits joined by
# a hyphen (digit-hyphen-digit, not letter-hyphen-digit like "SDE-3") is
# stripped before the checks above run, so neither number in the range gets
# mistaken for a standalone level marker. This does not affect "SDE-3" -
# the character before that hyphen is a letter, not a digit, so the range
# pattern never matches it.
_DIGIT_RANGE_RE = re.compile(r"\d\s*[-–]\s*\d")


def has_numbered_seniority_level(title: str) -> bool:
    """True if the title contains a numbered or Roman-numeral seniority
    level (II-V, SDE-2..5, L3..L7, or "<role noun> 2..5") - not an
    entry-level marker (I, SDE-1, L1/L2), and not a coincidental digit from
    something else (a year range, a quarter/req-id fragment, a 4-digit
    year). See docs/decisions.md for the real titles that motivated this."""
    stripped = _DIGIT_RANGE_RE.sub(" ", title)
    return bool(
        _ROMAN_NUMERAL_LEVEL_RE.search(stripped)
        or _SDE_LEVEL_RE.search(stripped)
        or _L_LEVEL_RE.search(stripped)
        or _ROLE_NOUN_DIGIT_RE.search(stripped)
    )


def location_matches_india(location: Optional[str], india_location_keywords: Optional[list[str]] = None) -> bool:
    """True if the raw location string identifies an India-based or
    remote-inclusive-of-India posting - a recognized city or "india"/"ind"
    appearing anywhere in the string. This also correctly accepts multi-country
    strings like "Remote, Canada; Remote, India; Remote, US" without any special
    handling, since the India keyword just needs to appear somewhere in the
    text, not be the only thing in it. A missing location is rejected, same as
    everything else that doesn't confirm India eligibility.

    india_location_keywords: override for previewing a candidate Roles-tab
    edit before saving (see reject_reason_for) - defaults to the live
    config.INDIA_LOCATION_KEYWORDS every real filtering call uses."""
    if not location:
        return False
    keywords = india_location_keywords if india_location_keywords is not None else config.INDIA_LOCATION_KEYWORDS
    return _text_contains_any(location, keywords)


def reject_reason_for(title: str, location: Optional[str], preferences: Optional[Preferences] = None) -> Optional[str]:
    """The actual rule logic, operating directly on title/location strings
    rather than a full JobPosting - reject_reason(job) below is a thin
    wrapper. Exists so a caller that doesn't have (or doesn't want to pay
    for constructing) a full JobPosting can still filter: every rule here
    only ever reads title and location, never description - the heaviest
    field per row - so a caller can run this against a lightweight
    (title, location) query instead of a full ORM row + Pydantic
    reconstruction. See app.py's run_filter_pass, which does exactly that
    against the live database rather than materializing every row just to
    discard most of them (see docs/decisions.md).

    preferences: when given, evaluates against these candidate keyword
    lists instead of the live config.* values - app.py's Roles tab uses
    this to preview the survivor-count impact of an in-progress edit
    before saving it, without mutating config.* (which every other
    session/tab reads too). None (the default) means "whatever's currently
    loaded" - every real filtering call (pipeline.py, app.py's own actual
    filter pass, tests) leaves this unset.

    No experience-years cutoff here: parse_max_experience_years is accurate
    (verified against real false-reject cases - the parser was reading real,
    correctly-anchored requirements, not company boilerplate), but a hard
    MAX_EXPERIENCE_YEARS cutoff encoded a judgment this system isn't
    equipped to make - whether a given number of years is worth applying
    anyway is a per-job call, and the hand-labeled ground truth showed the
    old cutoff (2 years) rejected roles the user's own labels accepted up to
    7 years. See docs/decisions.md. The parsed figure is still computed and
    stored on every job (see db.py, pipeline.py's persist_jobs) and surfaced
    alongside each listing so the user can judge it themselves, not silently
    dropped."""
    seniority_keywords = preferences.seniority_keywords if preferences else config.SENIORITY_KEYWORDS
    non_engineering_keywords = preferences.non_engineering_keywords if preferences else config.NON_ENGINEERING_KEYWORDS
    title_allowlist = preferences.title_allowlist if preferences else config.TITLE_ALLOWLIST
    india_location_keywords = preferences.india_location_keywords if preferences else config.INDIA_LOCATION_KEYWORDS

    if _text_contains_any(title, seniority_keywords) or has_numbered_seniority_level(title):
        return "seniority"
    if _text_contains_any(title, non_engineering_keywords):
        return "non_engineering"
    if not _text_contains_any(title, title_allowlist):
        return "not_allowlisted"
    if not location_matches_india(location, india_location_keywords):
        return "not_india"

    return None


def reject_reason(job: JobPosting, preferences: Optional[Preferences] = None) -> Optional[str]:
    """The single reason a job is filtered out, or None if it survives every
    rule. See reject_reason_for's docstring for the rule logic itself, why
    it's factored out this way, and what `preferences` overrides."""
    return reject_reason_for(job.title, job.location, preferences)


def rejected_jobs(jobs: list[JobPosting], preferences: Optional[Preferences] = None) -> list[tuple[JobPosting, str]]:
    """Every rejected job paired with which rule it failed - the detail view
    behind filter_jobs' summary counts, for inspecting what a rule actually
    turns away before tuning its keyword list."""
    return [(job, reason) for job in jobs if (reason := reject_reason(job, preferences)) is not None]


def filter_jobs(jobs: list[JobPosting], preferences: Optional[Preferences] = None) -> tuple[list[JobPosting], Counter]:
    """Split jobs into survivors and a count of rejections per rule."""
    kept: list[JobPosting] = []
    rejected_by: Counter = Counter()

    for job in jobs:
        reason = reject_reason(job, preferences)
        if reason is None:
            kept.append(job)
        else:
            rejected_by[reason] += 1

    return kept, rejected_by
