"""Nightly job-discovery run: fetch every configured company's postings and
report what came back. Sequential and synchronous by design - LangGraph
orchestration (retries, conditional branches, crash-safe state) comes later,
see CLAUDE.md. No dedupe, filtering, ranking, or DB write yet; this is just
fetch and report so the ATS adapters and companies.yaml can be sanity-checked
against real data.
"""

import argparse
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from adapters.ashby import fetch_jobs as fetch_ashby
from adapters.base import BoardNotFoundError
from adapters.greenhouse import fetch_jobs as fetch_greenhouse
from adapters.lever import fetch_jobs as fetch_lever
from adapters.workday import fetch_jobs as fetch_workday
from agents.analyst import AnalystResult, analyze, derive_outcome, is_unscored, prepare_resume_text
from config import (
    GEMINI_MODEL_STAGE1,
    GEMINI_MODEL_STAGE2,
    GEMINI_RATE_LIMITS,
    PREFERENCES_LOAD_WARNINGS,
    STAGE2_TOP_N,
    load_companies,
)
from db import JobPostingRow, get_engine, get_last_fetched_map, job_posting_from_row, record_fetch, upsert_jobs
from filters import filter_jobs, parse_max_experience_years, reject_reason, rejected_jobs
from llm import GeminiClient
from models import ATSSource, Cadence, CompanyConfig, JobPosting, RemoteType
from ranking import MAX_TOKENS, rank_jobs

# How many days must pass since a Cadence.WEEKLY company's last successful
# fetch before fetch_all considers it due again. Lives here, not in db.py -
# it's fetch_all's business-logic threshold, not a storage concern. See
# models.Cadence's docstring for why WEEKLY exists at all.
WEEKLY_CADENCE_DAYS = 7

# Every value takes the whole CompanyConfig, not (name, token) - Workday
# needs three fields (tenant/wd/site), not one token, so a shared 2-string
# signature stopped fitting every source once Workday existed. The other
# three adapters' own fetch_jobs(company: str, board_token: str) functions
# are unchanged - these lambdas are the only place that unwraps a
# CompanyConfig into each adapter's real, narrower signature.
FETCHERS: dict[ATSSource, Callable[[CompanyConfig], list[JobPosting]]] = {
    ATSSource.GREENHOUSE: lambda c: fetch_greenhouse(c.name, c.token),
    ATSSource.LEVER: lambda c: fetch_lever(c.name, c.token),
    ATSSource.ASHBY: lambda c: fetch_ashby(c.name, c.token),
    ATSSource.WORKDAY: lambda c: fetch_workday(c.name, c.workday_tenant, c.workday_wd, c.workday_site),
}


@dataclass
class ProgressEvent:
    """One update from a nightly run, for a UI (app.py's Run tab) to
    subscribe to without scraping print() output or duplicating the
    reporting logic the CLI functions below already have. Plain data only
    (str/int/dict) - this crosses an in-process callback boundary, not a
    serialization one, so there's nothing Pydantic would buy here.

    stage: a short machine-readable name ("fetch"/"filter"/"stage1"/
    "stage2") - a UI branches on this to decide how to render, never on
    parsing `message`.
    message: ready-to-display text. Wherever a CLI print() line and a
    ProgressEvent describe the same moment, the wording matches on
    purpose, so the two surfaces never say two different things about
    what's happening (see fetch_all, _run_analyst_over_jobs).
    current/total: set together for a live count within a stage (e.g. job
    12 of 41 being analyzed, or company 12 of 62 being fetched); both None
    for a stage-boundary event that isn't counting anything yet.
    agent: which of CLAUDE.md's three named agents is doing this work, or
    None when nothing agentic is - fetch and filter are pipeline stages,
    not agents, and labelling them "Scout" would be flatly untrue (Scout
    finds board tokens for new companies and never runs in the nightly
    path at all). Only the Analyst runs here, so only stage1/stage2 carry
    a name. A UI that wants to show "which agent is working" reads this
    rather than inferring it from `stage`.
    extra: stage-specific numbers a UI might want beyond the count - Gemini
    quota used so far, per-company job counts, etc."""

    stage: str
    message: str
    current: Optional[int] = None
    total: Optional[int] = None
    agent: Optional[str] = None
    extra: dict = field(default_factory=dict)


ProgressCallback = Callable[[ProgressEvent], None]

# The one agent that actually runs in the nightly path. Scout (board-token
# discovery for a new company) is on-demand and Coach (weekly RAG over the
# archive) is neither - see ProgressEvent.agent. Named here rather than
# spelled inline at each emit site so the string can't drift between them.
AGENT_ANALYST = "Analyst"


def _emit(on_progress: Optional[ProgressCallback], event: ProgressEvent) -> None:
    """The one call site every progress-reporting function below routes
    through - on_progress=None (every existing CLI call site, unchanged)
    means this is a no-op, so print() stays the CLI's only output exactly
    as before. A callback exception must never take down a real run just
    because a UI's rendering code had a bug - caught and dropped, not
    re-raised."""
    if on_progress is None:
        return
    try:
        on_progress(event)
    except Exception:  # noqa: BLE001 - a UI callback bug must not crash the pipeline
        pass


def _is_due(
    last_fetched_by_company: Optional[dict[str, datetime]], company: CompanyConfig, force: bool
) -> tuple[bool, Optional[int]]:
    """Whether `company` should be fetched this call, and (if not) how many
    days it's been since its last successful fetch - for the skip message.

    Pure: it takes an already-read mapping, never a Session, and so cannot
    issue a query. That is the point, not an incidental detail. When this
    took a Session it ran a SELECT per company from inside fetch_all's
    loop, which held a transaction open across 40+ minutes of HTTP work and
    was killed by Neon's idle-in-transaction timeout in a real GitHub
    Actions run. A signature that cannot touch the database cannot
    reintroduce that. See docs/decisions.md.

    None (cadence disabled - see fetch_all's docstring), force, or NIGHTLY
    means always due. A WEEKLY company absent from the mapping has never
    been fetched, so it is due on its first run, same as any brand-new
    companies.yaml entry."""
    if last_fetched_by_company is None or force or company.cadence == Cadence.NIGHTLY:
        return True, None
    last_fetched = last_fetched_by_company.get(company.name)
    if last_fetched is None:
        return True, None
    days_since = (datetime.now(timezone.utc).replace(tzinfo=None) - last_fetched).days
    return days_since >= WEEKLY_CADENCE_DAYS, days_since


def fetch_all(
    companies: list[CompanyConfig],
    on_progress: Optional[ProgressCallback] = None,
    engine: Optional[Engine] = None,
    force: bool = False,
) -> tuple[list[JobPosting], list[tuple[CompanyConfig, str]], list[tuple[CompanyConfig, str]]]:
    """Fetch every company in turn. One company's failure must not stop the
    others - this is the layer that decides to continue, not the adapters.
    BoardNotFoundError means companies.yaml points at a token that doesn't
    exist (a config error); anything else is an unexpected adapter or network
    failure. Both are recorded against the company and skipped, not raised.

    Takes an Engine, never a Session, and opens its own SHORT sessions
    around the fetch loop rather than holding one across it:

        1. one short session reads every company's last-fetch time at once
        2. the loop runs with no database connection held at all
        3. one short session records the successful fetches

    That shape is load-bearing, not stylistic. An earlier version took an
    open Session, and the first `_is_due` query started a transaction that
    then stayed open across the whole loop - 40+ minutes when the large
    Workday tenants are due. Neon terminates idle-in-transaction
    connections, so a real GitHub Actions run died with
    `psycopg.OperationalError: terminating connection due to
    idle-in-transaction timeout`, which LangGraph's retry policy then
    turned into a second complete fetch of all 67 companies. SQLite has no
    such timeout, which is why it never appeared locally. Accepting an
    Engine rather than a Session means the long-open-transaction bug is no
    longer expressible here. See docs/decisions.md.

    engine=None (the default) disables Cadence.WEEKLY entirely - every
    company is fetched every call, and nothing touches the database. That's
    what every caller predating cadence keeps getting for free, the same
    "None means old behavior" convention on_progress already uses on this
    function. A real caller passes an engine so WEEKLY companies are
    skipped when not due and successful fetches are recorded.

    force=True fetches every company regardless of cadence, but still
    records the fetch afterward - a forced run resets the weekly clock,
    it doesn't bypass it forever.

    Returns (jobs, failures, skipped). A company skipped for not being due
    yet is neither a success nor a failure - folding it into either list
    would misreport what actually happened this run."""
    jobs: list[JobPosting] = []
    failures: list[tuple[CompanyConfig, str]] = []
    skipped: list[tuple[CompanyConfig, str]] = []
    fetched_ok: list[str] = []

    # Phase 1: read the whole cadence picture in one short transaction,
    # then let it close before any network work starts.
    last_fetched_by_company: Optional[dict[str, datetime]] = None
    if engine is not None:
        with Session(engine) as session:
            last_fetched_by_company = get_last_fetched_map(session, [c.name for c in companies])

    _emit(on_progress, ProgressEvent(stage="fetch", message=f"Fetching from {len(companies)} companies...", total=len(companies)))

    # Phase 2: the long part. No session is open anywhere in this loop.
    for i, company in enumerate(companies, start=1):
        due, days_since = _is_due(last_fetched_by_company, company, force)
        if not due:
            reason = f"weekly cadence, last fetched {days_since} day(s) ago"
            skipped.append((company, reason))
            _emit(
                on_progress,
                ProgressEvent(
                    stage="fetch",
                    message=f"Fetching from {len(companies)} companies... ({i}/{len(companies)})",
                    current=i,
                    total=len(companies),
                    extra={"company": company.name, "skipped": True, "reason": reason},
                ),
            )
            print(f"  {company.name} ({company.ats.value}): skipped ({reason})")
            continue

        fetch = FETCHERS[company.ats]
        error: Optional[str] = None
        company_jobs: list[JobPosting] = []
        try:
            company_jobs = fetch(company)
        except BoardNotFoundError as exc:
            error = f"config error: {exc}"
        except Exception as exc:
            error = f"unexpected error: {type(exc).__name__}: {exc}"

        # Progress is emitted unconditionally, success or failure - a
        # skipped emit on the failure path (an earlier version of this
        # loop had the emit only on the success branch, caught by a test
        # deliberately exercising an all-failing FETCHERS) would leave a
        # UI's live count permanently short of the real company count
        # whenever any company failed, looking like a stall even though
        # the loop was working fine and moving on.
        _emit(
            on_progress,
            ProgressEvent(
                stage="fetch",
                message=f"Fetching from {len(companies)} companies... ({i}/{len(companies)})",
                current=i,
                total=len(companies),
                extra={"company": company.name, "jobs_found": len(company_jobs), "error": error},
            ),
        )

        if error is not None:
            failures.append((company, error))
            continue

        print(f"  {company.name} ({company.ats.value}): {len(company_jobs)} jobs")
        jobs.extend(company_jobs)
        fetched_ok.append(company.name)

    # Phase 3: one short session to record what succeeded. Only companies
    # whose fetch actually worked - a failed one must be retried next run,
    # not treated as freshly fetched and skipped for a week.
    if engine is not None and fetched_ok:
        with Session(engine) as session:
            for name in fetched_ok:
                record_fetch(session, name)
            session.commit()

    return jobs, failures, skipped


def load_jobs_from_db() -> list[JobPosting]:
    """Reconstruct every stored posting as a JobPosting, for --no-fetch
    iteration: no network calls, and filter_jobs/rank_jobs/analyze run
    against current logic rather than whatever filter_passed was frozen to
    at the last real fetch - so a filters.py/extraction.py tune shows its
    effect immediately without waiting on the network again."""
    engine = get_engine()
    with Session(engine) as session:
        # Explicitly ordered. SQLite returns rows in rowid order when no
        # ORDER BY is given, which made this stable by accident; Postgres
        # guarantees nothing, and --limit N slices this list (see
        # _print_analyst_stage) - so an unordered read would analyze a
        # different arbitrary N on every run of a flag whose entire purpose
        # is a repeatable cheap test before spending real quota.
        rows = session.query(JobPostingRow).order_by(JobPostingRow.first_seen, JobPostingRow.content_hash).all()
        return [job_posting_from_row(row) for row in rows]


def persist_jobs(jobs: list[JobPosting]) -> dict[str, int]:
    """Classify every fetched job (kept, or rejected with its rule) and
    upsert it. Rejected jobs are stored too, not discarded - they're the RAG
    archive corpus, and refetching them nightly would waste requests for no
    benefit. Returns per-outcome counts for the run summary."""
    engine = get_engine()
    classified = [(job, reject_reason(job), parse_max_experience_years(job.description)) for job in jobs]
    with Session(engine) as session:
        return upsert_jobs(session, classified)


def _format_experience(job: JobPosting) -> str:
    """Advisory only, never a filter criterion - see docs/decisions.md. None
    means the parser found nothing to anchor on, not that the role requires
    zero years; that distinction has to stay visible, not get flattened
    into a number."""
    years = parse_max_experience_years(job.description)
    return f"({int(years)} yrs)" if years is not None else "(not stated)"


def print_persistence(outcomes: dict[str, int]) -> None:
    print()
    print(f"Persisted: {outcomes['new']} new, {outcomes['unchanged']} already-seen, {outcomes['edited']} edited")


def print_failures(failures: list[tuple[CompanyConfig, str]]) -> None:
    if not failures:
        return
    print()
    print(f"FAILURES ({len(failures)})")
    for company, reason in failures:
        print(f"  ! {company.name} ({company.ats.value}, token={company.token!r}): {reason}")


def print_skipped(skipped: list[tuple[CompanyConfig, str]]) -> None:
    """Weekly-cadence companies not due this run - distinct from FAILURES,
    since nothing went wrong; see fetch_all's docstring."""
    if not skipped:
        return
    print()
    print(f"SKIPPED ({len(skipped)}, weekly cadence not yet due - use --force to fetch anyway)")
    for company, reason in skipped:
        print(f"  - {company.name}: {reason}")


def print_sample(jobs: list[JobPosting], count: int = 20) -> None:
    """Eyeball a random slice of what the adapters actually returned, before
    any filtering exists to narrow it down."""
    print()
    for job in random.sample(jobs, min(count, len(jobs))):
        location = job.location or "-"
        print(f"{job.company} | {job.title} | {location} | {job.remote_type.value}")


def print_filtered(jobs: list[JobPosting]) -> None:
    """Run the rule-based filters and show what's left, plus how many each
    rule turned away - so a bad keyword tune shows up as a rejection-count
    spike, not just a shorter survivor list."""
    kept, rejected_by = filter_jobs(jobs)

    print()
    print(f"Survived filters: {len(kept)} / {len(jobs)}")
    for job in kept:
        location = job.location or "-"
        print(f"{job.company} | {job.title} | {location} | {job.remote_type.value} | {_format_experience(job)}")

    print()
    print("Rejected by rule:")
    for reason, count in rejected_by.most_common():
        print(f"  {reason}: {count}")


def print_rejected(jobs: list[JobPosting], rule: Optional[str] = None, count: int = 30) -> None:
    """Sample of rejected jobs, optionally narrowed to one rule, so a keyword
    tune can be checked against what it's actually turning away instead of
    just the shrinking survivor count."""
    pairs = rejected_jobs(jobs)
    if rule is not None:
        pairs = [(job, reason) for job, reason in pairs if reason == rule]

    print()
    if not pairs:
        print(f"No rejected jobs found{f' for rule {rule!r}' if rule else ''}.")
        return

    label = f", rule={rule!r}" if rule else ""
    print(f"Rejected sample ({min(count, len(pairs))} of {len(pairs)}{label}):")
    for job, reason in random.sample(pairs, min(count, len(pairs))):
        print(f"{job.company} | {job.title} | {reason}")


def print_ranked(jobs: list[JobPosting]) -> None:
    """Filter to survivors, then rank them against the resume by cosine
    similarity - a coarse relevance signal for prioritizing within the
    filtered pool, not a fit judgment. See ranking.py's module docstring for
    the failure modes this is expected to have (truncation, topical-vs-skill
    similarity) before trusting the scores. Extraction status is printed per
    job and summarized as a ratio, not silently folded into the score - a
    header this run's extractor doesn't recognize must be visible, not just
    a slightly-off number."""
    kept, _ = filter_jobs(jobs)
    print()
    if not kept:
        print("No jobs survived filtering - nothing to rank.")
        return

    engine = get_engine()
    with Session(engine) as session:
        ranked, diagnostics = rank_jobs(kept, session)

    print(f"Ranked {len(ranked)} survivors by resume similarity:")
    for job, score in ranked:
        location = job.location or "-"
        info = diagnostics.jobs[job.description_hash]
        status = "extracted" if info.extracted else "FALLBACK (no header matched)"
        if info.truncated:
            status += f", still truncated ({info.token_count} tok)"
        else:
            status += f" ({info.token_count} tok)"
        print(f"{score:.3f} | {job.company} | {job.title} | {location} | {_format_experience(job)} | {status}")

    print()
    resume_info = diagnostics.resume
    if resume_info.extracted:
        resume_status = "extracted cleanly"
    else:
        resume_status = "FALLBACK - no 'Technical Skills'/'Projects' header matched in resume.txt"
    truncated_note = ", still truncated" if resume_info.truncated else ", fits within window"
    print(f"Resume: {resume_status} ({resume_info.token_count} tok{truncated_note})")

    extracted_count = sum(1 for info in diagnostics.jobs.values() if info.extracted)
    total = len(diagnostics.jobs)
    rate_pct = diagnostics.job_extraction_rate * 100
    print(f"Job extraction: {extracted_count}/{total} ({rate_pct:.0f}%) had a recognized section extracted")

    still_truncated = [job for job, _ in ranked if diagnostics.jobs[job.description_hash].truncated]
    if still_truncated:
        names = ", ".join(f"{job.company} {job.title}" for job in still_truncated)
        print(f"Still over the {MAX_TOKENS}-token window after extraction ({len(still_truncated)}): {names}")


def _run_analyst_over_jobs(
    jobs: list[JobPosting],
    client: GeminiClient,
    resume_text: str,
    session: Session,
    stage: str = "stage1",
    on_progress: Optional[ProgressCallback] = None,
) -> tuple[list[tuple[JobPosting, AnalystResult]], dict[str, int], int, int, Optional[tuple[JobPosting, Exception]]]:
    """Core loop shared by both stages: stream a line per job as it
    completes, only calling the LLM on a cache miss (see agents/analyst.py's
    text_hash cache).

    A failure on one job stops the batch rather than raising uncaught - a
    real run hit a quota-exhaustion 429 partway through, and an earlier
    version (batch-collect-then-print-at-the-end) lost visibility into every
    job that had already succeeded when the exception propagated. Results
    already computed are safely in the database regardless (analyze()
    commits per job) - this is about not losing the *printed report* of
    them too. Returns (results, usage_totals, cache_hits, fresh_calls,
    failure).

    stage: the ProgressEvent.stage tag for every event this call emits -
    "stage1" for both a real stage-1 pass AND print_analyst_stage2's
    internal stage-1-as-ranking-input re-run (it genuinely is a stage-1
    pass, just usually all cache hits the second time - see
    print_analyst_stage2's docstring), "stage2" for the real stage-2 model
    run. Not inferred from `client.model_name` - a caller states it
    explicitly, since the two can diverge (this ranking-input re-run always
    uses GEMINI_MODEL_STAGE1 regardless of which stage is asking for it)."""
    results: list[tuple[JobPosting, AnalystResult]] = []
    cache_hits = 0
    fresh_calls = 0
    usage_totals = {"promptTokenCount": 0, "candidatesTokenCount": 0, "thoughtsTokenCount": 0, "totalTokenCount": 0}
    failure: Optional[tuple[JobPosting, Exception]] = None
    rpd = GEMINI_RATE_LIMITS.get(client.model_name, {}).get("rpd")

    for i, job in enumerate(jobs, start=1):
        try:
            result, from_cache = analyze(job, resume_text, client, session)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            failure = (job, exc)
            break

        cache_hits += from_cache
        results.append((job, result))
        source = "cache" if from_cache else "fresh"
        # Only a fresh call spends daily quota - a cache hit doesn't move
        # call_count, so the budget note only makes sense attached to it.
        budget = f", call {client.call_count}/{rpd}" if (not from_cache and rpd) else ""
        print(f"  [{i}/{len(jobs)}, {source}{budget}] fit={result.fit_score:>3} | {job.company} | {job.title}")
        # Tokens only on a fresh call. client.last_usage is only overwritten
        # when complete() actually runs, so on a cache hit it still holds
        # the PREVIOUS job's usage - reporting that here would silently
        # bill every cache hit for tokens it never spent, and a run that is
        # mostly cache hits (the normal case for stage 2, which re-reads
        # stage 1's work) would report several times its real usage.
        tokens = client.last_usage.get("totalTokenCount") if (not from_cache and client.last_usage) else None
        _emit(
            on_progress,
            ProgressEvent(
                stage=stage,
                message=f"[{i}/{len(jobs)}] {job.company} | {job.title}",
                current=i,
                total=len(jobs),
                agent=AGENT_ANALYST,
                extra={
                    "source": source,
                    "call_count": client.call_count,
                    "rpd": rpd,
                    "fit_score": result.fit_score,
                    "model": client.model_name,
                    "tokens": tokens,
                },
            ),
        )

        if not from_cache and client.last_usage:
            fresh_calls += 1
            for key in usage_totals:
                usage_totals[key] += client.last_usage.get(key, 0)

    return results, usage_totals, cache_hits, fresh_calls, failure


def _print_analyst_summary(
    jobs: list[JobPosting],
    results: list[tuple[JobPosting, AnalystResult]],
    usage_totals: dict[str, int],
    cache_hits: int,
    fresh_calls: int,
    failure: Optional[tuple[JobPosting, Exception]],
) -> None:
    print()
    print(f"Completed {len(results)} / {len(jobs)} ({cache_hits} from cache, {fresh_calls} fresh)")

    if failure is not None:
        failed_job, exc = failure
        remaining = len(jobs) - len(results) - 1
        print(f"STOPPED: {failed_job.company} | {failed_job.title} failed with {type(exc).__name__}: {exc}")
        print(f"{remaining} job(s) not attempted. Completed results are cached - re-running will skip them and resume here.")

    if fresh_calls:
        print()
        print(
            f"Measured token usage over {fresh_calls} fresh call(s): "
            f"{usage_totals['promptTokenCount']} prompt + {usage_totals['thoughtsTokenCount']} thinking "
            f"(hidden, not in the detail below) + {usage_totals['candidatesTokenCount']} visible output "
            f"= {usage_totals['totalTokenCount']} total "
            f"({usage_totals['totalTokenCount'] / fresh_calls:.0f} tokens/job avg)"
        )


def _print_results_detail(results: list[tuple[JobPosting, AnalystResult]]) -> None:
    """Scored jobs first, sorted by fit_score - unscored ones (empty
    matched AND empty missing, see agents/analyst.py's is_unscored) are
    never mixed into that ranking, since their fit_score isn't a real
    comparison and would sit among real scores as if it were one. Printed
    separately afterward instead of silently dropped."""
    scored = [(job, result) for job, result in results if not is_unscored(result)]
    unscored = [(job, result) for job, result in results if is_unscored(result)]

    print()
    print("Sorted by fit_score:")
    for job, result in sorted(scored, key=lambda pair: pair[1].fit_score, reverse=True):
        verdict = derive_outcome(result)
        print()
        print(f"{result.fit_score:>3} [{verdict:>8}] | {job.company} | {job.title}")
        print(f"      matched: {', '.join(result.matched_skills) or '-'}")
        print(f"      missing: {', '.join(result.missing_skills) or '-'}")
        gap = result.experience_gap
        if gap.years_required is not None:
            gap_str = f"{gap.years_required:g} yrs required, resume meets it: {gap.resume_meets_it}"
        else:
            gap_str = "not stated"
        print(f"      experience: {gap_str}")
        print(f"      {result.reasoning}")

    if unscored:
        print()
        print(f"Could not evaluate ({len(unscored)}) - no technical requirements extracted, not a real score:")
        for job, result in unscored:
            print(f"  ? {job.company} | {job.title} - {result.reasoning}")


def _analyze_stage(
    jobs: list[JobPosting],
    model: str,
    label: str,
    limit: Optional[int] = None,
    stage: str = "stage1",
    on_progress: Optional[ProgressCallback] = None,
) -> tuple[list[JobPosting], list[tuple[JobPosting, AnalystResult]]]:
    """Shared setup+run for one stage: filter, cap to limit, build a client
    for `model`, run the batch, print the streaming lines and the summary.
    Returns (kept, results) - kept is exposed so a caller reporting on stage
    2 can state its denominator against what stage 1 actually saw. Does NOT
    print the sorted fit_score detail listing; callers decide if and when
    that's wanted, since stage 2 reuses stage 1 purely as a ranking input
    and doesn't need stage 1's detail repeated.

    stage/on_progress: forwarded to _run_analyst_over_jobs - see its
    docstring for why `stage` is stated explicitly rather than inferred
    from `model`."""
    kept, _ = filter_jobs(jobs)
    if limit is not None:
        kept = kept[:limit]

    print()
    if not kept:
        print(f"{label}: no jobs survived filtering - nothing to analyze.")
        _emit(
            on_progress,
            ProgressEvent(
                stage=stage, message=f"{label}: no jobs survived filtering.", total=0, agent=AGENT_ANALYST
            ),
        )
        return kept, []

    rpm = GEMINI_RATE_LIMITS.get(model, {}).get("rpm")
    client = GeminiClient(model=model, requests_per_minute=rpm)
    resume_text = prepare_resume_text()
    engine = get_engine()

    pacing_note = f", paced to {rpm} req/min" if rpm else ""
    print(f"{label} ({model}{pacing_note}): analyzing {len(kept)} job(s) (only cache misses call the LLM)...")
    _emit(
        on_progress,
        ProgressEvent(
            stage=stage,
            message=f"{label}: analyzing {len(kept)} job(s)...",
            total=len(kept),
            agent=AGENT_ANALYST,
            extra={"model": model, "rpm": rpm},
        ),
    )
    with Session(engine) as session:
        results, usage_totals, cache_hits, fresh_calls, failure = _run_analyst_over_jobs(
            kept, client, resume_text, session, stage=stage, on_progress=on_progress
        )

    _print_analyst_summary(kept, results, usage_totals, cache_hits, fresh_calls, failure)
    return kept, results


def print_analyst_stage1(
    jobs: list[JobPosting], limit: Optional[int] = None, on_progress: Optional[ProgressCallback] = None
) -> list[tuple[JobPosting, AnalystResult]]:
    """Stage 1 of the two-stage cascade: the cheap model
    (GEMINI_MODEL_STAGE1) screens every survivor. Cheap-filters-first
    applied to LLM calls - the same principle the rest of the pipeline
    already uses (rule filters -> embeddings -> LLM) - a full pass at ~52%
    lower cost and zero thinking-token overhead is meant to fit inside the
    free-tier daily cap where a single-stage run against the stronger model
    previously didn't (see docs/decisions.md, the Flash-Lite comparison).
    limit caps how many survivors get analyzed, same as before."""
    _, results = _analyze_stage(jobs, GEMINI_MODEL_STAGE1, "Stage 1", limit=limit, stage="stage1", on_progress=on_progress)
    _print_results_detail(results)
    return results


def print_analyst_stage2(
    jobs: list[JobPosting], limit: Optional[int] = None, on_progress: Optional[ProgressCallback] = None
) -> list[tuple[JobPosting, AnalystResult]]:
    """Stage 2: run stage 1 first purely as a ranking input (mostly cache
    hits if stage 1 has already run, so this costs little to nothing extra),
    take the top STAGE2_TOP_N survivors by stage 1's fit_score, and
    re-analyze just those with the stronger model (GEMINI_MODEL_STAGE2).
    Prints stage 2's own detail, then the stage1-vs-stage2 comparison - the
    measurable "how often does stage 2 disagree with stage 1's ordering"
    the two-stage design was asked for. This is what bare --analyze runs by
    default: the full cascade, not just a screening pass.

    The ranking-input pass below emits stage="stage1" progress events, same
    as a real stage-1 run (see _run_analyst_over_jobs) - orchestrator.py's
    stage1_analyze node already ran this exact model over these exact jobs
    moments earlier, so this second pass is real but nearly always all
    cache hits; a UI subscribed via on_progress will see a brief stage1
    flash before the real "stage2"-tagged deep pass begins, which is an
    accurate reflection of what pipeline.py actually does here, not
    something worth hiding.

    Returns stage2_results (the CLI dispatch below discards it, same as
    always - orchestrator.py's stage2_analyze node is the reason this
    returns something now instead of None, so it can report which jobs
    actually got a stage-2 verdict without re-querying the DB for it)."""
    _, stage1_results = _analyze_stage(
        jobs, GEMINI_MODEL_STAGE1, "Stage 1 (ranking input)", limit=limit, stage="stage1", on_progress=on_progress
    )
    if not stage1_results:
        return []

    # Unscored jobs (see is_unscored) have no real fit_score to rank by -
    # excluded from stage-2 selection entirely, not just sorted low. A
    # fabricated 0 or 60 must not compete for one of stage 2's scarce slots
    # either direction.
    rankable = [(job, result) for job, result in stage1_results if not is_unscored(result)]
    stage1_sorted = sorted(rankable, key=lambda pair: pair[1].fit_score, reverse=True)
    stage1_top = stage1_sorted[:STAGE2_TOP_N]
    top_jobs = [job for job, _ in stage1_top]

    _, stage2_results = _analyze_stage(
        top_jobs, GEMINI_MODEL_STAGE2, f"Stage 2 (top {STAGE2_TOP_N})", stage="stage2", on_progress=on_progress
    )
    _print_results_detail(stage2_results)

    print_stage_comparison(stage1_top, stage2_results)
    return stage2_results


def print_stage_comparison(
    stage1_top: list[tuple[JobPosting, AnalystResult]], stage2_results: list[tuple[JobPosting, AnalystResult]]
) -> None:
    """Side-by-side stage1-vs-stage2 fit_score per job, plus pairwise
    ordering agreement: of every pair of jobs in this set, what fraction do
    the two stages agree on which one ranks higher? A tie in either stage's
    scores carries no direction to agree or disagree with, so those pairs
    are excluded from both the numerator and denominator rather than
    counted either way."""
    stage1_by_hash = {job.content_hash: result for job, result in stage1_top}
    stage2_sorted = sorted(stage2_results, key=lambda pair: pair[1].fit_score, reverse=True)

    print()
    print(f"Stage 1 vs stage 2 ({len(stage2_sorted)} jobs re-checked):")
    print(f"{'stage2':>6} | {'stage1':>6} | company | title")
    for job, s2_result in stage2_sorted:
        s1_result = stage1_by_hash[job.content_hash]
        print(f"{s2_result.fit_score:>6} | {s1_result.fit_score:>6} | {job.company} | {job.title}")

    hashes = [job.content_hash for job, _ in stage2_sorted]
    s1_scores = {h: stage1_by_hash[h].fit_score for h in hashes}
    s2_scores = {job.content_hash: result.fit_score for job, result in stage2_sorted}

    agree = 0
    compared = 0
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            a, b = hashes[i], hashes[j]
            s1_order = s1_scores[a] - s1_scores[b]
            s2_order = s2_scores[a] - s2_scores[b]
            if s1_order == 0 or s2_order == 0:
                continue  # a tie in either stage has no direction to agree/disagree with
            compared += 1
            if (s1_order > 0) == (s2_order > 0):
                agree += 1

    print()
    if compared:
        print(f"Pairwise ordering agreement: {agree}/{compared} ({agree / compared * 100:.0f}%) of non-tied pairs agree")
    else:
        print("Pairwise ordering agreement: no non-tied pairs to compare")


def print_distribution(jobs: list[JobPosting]) -> None:
    print()
    print("Remote type distribution:")
    counts = Counter(job.remote_type for job in jobs)
    for remote_type in RemoteType:
        print(f"  {remote_type.value}: {counts.get(remote_type, 0)}")

    with_posted_at = sum(1 for job in jobs if job.posted_at is not None)
    print()
    print(f"posted_at set: {with_posted_at} / {len(jobs)}  (missing: {len(jobs) - with_posted_at})")


if __name__ == "__main__":
    # Job titles/companies/descriptions aren't guaranteed ASCII (smart quotes,
    # en-/em-dashes are common in real postings). Windows defaults stdout to
    # the system codepage (cp1252) rather than UTF-8 when it isn't a real
    # console - e.g. redirected to a file - so without this, non-ASCII
    # characters get silently written as single cp1252 bytes that any
    # UTF-8 reader (an editor, a terminal, this tool's own output capture)
    # then misdecodes as "�" or worse. Verified live: this was corrupting
    # every --analyze run's output file, not adapters/base.py's strip_html -
    # the stored, fetched description text was already correct; only the
    # print step wasn't. Same fix as evaluate.py already has.
    sys.stdout.reconfigure(encoding="utf-8")

    # Printed unconditionally, before argument parsing even finishes -
    # PREFERENCES_LOAD_WARNINGS is set once at config.py's import time
    # (module load already happened by now, via the `from config import`
    # above), so this is cheap and always current for this process. Every
    # rejected job still gets fetched and rejected on real title/location
    # regardless, but a silently-emptied title_allowlist would pass every
    # job through unfiltered here specifically - the run that actually
    # spends LLM quota - so this warning belongs at the top of the one
    # command that can burn a day's budget on it, not just in the
    # dashboard's read-only view. See config.py's DEFAULT_PREFERENCES.
    for warning_text in PREFERENCES_LOAD_WARNINGS:
        print(f"WARNING: {warning_text}")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sample",
        action="store_true",
        help="print 20 random jobs as 'company | title | location | remote_type' instead of summary counts",
    )
    parser.add_argument(
        "--filtered",
        action="store_true",
        help="run the rule-based filters and print survivors plus rejection counts per rule",
    )
    parser.add_argument(
        "--rejected",
        nargs="?",
        const="__all__",
        default=None,
        metavar="RULE",
        help=(
            "print a sample of 30 rejected jobs as 'company | title | rejected_by_rule'; "
            "optionally pass a rule name (seniority, non_engineering, not_allowlisted, "
            "not_india) to see only that rule's rejections"
        ),
    )
    parser.add_argument(
        "--ranked",
        action="store_true",
        help="filter, then rank survivors against the resume by cosine similarity and print with scores",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help=(
            "filter, then run the Analyst (Gemini) over survivors and print fit results; "
            "see --stage for which model(s) run"
        ),
    )
    parser.add_argument(
        "--stage",
        type=int,
        choices=[1, 2],
        default=None,
        metavar="N",
        help=(
            "with --analyze, restrict to one stage of the two-stage cascade: 1 runs only the "
            "cheap screening model (GEMINI_MODEL_STAGE1) over every survivor; 2 (the default when "
            "--stage is omitted) runs stage 1 to rank, then re-checks the top STAGE2_TOP_N with "
            "the stronger model (GEMINI_MODEL_STAGE2) and prints the stage1-vs-stage2 comparison"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="with --analyze, only process the first N survivors (test before spending a full run's quota)",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip the network fetch entirely and read every stored posting straight from the database",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "fetch every company regardless of cadence, including Cadence.WEEKLY companies not "
            "yet due (see models.Cadence) - still records the fetch, resetting their weekly clock"
        ),
    )
    args = parser.parse_args()

    if args.no_fetch:
        jobs = load_jobs_from_db()
        failures = []
        skipped = []
        print(f"Loaded {len(jobs)} jobs from the database (no fetch, no persist)")
    else:
        companies = load_companies()
        print(f"Fetching jobs for {len(companies)} companies...")
        print()

        # A real engine, unlike every fetch_all() call in the test suite -
        # this is the one CLI call site where Cadence.WEEKLY actually takes
        # effect (see fetch_all's docstring: engine=None disables it).
        # fetch_all opens its own short sessions; nothing is held open
        # across the fetch.
        jobs, failures, skipped = fetch_all(companies, engine=get_engine(), force=args.force)

        print_failures(failures)
        print_skipped(skipped)

        persistence_outcomes = persist_jobs(jobs)
        print_persistence(persistence_outcomes)

    if args.sample:
        print_sample(jobs)
    elif args.filtered:
        print_filtered(jobs)
    elif args.rejected is not None:
        print_rejected(jobs, rule=None if args.rejected == "__all__" else args.rejected)
    elif args.ranked:
        print_ranked(jobs)
    elif args.analyze:
        if args.stage == 1:
            print_analyst_stage1(jobs, limit=args.limit)
        else:
            print_analyst_stage2(jobs, limit=args.limit)
    else:
        print()
        if args.no_fetch:
            print(f"TOTAL: {len(jobs)} jobs loaded from the database")
        else:
            ok_count = len(companies) - len(failures) - len(skipped)
            print(
                f"TOTAL: {len(jobs)} jobs fetched across {ok_count}/{len(companies)} companies "
                f"({len(skipped)} skipped, weekly cadence not due)"
            )

        print_distribution(jobs)
