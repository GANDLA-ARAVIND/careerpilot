import pytest

from filters import (
    filter_jobs,
    has_numbered_seniority_level,
    location_matches_india,
    parse_max_experience_years,
    reject_reason,
    reject_reason_for,
)
from models import ATSSource, JobPosting, Preferences


def _make_posting(**overrides):
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="123",
        company="Acme Corp",
        title="Software Engineer I",
        location="Bangalore, Karnataka",  # India-eligible by default, so existing
        # tests exercise the rule they're named for, not an incidental not_india
        description="We are looking for a software engineer to join our team.",
        url="https://boards.greenhouse.io/acme/jobs/123",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


# ---------------------------------------------------------------------------
# parse_max_experience_years - the cases from the task, verified against a
# real parser design: numbers are only read as an experience requirement when
# anchored near "experience"/"exp" (a bare "4-year degree" mentioned with no
# such word nearby must never be mistaken for a 4-year experience ask).
# "N+ years" and "minimum/at least N years" are unambiguous idioms even
# without that anchor.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description,expected",
    [
        ("We need someone with 2+ years of experience in Python.", 2.0),
        ("0-2 years of experience required.", 2.0),
        ("Minimum 3 years of professional experience is required.", 3.0),
        ("Looking for engineers with 2 to 4 years of experience.", 4.0),
        ("Freshers welcome to apply for this role.", None),
        ("This is a fresher-only position, no experience required.", None),
        ("0-1 year of relevant experience is a plus.", 1.0),
    ],
)
def test_experience_cases_from_task(description, expected):
    assert parse_max_experience_years(description) == expected


def test_experience_yrs_abbreviation():
    assert parse_max_experience_years("2-3 yrs exp required, immediate joiners preferred.") == 3.0


def test_experience_floor_only_accepts_on_unknown_ceiling():
    """"at least 1 year" states a floor, not a ceiling. We accept (return the
    floor) rather than reject on an unknown upper bound - a deliberate
    false-accept risk: a role wanting "1, ideally 5+" would still pass."""
    assert parse_max_experience_years("Candidates should have at least 1 year of relevant experience.") == 1.0


def test_experience_high_floor_still_rejects():
    assert parse_max_experience_years("10+ years of experience building distributed systems.") == 10.0


def test_experience_unrelated_numbers_ignored_via_fresher_override():
    text = (
        "30 days notice period. 2 rounds of interviews. "
        "This is a great opportunity for freshers with 0 years of experience."
    )
    assert parse_max_experience_years(text) is None


def test_experience_degree_length_without_experience_word_not_matched():
    """A "4-year" degree mentioned with no "experience"/"exp" anywhere in the
    text at all must not be read as a 4-year experience requirement - the
    anchor simply never finds anything to attach to."""
    text = "Candidates must hold a 4-year engineering degree from an accredited university."
    assert parse_max_experience_years(text) is None


def test_experience_degree_length_with_no_experience_phrase_nearby():
    """Same degree-length trap, but now "experience" does appear in the text
    (in a "not necessary" phrase) - the fresher-override regex must catch
    "no prior professional experience", not just literal "no experience", or
    the degree year would be wrongly picked up as the requirement instead."""
    text = "Candidates must hold a 4-year engineering degree. No prior professional experience is necessary."
    assert parse_max_experience_years(text) is None


@pytest.mark.xfail(
    reason=(
        "Known limitation: a company's own 'N years of experience' history sits "
        "just as close to the word 'experience' as a real candidate requirement "
        "does. Taking the max of all anchored numbers picks up the company's "
        "figure over the candidate's stated 0, causing a false reject - a real "
        "NLP problem, not solvable by proximity regex. Documented, not fixed."
    )
)
def test_experience_company_history_confused_with_candidate_requirement():
    text = (
        "Acme Corp has 5 years of experience delivering fintech products. "
        "We are hiring Associate Software Engineers straight out of college; "
        "candidates require 0 years of prior work experience."
    )
    assert parse_max_experience_years(text) is None


# ---------------------------------------------------------------------------
# Title-based rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Senior Software Engineer", "Staff Engineer", "Engineering Manager", "VP of Engineering", "Director, Platform"],
)
def test_seniority_titles_rejected(title):
    assert reject_reason(_make_posting(title=title)) == "seniority"


@pytest.mark.parametrize(
    "title",
    ["Business Development Executive", "Area Sales Executive", "Collections Associate", "HR Generalist"],
)
def test_non_engineering_titles_rejected(title):
    assert reject_reason(_make_posting(title=title)) == "non_engineering"


def test_title_not_on_allowlist_rejected():
    assert reject_reason(_make_posting(title="Product Analyst")) == "not_allowlisted"


@pytest.mark.parametrize(
    "title",
    ["Software Engineer I", "SDE 1", "Backend Developer", "Associate Software Engineer", "Graduate Engineer Trainee"],
)
def test_allowlisted_titles_pass(title):
    assert reject_reason(_make_posting(title=title)) is None


# ---------------------------------------------------------------------------
# Numbered seniority levels - SENIORITY_KEYWORDS is a fixed word list with no
# concept of numbered levels at all, so "SDE III"/"Java Developer IV" passed
# every existing filter untouched (real titles observed in the live
# database). This gap is exactly what MAX_EXPERIENCE_YEARS's hard cutoff had
# been catching by accident before it was made advisory - see
# docs/decisions.md. Tested directly against has_numbered_seniority_level
# (not just via reject_reason) so a title that would ALSO be excluded for an
# unrelated reason (e.g. "Tier 2 Support" is arguably non-engineering too)
# still proves this specific rule doesn't fire for the wrong reason.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "SDE III",
        "Java Developer IV",
        "Software Development Engineer - II",
        "Engineer III",
        "SDE-2",
        "SDE-3",
        "SDE-4",
        "SDE-5",
        "SDE2",  # no separator
        "L3 Backend Engineer",
        "L7 Software Engineer",
        "Software Engineer 2",  # bare arabic digit, no roman numeral
        "Backend Developer-3",
    ],
)
def test_numbered_seniority_levels_detected(title):
    assert has_numbered_seniority_level(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer I",  # entry level - roman numeral I excluded on purpose
        "SDE-1",
        "SDE 1",
        "SDE1",
        "L1 Engineer",  # below the L3-L7 range this project treats as senior
        "L2 Backend Engineer",
        "Tier 2 Support",  # a support tier, not an engineering level - must not match via a role-noun-adjacent digit
        "Software Engineer, React Native (3-5 Years)",  # real title - a year range, not a level
        "Backend Engineer Q2-02",  # real title - "Q2" is a quarter/req-id fragment, not a level
        "Software Engineer (Batch 2024)",  # a 4-digit year, not a single-digit level
        "Backend Developer",  # no digit or numeral at all
    ],
)
def test_numbered_seniority_levels_not_falsely_matched(title):
    assert has_numbered_seniority_level(title) is False


@pytest.mark.parametrize("title", ["SDE III", "Java Developer IV", "L5 Backend Engineer"])
def test_numbered_seniority_titles_rejected_via_reject_reason(title):
    assert reject_reason(_make_posting(title=title)) == "seniority"


def test_year_range_title_still_survives_via_reject_reason():
    """The exact real title that motivated the "title with a year in it"
    test case - must not be rejected as seniority just because it contains
    digits."""
    assert reject_reason(_make_posting(title="Software Engineer, React Native (3-5 Years)")) is None


# ---------------------------------------------------------------------------
# reject_reason_for - the lower-level primitive reject_reason(job) now
# delegates to, so a caller (app.py's run_filter_pass) can filter directly
# against title/location strings without constructing a full JobPosting for
# a row that's just going to be discarded.
# ---------------------------------------------------------------------------


def test_reject_reason_for_matches_reject_reason_for_every_case():
    """Same rule logic, two entry points - reject_reason(job) must always
    agree with reject_reason_for(job.title, job.location) for the exact
    same inputs, since one is defined purely in terms of the other."""
    cases = [
        ("Senior Software Engineer", "Bangalore, Karnataka"),
        ("Business Development Executive", "Bangalore, Karnataka"),
        ("Product Analyst", "Bangalore, Karnataka"),
        ("Software Engineer I", "San Francisco, California"),
        ("SDE III", "Bangalore, Karnataka"),
        ("Software Engineer I", "Bangalore, Karnataka"),
    ]
    for title, location in cases:
        job = _make_posting(title=title, location=location)
        assert reject_reason_for(title, location) == reject_reason(job)


def test_reject_reason_for_handles_missing_location_directly():
    assert reject_reason_for("Software Engineer I", None) == "not_india"


@pytest.mark.parametrize(
    "location",
    [
        "India",
        "Bengaluru, India",
        "Bangalore, Karnataka",
        "Bangalore, IND",
        "Hyderabad, Telangana, India",
        "Noida, Uttar Pradesh",
        "Mumbai, Maharashtra",
        "India (Remote)",
        "India - remote",
        "Remote, Canada; Remote, India; Remote, US",
        "Bangalore, India; Remote, Canada; Remote, Israel; Remote, United Kingdom; Remote, United States",
    ],
)
def test_location_matches_india_accepts_real_observed_patterns(location):
    assert location_matches_india(location) is True


@pytest.mark.parametrize(
    "location",
    [
        None,
        "",
        "Remote",
        "Remote - US",
        "Remote(US)",
        "Remote, USA",
        "United States",
        "San Francisco, California",
        "Singapore",
        "London, United Kingdom",
        "Remote, Canada; Remote, United Kingdom; Remote, United States",
    ],
)
def test_location_matches_india_rejects_non_india_patterns(location):
    """A bare "Remote" (or "Remote - US") with no India signal is rejected -
    on this roster, unqualified "Remote" postings are US-only in practice."""
    assert location_matches_india(location) is False


def test_non_india_location_rejected_after_title_checks_pass():
    job = _make_posting(title="Software Engineer", location="San Francisco, California")
    assert reject_reason(job) == "not_india"


def test_high_experience_requirement_no_longer_rejected():
    """experience_too_high was removed - a stated experience requirement is
    advisory (see pipeline.py's _format_experience), never a hard reject.
    The parser was correct; the hard cutoff was the config error - see
    docs/decisions.md."""
    job = _make_posting(
        title="Software Engineer",
        description="Minimum 5 years of experience required.",
    )
    assert reject_reason(job) is None


# ---------------------------------------------------------------------------
# filter_jobs aggregate behavior
# ---------------------------------------------------------------------------


def test_filter_jobs_splits_kept_and_counts_rejections_by_rule():
    jobs = [
        _make_posting(source_job_id="1", title="Software Engineer I"),
        _make_posting(source_job_id="2", title="Senior Software Engineer"),
        _make_posting(source_job_id="3", title="Area Sales Executive"),
        _make_posting(source_job_id="4", title="Product Analyst"),
        _make_posting(
            # high experience requirement no longer rejects - kept, not counted below
            source_job_id="5",
            title="Backend Developer",
            description="Minimum 5 years of experience required.",
        ),
        _make_posting(source_job_id="6", title="Frontend Developer", location="San Francisco, California"),
    ]

    kept, rejected_by = filter_jobs(jobs)

    assert [job.source_job_id for job in kept] == ["1", "5"]
    assert rejected_by == {
        "seniority": 1,
        "non_engineering": 1,
        "not_allowlisted": 1,
        "not_india": 1,
    }


# ---------------------------------------------------------------------------
# preferences override - app.py's Roles tab preview, without touching
# config.* global state every other session/call reads.
# ---------------------------------------------------------------------------


def _make_preferences(**overrides):
    defaults = dict(
        title_allowlist=["software engineer"],
        seniority_keywords=["senior"],
        non_engineering_keywords=["sales"],
        india_location_keywords=["india"],
    )
    defaults.update(overrides)
    return Preferences(**defaults)


def test_location_matches_india_override_replaces_live_keywords():
    # "atlantis" is not in the real config.INDIA_LOCATION_KEYWORDS list
    assert location_matches_india("Atlantis", india_location_keywords=["atlantis"]) is True
    assert location_matches_india("Atlantis") is False  # unaffected without an override


def test_reject_reason_for_override_replaces_all_four_lists():
    prefs = _make_preferences(title_allowlist=["zzznotarealtitle"])
    # Fails the real live TITLE_ALLOWLIST but not this candidate one
    assert reject_reason_for("Zzznotarealtitle", "India") is not None  # rejected under live config
    assert reject_reason_for("Zzznotarealtitle", "India", preferences=prefs) is None


def test_reject_reason_for_override_can_be_stricter_than_live():
    """A candidate ruleset can also reject something the live config would
    have kept - the preview must show survivor count going DOWN too, not
    just up."""
    prefs = _make_preferences(seniority_keywords=["engineer"])  # deliberately broad
    assert reject_reason_for("Software Engineer I", "India") is None  # survives live config
    assert reject_reason_for("Software Engineer I", "India", preferences=prefs) == "seniority"


def test_reject_reason_override_does_not_mutate_config_globals():
    """The whole point of threading `preferences` through as an explicit
    argument instead of monkeypatching config.* - a preview call must never
    leak into a later call that doesn't pass one."""
    import config

    original = list(config.TITLE_ALLOWLIST)
    prefs = _make_preferences(title_allowlist=["totally different list"])

    reject_reason_for("Totally Different List", "India", preferences=prefs)

    assert config.TITLE_ALLOWLIST == original


def test_reject_reason_accepts_preferences_override():
    job = _make_posting(title="Zzznotarealtitle", location="Bangalore, India")
    prefs = _make_preferences(title_allowlist=["zzznotarealtitle"])

    assert reject_reason(job) is not None
    assert reject_reason(job, preferences=prefs) is None


def test_filter_jobs_accepts_preferences_override():
    jobs = [_make_posting(source_job_id="1", title="Zzznotarealtitle", location="Bangalore, India")]
    prefs = _make_preferences(title_allowlist=["zzznotarealtitle"])

    kept_live, _ = filter_jobs(jobs)
    kept_candidate, _ = filter_jobs(jobs, preferences=prefs)

    assert kept_live == []
    assert len(kept_candidate) == 1
