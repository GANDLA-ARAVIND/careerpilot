"""The editable rule-filter keyword lists.

Every write goes through config.save_preferences + config.apply_preferences
so the change takes effect in this process immediately - filters.py reads
config.TITLE_ALLOWLIST etc. as live module attributes for exactly this
reason (see config.apply_preferences).

Preview and save both report survivor counts by running the real filter
pass against the candidate lists via filters' `preferences` override -
never a second copy of the rule logic.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import config
from api.deps import get_session
from api.schemas.preferences import (
    PreferenceLists,
    PreferencesImpact,
    PreferencesResponse,
    PreferencesUpdateResponse,
)
from api.services.dashboard import load_dashboard_jobs, run_filter_pass
from app import read_last_viewed
from models import Preferences

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


def _current_lists() -> PreferenceLists:
    return PreferenceLists(
        title_allowlist=list(config.TITLE_ALLOWLIST),
        seniority_keywords=list(config.SENIORITY_KEYWORDS),
        non_engineering_keywords=list(config.NON_ENGINEERING_KEYWORDS),
        india_location_keywords=list(config.INDIA_LOCATION_KEYWORDS),
    )


def _survivors_and_pending(session: Session, preferences=None) -> tuple[int, int]:
    result = run_filter_pass(session, preferences)
    scored, unscored, pending = load_dashboard_jobs(session, result.kept, read_last_viewed())
    return len(result.kept), pending


def _save(preferences: Preferences) -> None:
    """Saves and applies, passing the path EXPLICITLY.

    config.save_preferences' signature is
    `save_preferences(preferences, path=PREFERENCES_PATH)` - a default
    argument, so the path is bound once when config.py is imported.
    Calling it as save_preferences(prefs) therefore always writes to
    whatever the path was at import time, and reassigning
    config.PREFERENCES_PATH afterwards has no effect at all. That is not a
    hypothetical: it made a test write to the user's real
    data/preferences.json instead of its temp copy. Reading
    config.PREFERENCES_PATH here and passing it through resolves it at call
    time, which is what the redirection people expect. (Same early-binding
    trap as db.get_engine's db_path default - see docs/decisions.md.)"""
    config.save_preferences(preferences, config.PREFERENCES_PATH)
    config.apply_preferences(preferences)


def _reject_empty(payload: PreferenceLists) -> None:
    """An empty title_allowlist would match nothing to allow, so every job
    would pass that rule - roughly 8000 jobs through to the Analyst, and a
    day's free-tier quota gone in one run. config.load_preferences already
    falls back to defaults for an emptied list on read; refusing the write
    here means the user finds out now, from a 422 that says why, rather
    than later from a silent fallback."""
    for field_name in ("title_allowlist", "seniority_keywords", "non_engineering_keywords", "india_location_keywords"):
        if not getattr(payload, field_name):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{field_name} cannot be empty. An empty title_allowlist in particular would let "
                    f"every fetched job through to the Analyst and exhaust the daily LLM quota. "
                    f"Use POST /api/preferences/reset to restore built-in defaults."
                ),
            )


@router.get("", response_model=PreferencesResponse)
def get_preferences() -> PreferencesResponse:
    lists = _current_lists()
    return PreferencesResponse(
        **lists.model_dump(),
        warnings=list(config.PREFERENCES_LOAD_WARNINGS),
        is_default=lists.model_dump() == config.DEFAULT_PREFERENCES.model_dump(),
    )


@router.post("/preview", response_model=PreferencesImpact)
def preview_preferences(
    payload: PreferenceLists,
    session: Session = Depends(get_session),
) -> PreferencesImpact:
    """Impact without saving. Runs the same filter pass the pipeline would,
    against the candidate lists, leaving config.* untouched."""
    _reject_empty(payload)
    current_survivors, _ = _survivors_and_pending(session)
    candidate = Preferences(**payload.model_dump())
    new_survivors, unanalyzed = _survivors_and_pending(session, candidate)
    return PreferencesImpact(
        current_survivors=current_survivors,
        new_survivors=new_survivors,
        delta=new_survivors - current_survivors,
        unanalyzed_after=unanalyzed,
    )


@router.put("", response_model=PreferencesUpdateResponse)
def update_preferences(
    payload: PreferenceLists,
    session: Session = Depends(get_session),
) -> PreferencesUpdateResponse:
    _reject_empty(payload)
    preferences = Preferences(**payload.model_dump())
    _save(preferences)

    survivors, unanalyzed = _survivors_and_pending(session)
    return PreferencesUpdateResponse(
        **payload.model_dump(),
        saved=True,
        survivors=survivors,
        unanalyzed=unanalyzed,
        warnings=[],
    )


@router.post("/reset", response_model=PreferencesUpdateResponse)
def reset_preferences(session: Session = Depends(get_session)) -> PreferencesUpdateResponse:
    defaults = config.DEFAULT_PREFERENCES
    _save(defaults)

    survivors, unanalyzed = _survivors_and_pending(session)
    return PreferencesUpdateResponse(
        **defaults.model_dump(),
        saved=True,
        survivors=survivors,
        unanalyzed=unanalyzed,
        warnings=[],
    )
