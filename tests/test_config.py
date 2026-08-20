import json

import pytest

import config
from models import Preferences


def _make_preferences(**overrides):
    defaults = dict(
        title_allowlist=["engineer"],
        seniority_keywords=["senior"],
        non_engineering_keywords=["sales"],
        india_location_keywords=["india"],
    )
    defaults.update(overrides)
    return Preferences(**defaults)


@pytest.fixture
def preferences_path(tmp_path):
    """Every load_preferences/save_preferences call in these tests passes
    this explicit path - never the real data/preferences.json, the same
    isolation discipline this project applies to the SQLite database in
    app.py's tests (see tests/test_app.py)."""
    return tmp_path / "preferences.json"


@pytest.fixture
def restore_config_globals():
    """apply_preferences mutates real module-level config.* state that
    filters.py (and its own tests) depend on being at real/default values -
    snapshot and restore around any test that calls apply_preferences, so
    this test file can never leak a test-only ruleset into a test that
    runs after it, regardless of pass/fail or test order."""
    original = {
        "TITLE_ALLOWLIST": config.TITLE_ALLOWLIST,
        "SENIORITY_KEYWORDS": config.SENIORITY_KEYWORDS,
        "NON_ENGINEERING_KEYWORDS": config.NON_ENGINEERING_KEYWORDS,
        "INDIA_LOCATION_KEYWORDS": config.INDIA_LOCATION_KEYWORDS,
    }
    yield
    for name, value in original.items():
        setattr(config, name, value)


# ---------------------------------------------------------------------------
# DEFAULT_PREFERENCES
# ---------------------------------------------------------------------------


def test_default_preferences_has_no_empty_lists():
    """The one invariant that must never break: DEFAULT_PREFERENCES is the
    fallback target precisely because it's assumed safe - an empty list
    here would defeat load_preferences' whole reason to exist."""
    dumped = config.DEFAULT_PREFERENCES.model_dump()
    assert all(dumped.values()), "DEFAULT_PREFERENCES has an empty list - this must never happen"


# ---------------------------------------------------------------------------
# save_preferences / load_preferences round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_round_trips(preferences_path):
    prefs = _make_preferences(title_allowlist=["backend", "frontend"])
    config.save_preferences(prefs, preferences_path)

    loaded, warnings = config.load_preferences(preferences_path)

    assert loaded == prefs
    assert warnings == []


def test_save_writes_human_readable_json(preferences_path):
    """Human-editable by hand if ever needed, matching companies.yaml's own
    editability - not a minified single line."""
    config.save_preferences(_make_preferences(), preferences_path)
    text = preferences_path.read_text(encoding="utf-8")
    assert "\n" in text
    assert json.loads(text)  # still valid JSON


# ---------------------------------------------------------------------------
# load_preferences - missing file
# ---------------------------------------------------------------------------


def test_load_missing_file_falls_back_to_defaults(preferences_path):
    loaded, warnings = config.load_preferences(preferences_path)

    assert loaded == config.DEFAULT_PREFERENCES
    assert len(warnings) == 1
    assert "not found" in warnings[0]


def test_load_missing_file_recreates_it_from_defaults(preferences_path):
    assert not preferences_path.exists()
    config.load_preferences(preferences_path)

    assert preferences_path.exists()
    recreated, warnings = config.load_preferences(preferences_path)
    assert recreated == config.DEFAULT_PREFERENCES
    assert warnings == []  # the file exists now - no fallback needed on this second load


# ---------------------------------------------------------------------------
# load_preferences - malformed file
# ---------------------------------------------------------------------------


def test_load_unparseable_json_falls_back_to_defaults(preferences_path):
    preferences_path.write_text("{not valid json", encoding="utf-8")

    loaded, warnings = config.load_preferences(preferences_path)

    assert loaded == config.DEFAULT_PREFERENCES
    assert len(warnings) == 1
    assert "could not be parsed" in warnings[0]


def test_load_unknown_field_falls_back_to_defaults(preferences_path):
    """extra='forbid' on Preferences means a typo'd key fails validation,
    not just gets silently dropped - load_preferences must catch that and
    fall back the same as any other malformed file, not raise."""
    preferences_path.write_text(
        json.dumps(
            {
                "tittle_allowlist": ["oops"],
                "seniority_keywords": ["senior"],
                "non_engineering_keywords": ["sales"],
                "india_location_keywords": ["india"],
            }
        ),
        encoding="utf-8",
    )

    loaded, warnings = config.load_preferences(preferences_path)

    assert loaded == config.DEFAULT_PREFERENCES
    assert len(warnings) == 1
    assert "could not be parsed" in warnings[0]


def test_load_wrong_type_falls_back_to_defaults(preferences_path):
    preferences_path.write_text(json.dumps({"title_allowlist": "not a list"}), encoding="utf-8")

    loaded, warnings = config.load_preferences(preferences_path)

    assert loaded == config.DEFAULT_PREFERENCES
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# load_preferences - one or more emptied lists
# ---------------------------------------------------------------------------


def test_load_one_empty_list_falls_back_only_that_field(preferences_path):
    """The other 3 fields' real customization must survive - an empty
    title_allowlist shouldn't discard a deliberately-edited
    seniority_keywords too."""
    custom = _make_preferences(title_allowlist=[], seniority_keywords=["senior", "staff", "principal"])
    config.save_preferences(custom, preferences_path)

    loaded, warnings = config.load_preferences(preferences_path)

    assert loaded.title_allowlist == config.DEFAULT_PREFERENCES.title_allowlist
    assert loaded.seniority_keywords == ["senior", "staff", "principal"]  # preserved, not reset
    assert len(warnings) == 1
    assert "title_allowlist" in warnings[0]
    assert "empty" in warnings[0]


def test_load_multiple_empty_lists_each_get_their_own_warning(preferences_path):
    custom = _make_preferences(title_allowlist=[], india_location_keywords=[])
    config.save_preferences(custom, preferences_path)

    loaded, warnings = config.load_preferences(preferences_path)

    assert loaded.title_allowlist == config.DEFAULT_PREFERENCES.title_allowlist
    assert loaded.india_location_keywords == config.DEFAULT_PREFERENCES.india_location_keywords
    assert loaded.seniority_keywords == ["senior"]  # untouched field, preserved
    assert len(warnings) == 2


def test_load_all_four_empty_falls_back_entirely_with_four_warnings(preferences_path):
    custom = Preferences(
        title_allowlist=[], seniority_keywords=[], non_engineering_keywords=[], india_location_keywords=[]
    )
    config.save_preferences(custom, preferences_path)

    loaded, warnings = config.load_preferences(preferences_path)

    assert loaded == config.DEFAULT_PREFERENCES
    assert len(warnings) == 4


# ---------------------------------------------------------------------------
# apply_preferences - the live-reload mechanism filters.py depends on
# ---------------------------------------------------------------------------


def test_apply_preferences_rebinds_config_module_attributes(restore_config_globals):
    custom = _make_preferences(title_allowlist=["only this survives"])
    config.apply_preferences(custom)

    assert config.TITLE_ALLOWLIST == ["only this survives"]
    assert config.SENIORITY_KEYWORDS == custom.seniority_keywords
    assert config.NON_ENGINEERING_KEYWORDS == custom.non_engineering_keywords
    assert config.INDIA_LOCATION_KEYWORDS == custom.india_location_keywords


def test_apply_preferences_takes_effect_in_filters_module_immediately(restore_config_globals):
    """The actual property this whole design exists for: filters.py reads
    config.TITLE_ALLOWLIST live (via `import config`, not `from config
    import TITLE_ALLOWLIST`) - a rebind here must be visible to
    filters.reject_reason_for on the very next call, in the same process,
    with no re-import needed. This is what lets app.py's Roles tab take
    effect without restarting Streamlit."""
    import filters

    config.apply_preferences(_make_preferences(title_allowlist=["zzzuniquetoken"]))

    assert filters.reject_reason_for("Zzzuniquetoken Engineer", "India") is None
    assert filters.reject_reason_for("Software Engineer", "India") == "not_allowlisted"
