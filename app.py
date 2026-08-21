"""Streamlit dashboard - the thing opened every morning to see what's worth
applying to. Read-only against the database and companies.yaml except three
write paths: application_status (already protected by upsert_job never
touching it - see db.py; also sets applied_at the first time a job is
marked "applied" - see set_application_status and the Applied tab), resume
upload (an explicit, confirm-before-save action - see the Resume tab), and
the rule-filter keyword lists (Roles tab - add/remove entries, preview
impact, save to data/preferences.json; see config.py's Preferences/
load_preferences/save_preferences/apply_preferences). The Run tab is a
fourth surface with real side effects, but not a fourth *write path* of its
own - it just triggers the same orchestrator.py a cron job would, which
writes through the same upsert_job/set_application_status paths everything
else already goes through. Never fetches or calls an LLM on its own outside
of that - this otherwise only displays what's already there.

Score lookup mirrors agents/coach.py's stage1_overlap: AnalystResultRow has
no foreign key back to the job it scored (its primary key is a hash of
everything that determined the LLM's input, not the job's identity), so a
job's score is found by recomputing the same cache key agents/analyst.py's
analyze() would use and looking it up - never by re-deriving a fit_score
independently. Stage-2's result is preferred when present, stage-1
otherwise, per the two-stage design (see docs/decisions.md).

run_filter_pass is st.cache_data-wrapped (ttl=300) since it depends only on
title/location/company, none of which change within a session - see its own
docstring. load_dashboard_jobs deliberately has no such wrapper: it depends
on application_status and resume text, both expected to show up immediately
within a session, and the scored population is small enough (~20-50 jobs)
that a full reload on every interaction is fast without caching. See
docs/decisions.md for the measured before/after.

Page is organized into tabs (Jobs / Run / Roles / Resume / Applied /
Rejected / Companies / Coach) - a run-stats strip stays outside the tabs
since it's an orienting glance, not something worth clicking into. Tab
*order* is
entirely determined by the string list passed to st.tabs() - verified live
before relying on this: the position of each `with tab_x:` block in this
file's own source does not affect render order at all, so the tab bodies
below don't need to appear in the same order as st.tabs()'s list, though
they mostly do, for a human reader's sake.
"""

import html
import io
import json
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pypdf
from sqlalchemy import func
from sqlalchemy.orm import Session

import config
from agents.analyst import SYSTEM_INSTRUCTION, _text_hash, prepare_resume_text
from agents.coach import missing_skills_below
from config import GEMINI_MODEL_STAGE1, GEMINI_MODEL_STAGE2, load_companies
from db import AnalystResultRow, JobPostingRow, get_engine, job_posting_from_row
from extraction import extract_jd_requirements, extract_resume_sections
from filters import reject_reason_for
from models import CompanyConfig, JobPosting, Preferences

RESUME_PATH = Path("data/resume.txt")
LAST_VIEWED_PATH = Path("data/dashboard_last_viewed.txt")
STATUSES = ["new", "applied", "rejected", "interviewing"]
TOP_N = 20  # CLAUDE.md's own success criterion is "15-25 genuinely relevant roles" - no control exposed for this, see module docstring's simplicity note
REJECTED_SAMPLE_SIZE = 30  # same default pipeline.py's --rejected CLI already uses
COACH_DEFAULT_THRESHOLD = 40
COACH_TOP_SKILLS = 20

# Resume-extraction quality tripwires. PDF extraction fails in ways a plain
# character count won't show: multi-column layouts interleave text, tables
# scramble, ligatures drop characters silently. None of these are proof of a
# bad extraction on their own - a real resume in plain English is just very
# unlikely to cross them, so a mangled one usually does. The scrollable
# preview next to these warnings is what actually catches everything else.
LONG_WORD_THRESHOLD = 40  # a token this long in normal prose means missing spaces somewhere
MIN_SPACE_RATIO = 0.08  # normal English prose runs ~13-18% spaces; well below that means words got glued together
MAJOR_LENGTH_DROP_RATIO = 0.5  # new extraction under half the current resume's length is worth a second look

MISSING_SKILLS_VISIBLE_COUNT = 8  # some postings list 20+; the rest collapse behind a native <details> disclosure


@dataclass
class DashboardJob:
    job: JobPosting
    content_hash: str
    fit_score: Optional[int]  # None for unscored jobs - never a fabricated number, see is_unscored below
    verdict: str
    matched_skills: list[str]
    missing_skills: list[str]
    years_required: Optional[float]
    resume_meets_it: bool
    reasoning: str
    model: str
    application_status: str
    first_seen: datetime
    is_new: bool
    is_unscored: bool  # True: both matched_skills and missing_skills came back empty - see agents/analyst.py's is_unscored


# How a job's experience requirement stands against the resume. Three
# states, not two, because "no figure was extracted" is genuinely different
# from both "you clear the bar" and "you do not" - the same None-is-not-zero
# rule the rest of this codebase follows.
#
#   "meets"       the Analyst judged the resume meets it (figure or not)
#   "unconfirmed" no figure was extracted AND the Analyst judged it unmet -
#                 eligible on the stated rule (nothing concrete bars you)
#                 but with a real signal against it, so callers sort these
#                 below the confirmed ones rather than hiding or promoting
#   "not_met"     a figure WAS extracted and the resume does not meet it -
#                 the only state the eligibility filter hides
ELIGIBILITY_MEETS = "meets"
ELIGIBILITY_UNCONFIRMED = "unconfirmed"
ELIGIBILITY_NOT_MET = "not_met"


def experience_eligibility(years_required: Optional[float], resume_meets_it: bool) -> str:
    """Classify one job's experience standing. See the constants above.

    Deliberately computed here rather than in the frontend: the Jobs page
    already re-derived a rule once and silently undid the backend's
    ordering (see docs/decisions.md). The backend states the fact; the UI
    filters and groups on it."""
    if resume_meets_it:
        return ELIGIBILITY_MEETS
    if years_required is None:
        return ELIGIBILITY_UNCONFIRMED
    return ELIGIBILITY_NOT_MET


def partition_unscored_by_experience(
    unscored: list[DashboardJob],
) -> tuple[list[DashboardJob], list[DashboardJob]]:
    """Split unscored jobs into (qualifies, rest).

    "Unscored" means the Analyst found no concrete technical requirements to
    compare the resume against, so its fit_score is not a real comparison
    and is never shown or sorted on (see load_dashboard_jobs). That is a
    statement about the SKILLS comparison only. Experience is parsed
    separately by filters.parse_max_experience_years and is unaffected by
    it - so a job that states a requirement AND that the resume meets is
    carrying real, independent positive evidence even though it could not
    be scored.

    Burying such a job below 117 scored ones loses that evidence entirely.
    A Cisco "Software Engineer (Evergreen)" posting stating 0 years, which
    the resume meets, is a genuinely strong candidate that appeared dead
    last purely because the Analyst had nothing to compare.

    This does NOT invent a fit_score - the caller orders these ahead of the
    scored jobs, and they still render as "could not evaluate". Ordering
    says "worth your attention", not "scored highest".

    A job with no stated requirement (years_required is None) does not
    qualify: None means "not stated", never "zero", and must not be read as
    a met requirement. Qualifying jobs are ordered by the requirement
    ascending, so the lowest bar comes first.
    """
    qualifies = [dj for dj in unscored if dj.years_required is not None and dj.resume_meets_it]
    rest = [dj for dj in unscored if not (dj.years_required is not None and dj.resume_meets_it)]
    qualifies.sort(key=lambda dj: dj.years_required)
    return qualifies, rest


def load_dashboard_jobs(
    session: Session, kept: list[JobPosting], last_viewed_cutoff: Optional[datetime]
) -> tuple[list[DashboardJob], list[DashboardJob], int]:
    """Every rule-filter survivor (passed in, not recomputed here - see
    run_filter_pass) with a stage-1 or stage-2 result, split three ways:

    - scored: a real fit_score, sorted descending (stage-2 preferred over
      stage-1 for the same job, never averaged or otherwise combined)
    - unscored: analyzed, but matched_skills AND missing_skills both came
      back empty - the model had no concrete technical requirements to
      compare the resume against (verified against a real case, Broccoli's
      "Software Engineer" posting - the extractor found the right section,
      it genuinely states no concrete technology, see docs/decisions.md).
      fit_score in this state is not a real comparison and must never sort
      or display alongside real scores - kept in its own list, not folded
      into `scored` with a fabricated number.
    - unanalyzed_count: survivors with no AnalystResultRow at all yet
      (orchestrator hasn't reached them), surfaced as a count rather than
      silently dropped.

    Never cached (unlike run_filter_pass) - application_status changes via
    the status selectbox and resume_text changes via the Resume tab both
    flow through here, and both are expected to show up immediately within
    a session. What keeps this fast without caching is avoiding the N+1
    pattern the previous version had: up to 2 separate session.get() calls
    per survivor (one per model) meant 2N queries for N survivors. Every
    stage-1/stage-2 candidate hash is computed up front instead, and every
    matching AnalystResultRow is fetched in a single IN(...) query -
    measured ~5s -> a fraction of that for 37 survivors, see
    docs/decisions.md."""
    if not kept:
        return [], [], 0

    kept_hashes = [job.content_hash for job in kept]
    row_by_hash = {
        row.content_hash: row
        for row in session.query(JobPostingRow).filter(JobPostingRow.content_hash.in_(kept_hashes)).all()
    }

    resume_text = prepare_resume_text()

    job_hashes: dict[str, tuple[str, str]] = {}  # job.content_hash -> (stage2_hash, stage1_hash)
    all_hashes: set[str] = set()
    for job in kept:
        requirements_text, _extracted = extract_jd_requirements(job.description)
        stage2_hash = _text_hash(GEMINI_MODEL_STAGE2, SYSTEM_INSTRUCTION, resume_text, requirements_text)
        stage1_hash = _text_hash(GEMINI_MODEL_STAGE1, SYSTEM_INSTRUCTION, resume_text, requirements_text)
        job_hashes[job.content_hash] = (stage2_hash, stage1_hash)
        all_hashes.add(stage2_hash)
        all_hashes.add(stage1_hash)

    analyst_rows_by_hash = {
        row.text_hash: row
        for row in session.query(AnalystResultRow).filter(AnalystResultRow.text_hash.in_(all_hashes)).all()
    }

    scored: list[DashboardJob] = []
    unscored: list[DashboardJob] = []
    unanalyzed_count = 0

    for job in kept:
        row = row_by_hash[job.content_hash]
        stage2_hash, stage1_hash = job_hashes[job.content_hash]
        analyst_row = analyst_rows_by_hash.get(stage2_hash) or analyst_rows_by_hash.get(stage1_hash)
        if analyst_row is None:
            unanalyzed_count += 1
            continue

        matched_skills = json.loads(analyst_row.matched_skills)
        missing_skills = json.loads(analyst_row.missing_skills)
        # Checked directly against the parsed lists, not against the stored
        # verdict column - the same one-line condition agents/analyst.py's
        # is_unscored uses, just against already-parsed data. Self-verifying
        # regardless of whether an older row's stored verdict was backfilled.
        job_is_unscored = not matched_skills and not missing_skills

        is_new = last_viewed_cutoff is None or row.first_seen > last_viewed_cutoff
        dashboard_job = DashboardJob(
            job=job,
            content_hash=job.content_hash,
            fit_score=None if job_is_unscored else analyst_row.fit_score,
            verdict=analyst_row.verdict,
            matched_skills=matched_skills,
            missing_skills=missing_skills,
            years_required=analyst_row.experience_years_required,
            resume_meets_it=analyst_row.resume_meets_experience,
            reasoning=analyst_row.reasoning,
            model=analyst_row.model,
            application_status=row.application_status,
            first_seen=row.first_seen,
            is_new=is_new,
            is_unscored=job_is_unscored,
        )

        (unscored if job_is_unscored else scored).append(dashboard_job)

    scored.sort(key=lambda dj: dj.fit_score, reverse=True)
    return scored, unscored, unanalyzed_count


def set_application_status(engine, content_hash: str, status: str) -> None:
    """The dashboard's one job-status write path. upsert_job (db.py) already
    never touches application_status on a re-fetch - this is the only code
    that sets it, matching CLAUDE.md's manual-apply model.

    applied_at is set once, the first time status becomes "applied" - never
    overwritten by a later re-click (switching away and back must not reset
    the original application date), and never cleared if status later moves
    away from "applied" to something else - a job you applied to and then
    marked rejected is still a job you applied to. See db.py's
    JobPostingRow.applied_at and the Applied tab, which lists by
    applied_at IS NOT NULL, not by current status."""
    with Session(engine) as session:
        row = session.get(JobPostingRow, content_hash)
        if row is not None:
            row.application_status = status
            if status == "applied" and row.applied_at is None:
                row.applied_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.commit()


def read_last_viewed() -> Optional[datetime]:
    if not LAST_VIEWED_PATH.exists():
        return None
    text = LAST_VIEWED_PATH.read_text(encoding="utf-8").strip()
    return datetime.fromisoformat(text) if text else None


def write_last_viewed(when: datetime) -> None:
    LAST_VIEWED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_VIEWED_PATH.write_text(when.isoformat(), encoding="utf-8")


def format_age(delta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


# ---------------------------------------------------------------------------
# Filter pass - the rule-filtering step shared by the run-stats strip and
# the Jobs/Rejected/Companies tabs. Deliberately kept independent of
# resume text and application_status, and deliberately never constructs a
# full JobPosting (Pydantic validation + content_hash/description_hash
# recomputation) for a row that's just going to be discarded.
# ---------------------------------------------------------------------------


@dataclass
class RejectedJob:
    """Just enough to display and sample from - not a full JobPosting.
    description is never fetched for a rejected row at all (the heaviest
    column per row), since nothing in the Rejected tab has ever shown it."""

    company: str
    title: str
    location: Optional[str]
    reason: str


@dataclass
class FilterPassResult:
    kept: list[JobPosting]  # full objects - only for survivors, ~dozens not thousands
    total_fetched: int
    rejected_by: Counter  # {rule: count}
    fetched_by_company: Counter  # {company: count} - every row, not just survivors
    rejected: list[RejectedJob]  # every rejected job, lightweight - sampled by the Rejected tab

    @property
    def kept_count(self) -> int:
        return len(self.kept)


def run_filter_pass(session: Session, preferences: Optional[Preferences] = None) -> FilterPassResult:
    """Applies filters.reject_reason_for directly against a lightweight
    (content_hash, company, title, location) query - never constructing a
    full JobPosting for a row that's just going to be discarded.
    reject_reason_for only ever reads title and location (never
    description, the heaviest field per row - see filters.py), so this
    reads 4 narrow columns for every row instead of every column, and only
    the surviving content_hashes trigger a full row fetch + JobPosting
    reconstruction afterward.

    preferences: when given, evaluates against this candidate ruleset
    instead of the live config.* values - the Roles tab's "preview impact
    before saving" step calls this directly (uncached, on demand) with the
    in-progress edited lists. Every other caller (the page's own cached
    _cached_filter_pass, tests) leaves this unset and gets the live rules.

    Deliberately independent of resume text and application_status - rule
    filtering depends only on title/location/company, which never change
    except via a nightly fetch (pipeline.py/orchestrator.py, never from
    within this dashboard). That's what makes this specific function safe
    to cache (see __main__'s _cached_filter_pass) where load_dashboard_jobs
    is not: this result cannot go stale from a status change or a resume
    upload, because neither of those inputs ever enters this computation.
    (A preview call with an explicit `preferences` is never itself cached -
    see the Roles tab - so this guarantee isn't at risk from that path.)

    Measured: this was the dominant remaining cost after the
    sentence-transformers import fix - reconstructing all ~7500 rows into
    full JobPosting objects to filter out ~7460 of them, every page load.
    See docs/decisions.md for the before/after numbers."""
    light_rows = session.query(
        JobPostingRow.content_hash, JobPostingRow.company, JobPostingRow.title, JobPostingRow.location
    ).all()

    total_fetched = len(light_rows)
    surviving_hashes: list[str] = []
    rejected_by: Counter = Counter()
    fetched_by_company: Counter = Counter()
    rejected: list[RejectedJob] = []

    for content_hash, company, title, location in light_rows:
        fetched_by_company[company] += 1
        reason = reject_reason_for(title, location, preferences)
        if reason is None:
            surviving_hashes.append(content_hash)
        else:
            rejected_by[reason] += 1
            rejected.append(RejectedJob(company=company, title=title, location=location, reason=reason))

    kept: list[JobPosting] = []
    if surviving_hashes:
        full_rows = session.query(JobPostingRow).filter(JobPostingRow.content_hash.in_(surviving_hashes)).all()
        kept = [job_posting_from_row(row) for row in full_rows]

    return FilterPassResult(
        kept=kept,
        total_fetched=total_fetched,
        rejected_by=rejected_by,
        fetched_by_company=fetched_by_company,
        rejected=rejected,
    )


# ---------------------------------------------------------------------------
# Roles tab - editable rule-filter keyword lists, backed by config.py's
# Preferences / DEFAULT_PREFERENCES / load_preferences / save_preferences /
# apply_preferences. Preview and the post-save impact count both go
# through run_filter_pass/load_dashboard_jobs above via an explicit
# `preferences` override - no separate copy of the filter logic lives here.
# ---------------------------------------------------------------------------

# (Preferences field name, config.py module attribute holding the live
# value, editor label, help text) - one row per editable list.
ROLES_EDITOR_FIELDS: list[tuple[str, str, str, str]] = [
    ("title_allowlist", "TITLE_ALLOWLIST", "Title allowlist", "A job title must contain at least one of these to survive."),
    (
        "seniority_keywords",
        "SENIORITY_KEYWORDS",
        "Seniority keywords (reject)",
        "A title containing any of these is rejected as too senior.",
    ),
    (
        "non_engineering_keywords",
        "NON_ENGINEERING_KEYWORDS",
        "Non-engineering keywords (reject)",
        "A title containing any of these is rejected as not an engineering role.",
    ),
    (
        "india_location_keywords",
        "INDIA_LOCATION_KEYWORDS",
        "India location keywords",
        "A posting's location must contain at least one of these to survive.",
    ),
]


def _clean_keyword_rows(rows: list[dict]) -> list[str]:
    """st.data_editor (num_rows='dynamic') hands back one dict per row,
    including a trailing blank row while the user is mid-edit - stripped,
    emptied, and deduplicated (case-insensitive, first occurrence wins)
    before this counts as a real list. Order preserved, not sorted - a
    re-added entry should land back where typed, not jump to a sorted
    position."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for row in rows:
        value = str(row.get("keyword") or "").strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            cleaned.append(value)
    return cleaned


def render_keyword_list_editor(field_name: str, config_attr: str, label: str, help_text: str) -> list[str]:
    """One add/remove-capable list editor for a single Preferences field,
    backed by st.data_editor's native dynamic-rows mode - no extra
    dependency, no hand-built per-row remove buttons. Verified live
    (AppTest) before relying on this: st.data_editor's *return value* is
    the full current row list on every rerun, not an edit-delta; the
    edit-delta shape lives separately in st.session_state[key] and isn't
    what's used here.

    Seeded from st.session_state["roles_seed_<field_name>"], which
    defaults to the live config.<config_attr> value the first time this
    tab renders in a session, and is only ever overwritten explicitly - by
    the Reset button (to DEFAULT_PREFERENCES) or after a successful Save
    (to the just-saved, cleaned values) - never implicitly by a rerun.

    The widget's own `key` includes st.session_state["roles_editor_
    generation"], bumped by both of those actions: this is what forces
    st.data_editor to re-seed from fresh data instead of replaying
    accumulated edits on top of whatever it first saw. Also verified live:
    deleting a data_editor's own keyed session_state entry to force a
    reset does not work reliably in this Streamlit version (raises
    KeyError even right after that same key was written) - changing the
    key itself is the documented, working way to force a widget to treat
    new data as a fresh seed rather than a diff against what it already
    has."""
    seed_key = f"roles_seed_{field_name}"
    if seed_key not in st.session_state:
        st.session_state[seed_key] = list(getattr(config, config_attr))

    st.markdown(f"**{label}**")
    st.caption(help_text)
    generation = st.session_state.get("roles_editor_generation", 0)
    edited_rows = st.data_editor(
        [{"keyword": v} for v in st.session_state[seed_key]],
        key=f"roles_editor_{field_name}_{generation}",
        num_rows="dynamic",
        hide_index=True,
        column_config={"keyword": st.column_config.TextColumn("Keyword", required=True)},
        width="stretch",
    )
    return _clean_keyword_rows(edited_rows)


# ---------------------------------------------------------------------------
# Run tab - executes orchestrator.run_nightly() with a progress callback the
# UI subscribes to, per pipeline.py's ProgressEvent (see its docstring).
# orchestrator.py (and transitively LangGraph) is NOT imported at module
# level here - measured at ~0.75s on top of this file's own import time,
# the same "don't tax every page load for a feature used by clicking one
# button" principle Task C already applied to sentence-transformers (see
# docs/decisions.md) - imported lazily inside the button's click handler in
# __main__ instead.
# ---------------------------------------------------------------------------


def _format_run_progress_message(event) -> str:
    """Maps a pipeline.ProgressEvent to the short line this tab shows -
    matching pipeline.py's own CLI wording where the two describe the same
    moment (see ProgressEvent's docstring), phrased for a live status
    label rather than a scrolling log. Agents named explicitly ("Analyst"),
    not just "stage 1"/"stage 2"."""
    count_suffix = f" ({event.current}/{event.total})" if event.current is not None else ""
    if event.stage == "fetch":
        return f"Fetching from {event.total} companies...{count_suffix}"
    if event.stage == "filter":
        if "kept" in event.extra:
            return f"Filtering... {event.extra['kept']} of {event.extra['total']} survived."
        return "Filtering..."
    if event.stage == "stage1":
        return f"Analyst: scoring {event.total} job(s)...{count_suffix}"
    if event.stage == "stage2":
        return f"Analyst (deep pass): re-checking top {event.total}...{count_suffix}"
    return event.message  # unrecognized stage - show whatever pipeline.py sent rather than nothing


def make_run_progress_handler(status, quota_box) -> tuple[Callable, dict]:
    """Returns (handler, quota_state). quota_state is a plain dict the
    handler mutates in place (per-stage {call_count, rpd}) so the caller
    can read the final tally after the run completes without a second
    return channel - the handler itself returns nothing, matching
    pipeline.ProgressCallback's signature.

    status.update(label=...) fires on every event (cheap - a label swap);
    a line is written to the status body only the first time each stage is
    seen, so a 41-job stage-1 pass doesn't flood the box with 41 lines -
    the live label already carries the count. Verified live (AppTest)
    before relying on this: status.write()/status.update() called from a
    plain helper function holding `status` as a parameter - not lexically
    inside the `with st.status(...) as status:` block - still correctly
    target that status container, since Streamlit's "current container"
    tracking is dynamic-scope (thread-local), not lexical. See
    docs/decisions.md."""
    seen_stages: set[str] = set()
    quota_state: dict[str, dict] = {}

    def handler(event) -> None:
        message = _format_run_progress_message(event)
        status.update(label=message)
        if event.stage not in seen_stages:
            seen_stages.add(event.stage)
            status.write(message)

        call_count = event.extra.get("call_count")
        if call_count is not None:
            quota_state[event.stage] = {"call_count": call_count, "rpd": event.extra.get("rpd")}
            quota_lines = [
                (f"{stage}: {info['call_count']}/{info['rpd']}" if info["rpd"] else f"{stage}: {info['call_count']} calls")
                for stage, info in quota_state.items()
            ]
            quota_box.caption("Quota used - " + " | ".join(quota_lines))

    return handler, quota_state


# ---------------------------------------------------------------------------
# Applied tab
# ---------------------------------------------------------------------------


@dataclass
class AppliedJob:
    company: str
    title: str
    location: Optional[str]
    url: str
    applied_at: datetime
    application_status: str  # current status - may have moved on from "applied" since, see load_applied_jobs


def load_applied_jobs(session: Session) -> list[AppliedJob]:
    """Everything with applied_at set, most recent first - listed by
    applied_at IS NOT NULL, not by current application_status == "applied",
    so a job later marked "rejected" (an outcome, not an undo) still shows
    up here with that status attached. See set_application_status's
    docstring for why applied_at is never cleared by a later status
    change."""
    rows = (
        session.query(JobPostingRow)
        .filter(JobPostingRow.applied_at.isnot(None))
        .order_by(JobPostingRow.applied_at.desc())
        .all()
    )
    return [
        AppliedJob(
            company=row.company,
            title=row.title,
            location=row.location,
            url=row.url,
            applied_at=row.applied_at,
            application_status=row.application_status,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Companies tab
# ---------------------------------------------------------------------------


@dataclass
class CompanyStats:
    name: str
    ats: str
    token: str
    jobs_fetched: int
    survivors: int


def compute_company_stats(
    companies: list[CompanyConfig], fetched_by_company: Counter, kept: list[JobPosting]
) -> list[CompanyStats]:
    """Jobs fetched and rule-filter survivors per company, matched by exact
    name - the same string every adapter already writes into
    JobPosting.company (pipeline.py's FETCHERS calls fetch(company.name,
    company.token)). A company with fetched > 0 and survivors == 0
    contributed nothing to the pipeline despite being fetched every night -
    exactly what this view exists to surface. fetched_by_company comes
    from run_filter_pass's lightweight pass, not from materializing every
    fetched job just to count them."""
    survivor_counts = Counter(job.company for job in kept)
    return [
        CompanyStats(
            name=company.name,
            ats=company.ats.value,
            token=company.token,
            jobs_fetched=fetched_by_company.get(company.name, 0),
            survivors=survivor_counts.get(company.name, 0),
        )
        for company in companies
    ]


# ---------------------------------------------------------------------------
# Resume tab
# ---------------------------------------------------------------------------


def check_resume_extraction_quality(text: str, previous_text: Optional[str]) -> list[str]:
    """Tripwires for a garbled PDF extraction - see module docstring for
    why a character count alone can't catch this. Returns human-readable
    warnings, empty if nothing looks wrong. Not proof of a clean
    extraction, just the failure modes cheap enough to check for
    automatically."""
    warnings: list[str] = []

    words = text.split()
    if words:
        longest = max(words, key=len)
        if len(longest) > LONG_WORD_THRESHOLD:
            long_count = sum(1 for w in words if len(w) > LONG_WORD_THRESHOLD)
            warnings.append(
                f"{long_count} unusually long word(s) found (longest is {len(longest)} characters, "
                f"starts {longest[:30]!r}) - likely missing spaces from a multi-column layout or table."
            )

    if text:
        space_ratio = text.count(" ") / len(text)
        if space_ratio < MIN_SPACE_RATIO:
            warnings.append(
                f"Space-to-character ratio is {space_ratio:.1%} (normal prose runs ~13-18%) - "
                f"words may have been glued together during extraction."
            )

    if previous_text and len(text) < len(previous_text) * MAJOR_LENGTH_DROP_RATIO:
        warnings.append(
            f"Extracted text is {len(text):,} characters, down from {len(previous_text):,} in the "
            f"current resume.txt ({len(text) / len(previous_text):.0%} of the previous length) - "
            f"content may have been dropped."
        )

    return warnings


def extract_pdf_text(file_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


# ---------------------------------------------------------------------------
# Visual layer - custom HTML/CSS rendered via st.markdown(unsafe_allow_html=
# True), not a rewrite of the data logic above. Data-testid selectors below
# were verified against the installed streamlit==1.61.0 package's own static
# JS bundle (grepped directly, not assumed from memory - they're not a
# documented-stable public API and do change across versions). Card/chip/
# stat colours are this project's own CSS custom properties, not Streamlit's
# internal theme variables - none of those could be confirmed present in
# this version by the same static check, so depending on them would have
# been a guess. The trade-off: these custom elements follow the OS-level
# prefers-color-scheme, not Streamlit's own light/dark toggle, which can in
# principle diverge from it.
#
# All dynamic text (job titles, company names, LLM-generated skills/
# reasoning) is escaped via _esc before going into hand-built HTML - none of
# it is trusted input once unsafe_allow_html is in play.
# ---------------------------------------------------------------------------

PAGE_CSS = """
<style>
/* Hide Streamlit's default chrome (menu, header, footer, toolbar). ONLY the
   data-testid attributes confirmed present in this project's installed
   streamlit==1.61.0 (grepped directly from the static JS bundle, not
   assumed) - the bare #MainMenu/header/footer selectors from an earlier
   version of this file were removed after a real bug: a plain `header` tag
   selector isn't scoped to Streamlit's own chrome at all, and can match
   ANY <header> element the page happens to render (a title block, a tab
   panel's own semantic wrapper, anything) - `visibility: hidden` on a
   wrapper hides every descendant too, which is the leading suspect for the
   invisible-title bug this replaced. See docs/decisions.md. */
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stMainMenu"],
[data-testid="stDecoration"] {
    visibility: hidden;
    height: 0;
}

/* Hiding the header above does NOT reclaim the space it reserved - that
   space lives separately, as top/bottom padding on the block-container
   itself (measured live against this installed streamlit version:
   [data-testid="stMainBlockContainer"] carries padding-top: 96px,
   padding-bottom: 160px, by default - reserved so page content clears the
   header bar and never look pinned to the very bottom edge - regardless of
   whether that header is visually hidden). Confirmed via a real headless
   Chromium render, not just read from source: with only the chrome-hiding
   rule above and no fix here, the "CareerPilot" <h1> sits ~112px down from
   the true top of the viewport - dead space, not content. Reduced to a
   small deliberate value on both sides rather than 0 - "near the top", not
   pinned to the literal edge. Uses the same verified data-testid selector
   convention as the chrome-hiding rule above, not the co-existing
   `.block-container` class alias - data-testid is documented as the
   stable-across-versions one; a bare class name (especially the
   hash-suffixed st-emotion-cache-* siblings on the same element) is more
   likely to shift on a streamlit upgrade. */
[data-testid="stMainBlockContainer"] {
    padding-top: 1.5rem;
    padding-bottom: 2rem;
}

/* ---- Design tokens: one accent colour, restrained verdict tints ---- */
:root {
    --cp-accent: #4361EE;
    --cp-accent-soft: #EEF1FF;
    --cp-text: #1A1D29;
    --cp-text-muted: #6B7280;
    --cp-border: #E5E7EB;
    --cp-bg-card: #FFFFFF;
    --cp-bg-page: #F8F9FB;

    --cp-strong-bg: #EAF7F1; --cp-strong-border: #57AB8C; --cp-strong-text: #1F6E52;
    --cp-possible-bg: #FBF3E4; --cp-possible-border: #C99A4C; --cp-possible-text: #8A6423;
    --cp-weak-bg: #F5EFEE; --cp-weak-border: #B08A85; --cp-weak-text: #7A5750;
    --cp-unscored-bg: #F1F2F4; --cp-unscored-border: #9CA3AF; --cp-unscored-text: #4B5563;
}
@media (prefers-color-scheme: dark) {
    :root {
        --cp-accent: #7C96FF;
        --cp-accent-soft: #232A4D;
        --cp-text: #E7E9EE;
        --cp-text-muted: #9AA1B2;
        --cp-border: #2E3244;
        --cp-bg-card: #1B1F2E;
        --cp-bg-page: #12141C;

        --cp-strong-bg: #163A2E; --cp-strong-border: #4F9E80; --cp-strong-text: #8FDCBB;
        --cp-possible-bg: #3A2F17; --cp-possible-border: #C99A4C; --cp-possible-text: #E4C07E;
        --cp-weak-bg: #332523; --cp-weak-border: #8C6B65; --cp-weak-text: #D2ADA6;
        --cp-unscored-bg: #262A38; --cp-unscored-border: #6B7280; --cp-unscored-text: #C3C8D3;
    }
}

/* ---- Base type scale ---- */
html, body, [class*="css"] { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; }
h1 { font-size: 1.9rem !important; font-weight: 700 !important; letter-spacing: -0.02em; }
h2 { font-size: 1.3rem !important; font-weight: 600 !important; }
h3 { font-size: 1.05rem !important; font-weight: 600 !important; }

/* No page-background override here on purpose (removed - see
   docs/decisions.md). Streamlit's own theme (light/dark, chosen by the
   user or the browser, independent of this CSS) governs its own native
   elements' text colour; forcing only ONE half of a background/text pair
   on a native container this project doesn't fully control was the other
   leading suspect for invisible text - if Streamlit's real theme ever
   disagreed with what this override assumed, native text could end up
   rendered against a background it was never designed to sit on. Native
   Streamlit chrome (the page background included) is left alone entirely;
   colour is only ever applied to elements this file builds itself
   (.cp-card, .cp-chip, etc.), which always set background and text
   together from the same synced variable pair, so they can never drift out
   of sync with each other the way a native/custom mismatch could. */

/* ---- Tabs: styled without ever touching native text colour, for the same
   reason - only a border, which either shows or doesn't and has no
   "blends into an unexpected background" failure mode the way text colour
   does. Applied directly on button[role="tab"][aria-selected="true"] (a
   selector already confirmed working - font-weight differences between
   active/inactive tabs were rendering correctly, only colour was broken -
   not on a guessed-at internal "highlight" sub-element: an earlier version
   of this rule targeted [data-baseweb="tab-highlight"], which doesn't
   exist anywhere in this installed streamlit version's JS bundle (checked
   directly) - dead CSS matching nothing, the same unverified-selector
   mistake this whole file is otherwise careful about. See
   docs/decisions.md. */
[data-testid="stTabs"] button[role="tab"] { font-weight: 500; }
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
    font-weight: 700;
    border-bottom: 3px solid var(--cp-accent);
}

/* ---- Status selectbox next to each card - narrowed so it doesn't look
   like a full-width, unrelated widget floating under the card ---- */
[data-testid="stSelectbox"] { max-width: 220px; }

/* ---- Stats strip: a funnel, not four bare numbers ---- */
.cp-stats-strip {
    display: flex; align-items: center; gap: 0.5rem;
    background: var(--cp-bg-card); border: 1px solid var(--cp-border);
    border-radius: 12px; padding: 1rem 1.25rem; margin-bottom: 1rem;
}
.cp-stat { flex: 1; text-align: center; min-width: 0; }
.cp-stat-value { font-size: 1.6rem; font-weight: 700; color: var(--cp-text); line-height: 1.1; }
.cp-stat-label { font-size: 0.72rem; color: var(--cp-text-muted); margin-top: 2px; text-transform: uppercase; letter-spacing: 0.03em; }
.cp-stat-sub { font-size: 0.72rem; color: var(--cp-accent); margin-top: 2px; }
.cp-stat-arrow { color: var(--cp-border); font-size: 1.3rem; flex: 0 0 auto; }

/* ---- Job cards ---- */
.cp-card {
    background: var(--cp-bg-card); border: 1px solid var(--cp-border);
    border-left: 4px solid var(--cp-unscored-border);
    border-radius: 10px; padding: 1rem 1.25rem 0.6rem; margin-bottom: 0.5rem;
}
.cp-card.cp-verdict-strong { border-left-color: var(--cp-strong-border); }
.cp-card.cp-verdict-possible { border-left-color: var(--cp-possible-border); }
.cp-card.cp-verdict-weak { border-left-color: var(--cp-weak-border); }
.cp-card.cp-verdict-unscored { border-left-color: var(--cp-unscored-border); }

.cp-card-header { display: flex; align-items: flex-start; gap: 0.75rem; }
.cp-score-badge {
    flex-shrink: 0; width: 3rem; height: 3rem; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.15rem; font-weight: 700;
}
.cp-verdict-strong .cp-score-badge { background: var(--cp-strong-bg); color: var(--cp-strong-text); border: 2px solid var(--cp-strong-border); }
.cp-verdict-possible .cp-score-badge { background: var(--cp-possible-bg); color: var(--cp-possible-text); border: 2px solid var(--cp-possible-border); }
.cp-verdict-weak .cp-score-badge { background: var(--cp-weak-bg); color: var(--cp-weak-text); border: 2px solid var(--cp-weak-border); }
.cp-verdict-unscored .cp-score-badge { background: var(--cp-unscored-bg); color: var(--cp-unscored-text); border: 2px solid var(--cp-unscored-border); font-size: 1.4rem; }

.cp-card-title-block { flex: 1; min-width: 0; }
.cp-card-title { font-size: 1.05rem; font-weight: 600; color: var(--cp-text); }
.cp-card-company { font-size: 0.9rem; color: var(--cp-text-muted); }
.cp-new-badge {
    display: inline-block; background: var(--cp-accent-soft); color: var(--cp-accent);
    font-size: 0.68rem; font-weight: 700; padding: 0.1rem 0.45rem; border-radius: 999px;
    margin-left: 0.4rem; vertical-align: middle;
}
.cp-verdict-tag {
    flex-shrink: 0; font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.03em; padding: 0.2rem 0.55rem; border-radius: 999px;
}
.cp-verdict-strong .cp-verdict-tag { background: var(--cp-strong-bg); color: var(--cp-strong-text); }
.cp-verdict-possible .cp-verdict-tag { background: var(--cp-possible-bg); color: var(--cp-possible-text); }
.cp-verdict-weak .cp-verdict-tag { background: var(--cp-weak-bg); color: var(--cp-weak-text); }
.cp-verdict-unscored .cp-verdict-tag { background: var(--cp-unscored-bg); color: var(--cp-unscored-text); }

.cp-card-meta { font-size: 0.8rem; color: var(--cp-text-muted); margin: 0.5rem 0 0.6rem; }
.cp-card-reasoning { font-size: 0.9rem; color: var(--cp-text); margin-bottom: 0.7rem; line-height: 1.45; }

.cp-chip-section { margin-bottom: 0.5rem; }
.cp-chip-label { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--cp-text-muted); margin-bottom: 0.3rem; }
.cp-chip-row { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.cp-chip { display: inline-block; font-size: 0.78rem; padding: 0.15rem 0.6rem; border-radius: 999px; border: 1px solid transparent; }
.cp-chip-matched { background: var(--cp-strong-bg); color: var(--cp-strong-text); border-color: var(--cp-strong-border); }
.cp-chip-missing { background: var(--cp-weak-bg); color: var(--cp-weak-text); border-color: var(--cp-weak-border); }
.cp-chip-empty { color: var(--cp-text-muted); font-size: 0.8rem; font-style: italic; }

.cp-chip-more summary { cursor: pointer; font-size: 0.78rem; color: var(--cp-accent); margin-top: 0.3rem; list-style: none; }
.cp-chip-more summary::-webkit-details-marker { display: none; }
.cp-chip-more[open] summary { margin-bottom: 0.35rem; }

.cp-apply-link { display: inline-block; margin-top: 0.4rem; font-size: 0.85rem; font-weight: 600; color: var(--cp-accent); text-decoration: none; }
.cp-apply-link:hover { text-decoration: underline; }

/* ---- Empty states: say something useful, not blank space ---- */
.cp-empty-state {
    text-align: center; padding: 2.5rem 1rem; color: var(--cp-text-muted);
    background: var(--cp-bg-card); border: 1px dashed var(--cp-border); border-radius: 12px;
}
.cp-empty-state-title { font-weight: 600; color: var(--cp-text); margin-bottom: 0.3rem; }
</style>
"""


def _esc(value) -> str:
    """HTML-escape any dynamic text before interpolating into hand-built
    card HTML - job titles/company names come from external postings and
    matched/missing skills come from LLM output, neither fully trusted.
    unsafe_allow_html=True means this project, not Streamlit, is
    responsible for escaping."""
    return html.escape(str(value), quote=True)


def _verdict_css_class(verdict: str) -> str:
    return verdict if verdict in ("strong", "possible", "weak") else "unscored"


def render_matched_chips_html(skills: list[str]) -> str:
    if not skills:
        return '<div class="cp-chip-empty">No matched skills</div>'
    chips = "".join(f'<span class="cp-chip cp-chip-matched">{_esc(s)}</span>' for s in skills)
    return f'<div class="cp-chip-row">{chips}</div>'


def render_missing_chips_html(skills: list[str], visible_count: int = MISSING_SKILLS_VISIBLE_COUNT) -> str:
    """First `visible_count` chips inline, the rest behind a native
    <details> disclosure - no JavaScript or custom component needed, plain
    HTML works fine inside unsafe_allow_html markdown."""
    if not skills:
        return '<div class="cp-chip-empty">Nothing stated as missing</div>'
    visible, rest = skills[:visible_count], skills[visible_count:]
    visible_chips = "".join(f'<span class="cp-chip cp-chip-missing">{_esc(s)}</span>' for s in visible)
    parts = [f'<div class="cp-chip-row">{visible_chips}</div>']
    if rest:
        rest_chips = "".join(f'<span class="cp-chip cp-chip-missing">{_esc(s)}</span>' for s in rest)
        parts.append(
            f'<details class="cp-chip-more"><summary>+{len(rest)} more</summary>'
            f'<div class="cp-chip-row">{rest_chips}</div></details>'
        )
    return "".join(parts)


def render_job_card_html(dj: DashboardJob) -> str:
    """Full card for a scored job - fit score as a colour-coded badge (not
    a number in a heading), verdict tag, skills as pill chips with matched/
    missing visually distinct. Assumes dj.is_unscored is False - the caller
    dispatches unscored jobs to render_unscored_card_html instead, which
    has no score to show and shouldn't render two empty chip sections."""
    css_class = _verdict_css_class(dj.verdict)
    badge = f'<div class="cp-score-badge">{dj.fit_score}</div>'

    new_badge = '<span class="cp-new-badge">NEW</span>' if dj.is_new else ""
    title_block = (
        '<div class="cp-card-title-block">'
        f'<div class="cp-card-title">{_esc(dj.job.title)}{new_badge}</div>'
        f'<div class="cp-card-company">{_esc(dj.job.company)}</div>'
        "</div>"
    )
    verdict_tag = f'<div class="cp-verdict-tag">{_esc(dj.verdict)}</div>'

    location = dj.job.location or "-"
    if dj.years_required is not None:
        exp = f"{dj.years_required:g} yrs required · resume meets it: {'yes' if dj.resume_meets_it else 'no'}"
    else:
        exp = "experience not stated"
    meta = f'<div class="cp-card-meta">{_esc(location)} · {_esc(exp)} · scored by {_esc(dj.model)}</div>'

    reasoning = f'<div class="cp-card-reasoning">{_esc(dj.reasoning)}</div>'

    matched_section = (
        '<div class="cp-chip-section"><div class="cp-chip-label">Matched</div>'
        f"{render_matched_chips_html(dj.matched_skills)}</div>"
    )
    missing_section = (
        '<div class="cp-chip-section"><div class="cp-chip-label">Missing</div>'
        f"{render_missing_chips_html(dj.missing_skills)}</div>"
    )

    apply_link = f'<a class="cp-apply-link" href="{_esc(str(dj.job.url))}" target="_blank">Apply →</a>'

    return (
        f'<div class="cp-card cp-verdict-{css_class}">'
        f'<div class="cp-card-header">{badge}{title_block}{verdict_tag}</div>'
        f"{meta}{reasoning}{matched_section}{missing_section}{apply_link}"
        "</div>"
    )


def render_unscored_card_html(dj: DashboardJob) -> str:
    """Simpler card for a posting with no concrete technical requirements
    to compare against the resume (see agents/analyst.py's is_unscored) -
    no score number, no empty chip sections, just the explanation. A "?"
    badge, not a 0 or a blank, so it never reads as a real low score."""
    new_badge = '<span class="cp-new-badge">NEW</span>' if dj.is_new else ""
    title_block = (
        '<div class="cp-card-title-block">'
        f'<div class="cp-card-title">{_esc(dj.job.title)}{new_badge}</div>'
        f'<div class="cp-card-company">{_esc(dj.job.company)}</div>'
        "</div>"
    )
    location = dj.job.location or "-"
    meta = f'<div class="cp-card-meta">{_esc(location)} · analyzed by {_esc(dj.model)}</div>'
    reasoning = f'<div class="cp-card-reasoning">{_esc(dj.reasoning)}</div>'
    apply_link = f'<a class="cp-apply-link" href="{_esc(str(dj.job.url))}" target="_blank">Apply →</a>'

    return (
        '<div class="cp-card cp-verdict-unscored">'
        f'<div class="cp-card-header"><div class="cp-score-badge">?</div>{title_block}'
        '<div class="cp-verdict-tag">no comparison</div></div>'
        f"{meta}{reasoning}"
        '<div class="cp-chip-empty">No technical requirements were extracted from this posting - '
        "there was nothing concrete to compare against your resume.</div>"
        f"{apply_link}"
        "</div>"
    )


def render_stats_strip_html(total_fetched: int, survived: int, analyzed: int, unanalyzed: int) -> str:
    """The run-stats funnel as a dashboard summary: each stage's share of
    the stage before it, so the drop-off is visible at a glance instead of
    four unrelated numbers."""
    survive_pct = f"{survived / total_fetched * 100:.1f}% of fetched" if total_fetched else ""
    analyzed_pct = f"{analyzed / survived * 100:.0f}% of survivors" if survived else ""

    def stat(value: int, label: str, sub: str = "") -> str:
        sub_html = f'<div class="cp-stat-sub">{_esc(sub)}</div>' if sub else ""
        return (
            f'<div class="cp-stat"><div class="cp-stat-value">{value:,}</div>'
            f'<div class="cp-stat-label">{_esc(label)}</div>{sub_html}</div>'
        )

    arrow = '<div class="cp-stat-arrow">›</div>'
    return (
        '<div class="cp-stats-strip">'
        + stat(total_fetched, "Fetched")
        + arrow
        + stat(survived, "Survived filters", survive_pct)
        + arrow
        + stat(analyzed, "Analyzed", analyzed_pct)
        + arrow
        + stat(unanalyzed, "Not yet analyzed")
        + "</div>"
    )


def render_empty_state_html(title: str, body: str) -> str:
    return (
        '<div class="cp-empty-state">'
        f'<div class="cp-empty-state-title">{_esc(title)}</div>'
        f"<div>{_esc(body)}</div>"
        "</div>"
    )


if __name__ == "__main__":
    import streamlit as st

    st.set_page_config(page_title="CareerPilot", page_icon="\U0001f9ed", layout="wide")
    st.markdown(PAGE_CSS, unsafe_allow_html=True)
    st.title("CareerPilot")

    # Surfaced on every page load, not just inside the Roles tab - a
    # fallback here means the rule filters are running on
    # DEFAULT_PREFERENCES instead of whatever's actually in
    # data/preferences.json, which affects every tab's numbers, not just
    # Roles. See config.py's DEFAULT_PREFERENCES for why this specific
    # failure mode (most dangerously an emptied title_allowlist passing
    # every job through unfiltered) is worth a visible warning rather than
    # a silent fallback.
    for warning_text in config.PREFERENCES_LOAD_WARNINGS:
        st.warning(f"Preferences: {warning_text}")

    # Read the previous "last viewed" marker once per session (not on every
    # rerun a widget interaction triggers) so NEW badges stay stable while
    # filtering/setting status, then bump the marker to now - so a job that
    # shows as new this morning won't still say NEW tomorrow, but also
    # won't disappear mid-session just because a filter changed.
    if "last_viewed_cutoff" not in st.session_state:
        st.session_state["last_viewed_cutoff"] = read_last_viewed()
        write_last_viewed(datetime.now(timezone.utc).replace(tzinfo=None))
    cutoff = st.session_state["last_viewed_cutoff"]

    # Cached wrapper around run_filter_pass - safe because run_filter_pass
    # never reads application_status or resume text (see its docstring), so
    # nothing that happens inside a session (a status change, a resume
    # save) can make this cached result stale. `_session` (underscore
    # prefix) is Streamlit's documented convention for excluding an
    # argument from the cache key - verified live before relying on it
    # (see docs/decisions.md) - so this caches on nothing, i.e. one shared
    # result per TTL window rather than a real per-argument cache. TTL is
    # a safety net for the DB changing from outside this session (a manual
    # orchestrator.py run in another terminal), not something expected to
    # matter in normal use, since the dashboard itself never fetches.
    #
    # load_dashboard_jobs deliberately has no such wrapper - it depends on
    # both application_status and resume text, exactly the two things that
    # must never be served stale from a cache within a session.
    @st.cache_data(ttl=300, show_spinner=False)
    def _cached_filter_pass(_session: Session) -> FilterPassResult:
        return run_filter_pass(_session)

    engine = get_engine()
    with Session(engine) as session:
        last_run = session.query(func.max(JobPostingRow.last_seen)).scalar()

        # Computed once here, shared across the run-stats strip and every
        # tab below - Streamlit reruns every tab body on every interaction,
        # so without sharing this, the filter pass would redo its own work
        # up to 4 times per page load even with the cache in place.
        filter_result = _cached_filter_pass(session)
        kept = filter_result.kept
        rejected_by = filter_result.rejected_by

        dashboard_jobs, unscored_jobs, unanalyzed_count = load_dashboard_jobs(session, kept, cutoff)
        analyzed_count = len(dashboard_jobs) + len(unscored_jobs)

        companies = load_companies()
        company_stats = compute_company_stats(companies, filter_result.fetched_by_company, kept)

        if last_run is not None:
            age = datetime.now(timezone.utc).replace(tzinfo=None) - last_run
            st.caption(
                f"Data as of {last_run:%Y-%m-%d %H:%M} UTC ({format_age(age)} ago) - run orchestrator.py for fresh data."
            )
        else:
            st.caption("No data yet - run orchestrator.py or pipeline.py first.")

        # --- run-stats funnel, always visible, outside the tabs ---
        st.markdown(
            render_stats_strip_html(filter_result.total_fetched, len(kept), analyzed_count, unanalyzed_count),
            unsafe_allow_html=True,
        )

        if rejected_by:
            with st.expander(f"Rejection counts by rule ({sum(rejected_by.values())} total)"):
                for reason, count in sorted(rejected_by.items(), key=lambda pair: pair[1], reverse=True):
                    st.write(f"**{reason}**: {count}")

        tab_jobs, tab_run, tab_roles, tab_resume, tab_applied, tab_rejected, tab_companies, tab_coach = st.tabs(
            ["Jobs", "Run", "Roles", "Resume", "Applied", "Rejected", "Companies", "Coach"]
        )

        # -----------------------------------------------------------------
        # Jobs tab
        # -----------------------------------------------------------------
        with tab_jobs:
            status_filter = st.multiselect("Status", STATUSES, default=["new", "interviewing"], key="jobs_status_filter")

            filtered = [dj for dj in dashboard_jobs if dj.application_status in status_filter]
            shown = filtered[:TOP_N]
            unscored_shown = [dj for dj in unscored_jobs if dj.application_status in status_filter]

            st.write(f"Showing {len(shown)} of {len(filtered)} matching status filter ({len(dashboard_jobs)} scored total)")
            if unanalyzed_count:
                st.caption(f"{unanalyzed_count} rule-filter survivor(s) not yet analyzed - run orchestrator.py to score them.")

            def _render_status_control(dj: DashboardJob) -> None:
                status_key = f"status_{dj.content_hash}"

                def _on_change(engine=engine, content_hash=dj.content_hash, key=status_key):
                    set_application_status(engine, content_hash, st.session_state[key])

                st.selectbox(
                    "Application status",
                    STATUSES,
                    index=STATUSES.index(dj.application_status),
                    key=status_key,
                    on_change=_on_change,
                    label_visibility="collapsed",
                )

            if not shown and not unscored_shown:
                if unanalyzed_count:
                    body = (
                        f"Try widening the status filter above, or check back after the next orchestrator run - "
                        f"{unanalyzed_count} survivor(s) haven't been analyzed yet."
                    )
                else:
                    body = "Try widening the status filter above - everything currently scored is filtered out."
                st.markdown(render_empty_state_html("Nothing matches this filter", body), unsafe_allow_html=True)

            for dj in shown:
                st.markdown(render_job_card_html(dj), unsafe_allow_html=True)
                _render_status_control(dj)

            if unscored_shown:
                st.subheader("Could not evaluate")
                st.caption(
                    "No technical requirements extracted from these postings - the analysis had nothing concrete to "
                    "compare against your resume, so there's no real fit_score to show, not a low one."
                )
                for dj in unscored_shown:
                    st.markdown(render_unscored_card_html(dj), unsafe_allow_html=True)
                    _render_status_control(dj)

        # -----------------------------------------------------------------
        # Run tab
        # -----------------------------------------------------------------
        with tab_run:
            st.write("Runs the same nightly pipeline orchestrator.py runs on its own schedule - fetch, filter, then Analyst.")
            st.warning(
                "Don't run this while a batch Scout run is active in another terminal - both spend the same "
                "Gemini free-tier quota and will slow each other down via rate-limit backoff, not fail outright."
            )

            if "run_result_message" in st.session_state:
                run_outcome = st.session_state.pop("run_result_message")
                (st.error if run_outcome[0] == "error" else st.success)(run_outcome[1])

            if st.button("Run orchestrator now", key="run_orchestrator_button"):
                # Deferred import - see the module-level comment above
                # make_run_progress_handler for why orchestrator.py (and
                # LangGraph) is never imported at the top of this file.
                import orchestrator

                with st.status("Starting orchestrator...", expanded=True) as status:
                    quota_box = status.empty()
                    handler, _quota_state = make_run_progress_handler(status, quota_box)
                    try:
                        result = orchestrator.run_nightly(on_progress=handler)
                    except Exception as exc:  # noqa: BLE001 - must reach the UI, not crash the dashboard
                        status.update(label=f"Failed: {exc}", state="error")
                        st.session_state["run_result_message"] = ("error", f"Orchestrator run failed: {exc}")
                    else:
                        final_hashes = result.get("stage2_hashes") or result.get("stage1_ranked_hashes") or []
                        final_rows = (
                            session.query(JobPostingRow).filter(JobPostingRow.content_hash.in_(final_hashes)).all()
                            if final_hashes
                            else []
                        )
                        final_jobs = [job_posting_from_row(row) for row in final_rows]
                        final_scored, _final_unscored, _ = load_dashboard_jobs(session, final_jobs, cutoff)
                        strong_count = sum(1 for dj in final_scored if dj.verdict == "strong")
                        summary = f"Done - {len(final_jobs)} job(s) analyzed, {strong_count} strong match(es)."
                        status.update(label=summary, state="complete")
                        st.session_state["run_result_message"] = ("success", summary)

                # _cached_filter_pass was already called for this script run,
                # above - a fresh fetch/analysis just changed the database
                # out from under that stale result, same reasoning as the
                # Roles tab's Save button.
                _cached_filter_pass.clear()
                st.rerun()

        # -----------------------------------------------------------------
        # Roles tab
        # -----------------------------------------------------------------
        with tab_roles:
            st.write(
                "Edit the rule-filter keyword lists directly - add or remove entries, preview the impact "
                "on survivor count, then save. Saved changes take effect immediately, in this session."
            )

            edited_by_field: dict[str, list[str]] = {}
            for field_name, config_attr, label, help_text in ROLES_EDITOR_FIELDS:
                edited_by_field[field_name] = render_keyword_list_editor(field_name, config_attr, label, help_text)
                st.divider()

            empty_labels = [
                label for (field_name, _attr, label, _help) in ROLES_EDITOR_FIELDS if not edited_by_field[field_name]
            ]
            if empty_labels:
                st.error(
                    f"{', '.join(empty_labels)} {'is' if len(empty_labels) == 1 else 'are'} empty - preview and "
                    f"save are disabled. An empty list here would match nothing (allowlist/location keywords) or "
                    f"reject nothing (seniority/non-engineering keywords), silently unfiltering every job - never "
                    f"saved without confirmation. Use Reset to defaults to recover instead of hand-fixing this."
                )

            col_preview, col_save, col_reset = st.columns(3)

            if col_preview.button("Preview impact", key="roles_preview_button", disabled=bool(empty_labels)):
                candidate = Preferences(**edited_by_field)
                preview_result = run_filter_pass(session, candidate)
                st.session_state["roles_preview"] = {"current": filter_result.kept_count, "new": preview_result.kept_count}

            if col_save.button("Save changes", key="roles_save_button", disabled=bool(empty_labels)):
                candidate = Preferences(**edited_by_field)
                config.save_preferences(candidate)
                config.apply_preferences(candidate)
                _cached_filter_pass.clear()  # stale after this - see run_filter_pass's docstring

                saved_result = run_filter_pass(session)  # live config == candidate now
                _, _, saved_unanalyzed_count = load_dashboard_jobs(session, saved_result.kept, cutoff)

                for field_name, _attr, _label, _help in ROLES_EDITOR_FIELDS:
                    st.session_state[f"roles_seed_{field_name}"] = getattr(candidate, field_name)
                st.session_state["roles_editor_generation"] = st.session_state.get("roles_editor_generation", 0) + 1
                st.session_state.pop("roles_preview", None)
                st.session_state["roles_save_message"] = (
                    f"Saved. {saved_result.kept_count} job(s) now survive the filters "
                    f"({saved_unanalyzed_count} not yet analyzed - run orchestrator.py to score them)."
                )
                st.rerun()  # _cached_filter_pass was already called for this run, above - see its docstring

            with col_reset:
                if st.button("Reset to defaults", key="roles_reset_button"):
                    config.save_preferences(config.DEFAULT_PREFERENCES)
                    config.apply_preferences(config.DEFAULT_PREFERENCES)
                    _cached_filter_pass.clear()

                    for field_name, _attr, _label, _help in ROLES_EDITOR_FIELDS:
                        st.session_state[f"roles_seed_{field_name}"] = getattr(config.DEFAULT_PREFERENCES, field_name)
                    st.session_state["roles_editor_generation"] = st.session_state.get("roles_editor_generation", 0) + 1
                    st.session_state.pop("roles_preview", None)
                    st.session_state["roles_save_message"] = "Reset to built-in defaults and saved."
                    st.rerun()
                st.caption("Overwrites data/preferences.json with the built-in defaults shown in config.py.")

            if "roles_save_message" in st.session_state:
                st.success(st.session_state.pop("roles_save_message"))

            preview = st.session_state.get("roles_preview")
            if preview:
                delta = preview["new"] - preview["current"]
                st.info(
                    f"Current: **{preview['current']}** job(s) survive the filters. With these edits: "
                    f"**{preview['new']}** ({'+' if delta > 0 else ''}{delta})."
                )

        # -----------------------------------------------------------------
        # Resume tab
        # -----------------------------------------------------------------
        with tab_resume:
            st.write("Upload a PDF or paste text to replace data/resume.txt. Nothing is saved until you confirm below.")

            previous_text = RESUME_PATH.read_text(encoding="utf-8") if RESUME_PATH.exists() else None
            if previous_text is not None:
                with st.expander(f"Current resume.txt ({len(previous_text):,} characters) - what's active right now"):
                    st.text_area(
                        "Current resume.txt", value=previous_text, height=250, disabled=True, key="resume_current_preview"
                    )
            else:
                st.info("No resume.txt saved yet - upload or paste one below.")

            uploaded = st.file_uploader("Resume PDF", type=["pdf"], key="resume_pdf_upload")
            pasted = st.text_area("...or paste resume text", height=150, key="resume_paste")

            raw_text = None
            if uploaded is not None:
                try:
                    raw_text = extract_pdf_text(uploaded.getvalue())
                except Exception as exc:  # noqa: BLE001 - a corrupt/encrypted PDF must not crash the page
                    st.error(f"Could not extract text from this PDF: {exc}")
            elif pasted.strip():
                raw_text = pasted

            if raw_text and raw_text.strip():
                st.subheader("Extracted text")
                st.text_area("Raw extracted text", value=raw_text, height=300, disabled=True, key="resume_raw_preview")

                quality_warnings = check_resume_extraction_quality(raw_text, previous_text)
                for warning_text in quality_warnings:
                    st.warning(warning_text)
                if not quality_warnings:
                    st.success(
                        "No obvious extraction problems detected (long-word / space-ratio / length-drop checks) - "
                        "still worth reading the box above before confirming."
                    )

                skills, projects, extracted = extract_resume_sections(raw_text)
                st.subheader("What the Analyst would actually receive")
                if extracted:
                    st.text_area(
                        "Skills + Projects", value=f"{skills}\n\n{projects}", height=200, disabled=True, key="resume_sections_preview"
                    )
                else:
                    st.warning(
                        "extract_resume_sections found no recognized 'Technical Skills'/'Projects' headers - "
                        "the FULL raw text above would be sent to the Analyst instead of a trimmed section. Same "
                        "visible-fallback principle as the JD extractor: a silent miss here would look like every "
                        "other run, not an error."
                    )

                st.warning(
                    f"Saving this changes every future Analyst comparison - the resume text is baked into every "
                    f"cached verdict's identity. **{analyzed_count} currently-analyzed job(s)** will need to be "
                    f"re-scored: their old cache entries simply stop matching (nothing needs manual clearing), but "
                    f"the next orchestrator run will spend real LLM quota re-analyzing all of them."
                )

                if st.button("Confirm and save to data/resume.txt", key="resume_confirm_save"):
                    RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
                    RESUME_PATH.write_text(raw_text, encoding="utf-8")
                    st.success(
                        f"Saved. {analyzed_count} job(s)' cached analyses are now stale - re-run the orchestrator "
                        f"to refresh them."
                    )

        # -----------------------------------------------------------------
        # Applied tab
        # -----------------------------------------------------------------
        with tab_applied:
            st.write("Everything marked applied, most recent first.")

            applied_jobs = load_applied_jobs(session)
            if not applied_jobs:
                st.markdown(
                    render_empty_state_html(
                        "Nothing applied to yet",
                        'Mark a job "applied" from the status control on the Jobs tab and it shows up here.',
                    ),
                    unsafe_allow_html=True,
                )
            else:
                st.write(f"{len(applied_jobs)} job(s) applied to.")
                for aj in applied_jobs:
                    location = aj.location or "-"
                    status_note = f" _(now: {aj.application_status})_" if aj.application_status != "applied" else ""
                    st.write(f"**{aj.applied_at:%Y-%m-%d}** — [{aj.company} | {aj.title}]({aj.url}) | {location}{status_note}")

        # -----------------------------------------------------------------
        # Rejected tab
        # -----------------------------------------------------------------
        with tab_rejected:
            st.write(
                "A sample of jobs turned away by rule filters - if the filters are cutting something good, "
                "this is where to spot it."
            )
            rejected = filter_result.rejected
            reasons = sorted({r.reason for r in rejected})
            rule = st.selectbox("Rule", ["(all)"] + reasons, key="rejected_rule_filter")
            if rule != "(all)":
                rejected = [r for r in rejected if r.reason == rule]

            rule_label = f" for rule {rule!r}" if rule != "(all)" else ""
            st.write(f"{len(rejected)} job(s) rejected{rule_label}")

            sample = random.sample(rejected, min(REJECTED_SAMPLE_SIZE, len(rejected)))
            for r in sample:
                location = r.location or "-"
                st.write(f"**{r.company}** | {r.title} | {location} | `{r.reason}`")

            if len(rejected) > REJECTED_SAMPLE_SIZE:
                st.caption(f"Showing a random {REJECTED_SAMPLE_SIZE} of {len(rejected)} - rerun for a different sample.")
            elif not rejected:
                st.markdown(
                    render_empty_state_html(
                        "Nothing rejected under this rule",
                        "Either the rule genuinely hasn't fired, or every survivor was caught by a different one first.",
                    ),
                    unsafe_allow_html=True,
                )

        # -----------------------------------------------------------------
        # Companies tab
        # -----------------------------------------------------------------
        with tab_companies:
            st.write("Jobs fetched and rule-filter survivors per company from companies.yaml.")

            zero_survivor = [c for c in company_stats if c.jobs_fetched > 0 and c.survivors == 0]
            if zero_survivor:
                st.warning(
                    f"{len(zero_survivor)} company(ies) fetched jobs but produced zero survivors - "
                    "candidates for removal from companies.yaml: " + ", ".join(c.name for c in zero_survivor)
                )

            sorted_stats = sorted(company_stats, key=lambda c: c.survivors)
            st.dataframe(
                [
                    {
                        "Company": c.name,
                        "ATS": c.ats,
                        "Token": c.token,
                        "Fetched": c.jobs_fetched,
                        "Survivors": c.survivors,
                    }
                    for c in sorted_stats
                ],
                hide_index=True,
                width="stretch",
            )

        # -----------------------------------------------------------------
        # Coach tab
        # -----------------------------------------------------------------
        with tab_coach:
            st.write(
                "Missing-skills frequency across stage-1-scored jobs below a threshold - same as "
                "`python -m agents.coach --missing-skills`, no LLM call, stage-1 only (see agents/coach.py "
                "for why: mixing stage-1/stage-2 scores would aggregate rows judged by different models)."
            )
            threshold = st.number_input(
                "Fit score threshold", min_value=0, max_value=100, value=COACH_DEFAULT_THRESHOLD, step=5, key="coach_threshold"
            )
            report = missing_skills_below(session, threshold)
            st.write(f"{report.job_count} job(s) scored below {threshold} on stage-1 fit_score.")
            if report.skill_counts:
                for skill, count in report.skill_counts[:COACH_TOP_SKILLS]:
                    st.write(f"**{count}** — {skill}")
            else:
                st.markdown(
                    render_empty_state_html(
                        "Nothing scored below this threshold yet",
                        "Try raising the threshold, or confirm stage-1 has actually run on these survivors.",
                    ),
                    unsafe_allow_html=True,
                )
