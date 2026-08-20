import pytest
from pydantic import ValidationError

from models import (
    ATSSource,
    Cadence,
    CompanyConfig,
    JobPosting,
    Preferences,
    compute_content_hash,
    compute_description_hash,
    normalize_location,
)


def _make_posting(**overrides):
    defaults = dict(
        source=ATSSource.GREENHOUSE,
        source_job_id="123",
        company="Acme Corp",
        title="Software Engineer I",
        location="Hyderabad, India",
        description="We are looking for a software engineer to join our team.",
        url="https://boards.greenhouse.io/acme/jobs/123",
    )
    defaults.update(overrides)
    return JobPosting(**defaults)


def test_same_job_different_formatting_collides():
    """Same city, expressed as "<city>, <country>" on one source and
    "<city>, <state>" on the other - both normalize to just the city, so this
    still collides even though location is now part of the hash."""
    a = _make_posting(
        source=ATSSource.GREENHOUSE,
        title="Software Engineer - I",
        location="Hyderabad, India",
    )
    b = _make_posting(
        source=ATSSource.LEVER,
        source_job_id="456",
        title="software engineer i",
        location="Hyderabad, Telangana",
        url="https://jobs.lever.co/acme/456",
    )
    assert a.content_hash == b.content_hash


def test_same_title_different_city_does_not_collide():
    """The bug this fixes: "Software Engineer" in Bangalore and Hyderabad are
    two different jobs, not one duplicate."""
    bangalore = _make_posting(title="Software Engineer", location="Bangalore, Karnataka")
    hyderabad = _make_posting(title="Software Engineer", location="Hyderabad, Telangana")
    assert bangalore.content_hash != hyderabad.content_hash


def test_city_alias_collides_with_canonical_name():
    a = _make_posting(title="Software Engineer", location="Bangalore, Karnataka, India")
    b = _make_posting(title="Software Engineer", location="Bengaluru, India")
    assert a.content_hash == b.content_hash


def test_missing_location_does_not_collide_with_a_stated_one():
    no_location = _make_posting(title="Software Engineer", location=None)
    has_location = _make_posting(title="Software Engineer", location="Bangalore, Karnataka")
    assert no_location.content_hash != has_location.content_hash


@pytest.mark.parametrize(
    "location,expected",
    [
        (None, ""),
        ("", ""),
        ("Hyderabad, India", "hyderabad"),
        ("Hyderabad, Telangana", "hyderabad"),
        ("Bangalore, Karnataka, India", "bangalore"),
        ("Bengaluru, India", "bangalore"),
        ("BANGALORE, INDIA", "bangalore"),
        ("Bombay, Maharashtra", "mumbai"),
        ("Gurugram, Haryana", "gurgaon"),
        ("Pune  ", "pune"),
        ("Singapore", "singapore"),
    ],
)
def test_normalize_location(location, expected):
    assert normalize_location(location) == expected


def test_different_jobs_do_not_collide():
    different_title = _make_posting(title="Data Analyst")
    same_title = _make_posting(title="Software Engineer I")
    assert different_title.content_hash != same_title.content_hash

    different_company = _make_posting(company="Beta Inc")
    assert different_company.content_hash != same_title.content_hash


def test_description_edit_changes_description_hash_not_content_hash():
    original = _make_posting(description="Join our backend team as a fresher.")
    edited = _make_posting(description="Join our backend team as a fresher engineer.")

    assert original.content_hash == edited.content_hash
    assert original.description_hash != edited.description_hash


def test_model_hashes_match_standalone_functions():
    posting = _make_posting()
    assert posting.content_hash == compute_content_hash(posting.company, posting.title, posting.location)
    assert posting.description_hash == compute_description_hash(posting.description)


# ---------------------------------------------------------------------------
# Preferences - the Roles tab's editable rule-filter keyword lists
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


def test_preferences_accepts_all_four_lists():
    prefs = _make_preferences()
    assert prefs.title_allowlist == ["software engineer"]
    assert prefs.india_location_keywords == ["india"]


def test_preferences_allows_an_empty_list():
    """Emptiness is a config.py load_preferences concern (fall back and
    warn), not something Preferences itself should reject - config.py
    needs to be able to construct one from exactly what's on disk,
    including an accidentally-emptied field, before it can detect and
    react to that."""
    prefs = _make_preferences(title_allowlist=[])
    assert prefs.title_allowlist == []


def test_preferences_rejects_unknown_field():
    """extra='forbid', same reasoning as CompanyConfig: a typo'd key in a
    hand-edited preferences.json must fail validation loudly - triggering
    config.py's defaults fallback - rather than silently dropping the real
    field."""
    with pytest.raises(ValidationError):
        _make_preferences(tittle_allowlist=["oops"])


def test_preferences_requires_all_four_fields():
    with pytest.raises(ValidationError):
        Preferences(title_allowlist=["software engineer"])


# ---------------------------------------------------------------------------
# CompanyConfig - the token vs. workday_tenant/wd/site cross-field validator.
# Greenhouse/Lever/Ashby all share the token shape; only Workday differs, so
# most cases below are exercised via ats=greenhouse and treated as
# representative of all three token-based sources.
# ---------------------------------------------------------------------------


def test_company_config_accepts_a_token_based_source():
    config = CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme")
    assert config.token == "acme"
    assert config.workday_tenant is None


def test_company_config_rejects_a_token_based_source_with_no_token():
    with pytest.raises(ValidationError, match="requires `token`"):
        CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE)


def test_company_config_rejects_a_token_based_source_with_workday_fields_set():
    """A greenhouse entry that also carries workday_tenant (e.g. a copy-paste
    mistake while hand-editing companies.yaml) must fail loudly rather than
    silently ignoring the stray fields."""
    with pytest.raises(ValidationError, match="doesn't use workday_tenant"):
        CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme", workday_tenant="acme")


def test_company_config_accepts_a_complete_workday_entry():
    config = CompanyConfig(
        name="Acme India", ats=ATSSource.WORKDAY, workday_tenant="acme", workday_wd="wd1", workday_site="acmecareers"
    )
    assert config.token is None
    assert config.workday_wd == "wd1"


def test_company_config_rejects_workday_missing_one_field():
    """A typo'd or half-filled-in companies.yaml entry (e.g. workday_site
    forgotten) fails at load time, naming the specific missing field,
    rather than surfacing as an AttributeError deep inside fetch_all."""
    with pytest.raises(ValidationError, match="requires workday_site"):
        CompanyConfig(name="Acme India", ats=ATSSource.WORKDAY, workday_tenant="acme", workday_wd="wd1")


def test_company_config_rejects_workday_with_a_token_set():
    with pytest.raises(ValidationError, match="doesn't use `token`"):
        CompanyConfig(
            name="Acme India",
            ats=ATSSource.WORKDAY,
            token="acme",
            workday_tenant="acme",
            workday_wd="wd1",
            workday_site="acmecareers",
        )


def test_company_config_rejects_an_unknown_field():
    """extra="forbid" - same reasoning as Preferences: a misspelled field
    name in companies.yaml must fail loudly, not silently drop the real one."""
    with pytest.raises(ValidationError):
        CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme", toekn="acme")


# ---------------------------------------------------------------------------
# CompanyConfig.cadence - nightly by default, weekly opt-in (see
# pipeline.fetch_all and docs/decisions.md)
# ---------------------------------------------------------------------------


def test_company_config_defaults_to_nightly_cadence():
    """Every existing companies.yaml entry predates this field - it must
    keep meaning "fetch every run" without being edited."""
    config = CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme")
    assert config.cadence == Cadence.NIGHTLY


def test_company_config_accepts_weekly_cadence():
    config = CompanyConfig(name="Cisco", ats=ATSSource.WORKDAY, workday_tenant="cisco", workday_wd="wd5", workday_site="Cisco_Careers", cadence="weekly")
    assert config.cadence == Cadence.WEEKLY


def test_company_config_rejects_an_unknown_cadence():
    with pytest.raises(ValidationError):
        CompanyConfig(name="Acme", ats=ATSSource.GREENHOUSE, token="acme", cadence="monthly")
