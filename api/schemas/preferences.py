from pydantic import BaseModel, Field


class PreferenceLists(BaseModel):
    """The four editable rule-filter keyword lists. Mirrors
    models.Preferences exactly - kept as its own type so the HTTP contract
    doesn't silently change shape if the domain model gains an internal
    field."""

    title_allowlist: list[str]
    seniority_keywords: list[str]
    non_engineering_keywords: list[str]
    india_location_keywords: list[str]


class PreferencesResponse(PreferenceLists):
    """`warnings` carries config.load_preferences' fallback notices - an
    emptied or unparseable list means the filters are running on built-in
    defaults instead of what's on disk, and that has to be visible rather
    than silently corrected. An empty title_allowlist in particular would
    let every job through and burn a day's LLM quota on unfiltered noise."""

    warnings: list[str] = Field(default_factory=list)
    is_default: bool = False


class PreferencesImpact(BaseModel):
    """Survivor counts before and after a candidate edit. `unanalyzed_after`
    is how many of the new survivors have no Analyst result yet - i.e. how
    much a run would actually have to do."""

    current_survivors: int
    new_survivors: int
    delta: int
    unanalyzed_after: int


class PreferencesUpdateResponse(PreferenceLists):
    saved: bool
    survivors: int
    unanalyzed: int
    warnings: list[str] = Field(default_factory=list)
