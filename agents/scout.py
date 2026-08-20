"""Scout agent: given a company name, determines which ATS it uses, finds the
board token, and validates that jobs actually come back - genuinely agentic,
unlike the Analyst: it forms a hypothesis (a candidate token), tests it
against reality via the adapter's own hard 404-vs-not signal, and retries
differently on failure, bounded by a hard attempt cap so a company that
isn't on any of these three ATSs fails cheaply rather than looping forever.

Mechanical candidates run first and are free - lowercased/hyphenated/
suffixed variants of the company name, tested against real board endpoints.
The LLM only enters once mechanical is exhausted, and only to propose new
token strings from general patterns (concatenated corporate suffixes,
domain-as-token, abbreviations) - it never judges whether a candidate
"worked". That judgment is the adapter's: BoardNotFoundError (404) means
wrong token for this source, anything else means the board exists. See
docs/decisions.md for the live test run against known-tricky real companies.
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from adapters.ashby import fetch_jobs as fetch_ashby
from adapters.base import ATSAdapterError, BoardNotFoundError
from adapters.greenhouse import fetch_jobs as fetch_greenhouse
from adapters.lever import fetch_jobs as fetch_lever
from llm import LLMClient, LLMError
from models import ATSSource, CompanyConfig

ADAPTERS = {
    ATSSource.ASHBY: fetch_ashby,
    ATSSource.LEVER: fetch_lever,
    ATSSource.GREENHOUSE: fetch_greenhouse,
}

# Tested in this order per candidate - matches the actual ATS distribution in
# companies.yaml, so the common case short-circuits fastest. Not a claim this
# order is universal, just a reasonable prior worth spending first.
SOURCE_ORDER = [ATSSource.ASHBY, ATSSource.LEVER, ATSSource.GREENHOUSE]

# Deliberately modest and bounded - a short list of common corporate
# suffixes, concatenated with and without a hyphen. No TLD/domain guessing
# here: that's genuinely open-ended (.com, .io, .ai, .dev, ...) in a way a
# fixed suffix list isn't, which is exactly what the LLM fallback exists
# for - see generate_mechanical_candidates.
_SUFFIXES = ["inc", "hq", "io", "co", "labs"]

# Indian legal-entity suffixes, found the hard way: Razorpay's real
# Greenhouse token is "razorpaysoftwareprivatelimited" - its full registered
# legal name, concatenated with no separator - not "razorpay" or any
# hyphenated form. _SUFFIXES's hyphenated variant doesn't model this at all;
# these are concatenated-only for the same reason no_space/hyphenated both
# exist above - matching the real pattern instead of doubling candidates
# that don't occur in practice. "labs" duplicates an existing _SUFFIXES
# candidate (companylabs) - harmless, generate_mechanical_candidates already
# dedupes - kept here anyway so this list stays a complete, literal record
# of the requested suffix set rather than a silently-trimmed one. See
# docs/decisions.md for the Razorpay case this was built from.
_INDIAN_LEGAL_SUFFIXES = [
    "softwareprivatelimited",
    "technologiesprivatelimited",
    "privatelimited",
    "pvtltd",
    "technologies",
    "software",
    "labs",
    "solutions",
]

# Raised from 50 alongside _INDIAN_LEGAL_SUFFIXES: worst-case mechanical
# alone is now up to 57 attempts (19 candidates x 3 sources) for a
# genuinely-unsupported two-word company name, which would exhaust the old
# cap before the mechanical queue ever emptied - meaning the LLM fallback
# (and the razorpaysoftwareprivatelimited-shaped example now in its prompt)
# would rarely even get invoked. 75 comfortably covers worst-case mechanical
# (57) plus one full LLM round (5 candidates x 3 sources = 15). This raises
# the per-unsupported-company request ceiling from ~45 to ~72 - still zero
# dollar cost (these are free, unauthenticated public board APIs), but a
# real ~1.6x increase in wall-clock requests for a batch run, worth knowing
# before scouting dozens of companies at once.
MAX_TOTAL_ATTEMPTS = 75
# One round only. A first round of LLM suggestions failing means further
# rounds are guessing at guesses with no new information - not worth the
# extra requests for a company that's very possibly just not on any of
# these three ATSs at all.
MAX_LLM_ROUNDS = 1
LLM_CANDIDATES_PER_ROUND = 5


def _normalize_base(company_name: str) -> str:
    lowered = company_name.lower()
    lowered = re.sub(r"[^a-z0-9\s-]", "", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def generate_mechanical_candidates(company_name: str) -> list[str]:
    """No-space and hyphenated forms of the name, plus each form with a
    common corporate suffix concatenated on - directly (no separator) and
    hyphen-joined, since real tokens use both ("harnessinc" has no
    separator; some others do) - plus each form with an Indian legal-entity
    suffix concatenated on, directly only (see _INDIAN_LEGAL_SUFFIXES;
    "razorpaysoftwareprivatelimited" is the real pattern this models).
    Deduplicated, order preserved."""
    base = _normalize_base(company_name)
    no_space = base.replace(" ", "")
    hyphenated = base.replace(" ", "-")

    candidates = [no_space]
    if hyphenated != no_space:
        candidates.append(hyphenated)

    for suffix in _SUFFIXES:
        candidates.append(f"{no_space}{suffix}")
        candidates.append(f"{no_space}-{suffix}")

    for suffix in _INDIAN_LEGAL_SUFFIXES:
        candidates.append(f"{no_space}{suffix}")

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


@dataclass
class ScoutAttempt:
    token: str
    source: ATSSource
    origin: str  # "mechanical" | "llm"
    outcome: str  # "found" | "empty_board" | "not_found" | "error"
    job_count: Optional[int] = None
    error: Optional[str] = None


@dataclass
class ScoutResult:
    company_name: str
    success: bool
    config: Optional[CompanyConfig]
    attempts: list[ScoutAttempt]
    conclusion: str
    # Real boards with zero current jobs, found along the way - populated on
    # both success and failure (a later candidate can still win after an
    # earlier one turns up empty). Exposed as a field, not left to be parsed
    # back out of `conclusion`'s text, so batch mode (and any other caller)
    # can group on it directly.
    empty_boards: list[ScoutAttempt] = field(default_factory=list)


def _test_candidate(company_name: str, token: str, source: ATSSource, origin: str) -> ScoutAttempt:
    """The hard, verifiable signal the whole agent runs on: BoardNotFoundError
    means a 404 - the token is wrong for this source, not a judgment call,
    and never something the LLM is asked about. Any other ATSAdapterError (a
    genuine network/5xx failure after backoff already exhausted its own
    retries) is recorded as "error", not "not_found" - those aren't the same
    claim, and collapsing them would misreport an infra hiccup as "definitely
    not this company's board"."""
    fetch = ADAPTERS[source]
    try:
        jobs = fetch(company_name, token)
    except BoardNotFoundError:
        return ScoutAttempt(token=token, source=source, origin=origin, outcome="not_found")
    except ATSAdapterError as exc:
        return ScoutAttempt(token=token, source=source, origin=origin, outcome="error", error=str(exc))

    if jobs:
        return ScoutAttempt(token=token, source=source, origin=origin, outcome="found", job_count=len(jobs))
    # A real board, confirmed by a non-404 response - but zero current
    # openings contributes nothing to the pipeline. This is the failure mode
    # BoardNotFoundError exists to prevent from the other direction: don't
    # let a technically-real-but-useless board silently become a "success"
    # that lands in companies.yaml looking like a working company. See
    # docs/decisions.md.
    return ScoutAttempt(token=token, source=source, origin=origin, outcome="empty_board", job_count=0)


class _ScoutCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[str]


SCOUT_SYSTEM_INSTRUCTION = """You generate candidate ATS board-token slugs for a company, to test against Greenhouse, Lever, and Ashby job board APIs.

A board token is a short, URL-safe identifier used in that ATS's public API path. It is often, but not always, mechanically derivable from the company's public name. Patterns seen in real board tokens:
- the company name with no spaces or punctuation, unchanged
- a corporate suffix concatenated directly onto the name with no separator (e.g. a company called "Foo" using token "fooinc" or "foohq")
- an Indian company's full registered legal name, concatenated with no separator - e.g. Razorpay's real Greenhouse token is "razorpaysoftwareprivatelimited", not "razorpay". "Private Limited"/"Pvt Ltd" is the standard Indian company suffix; some use "Technologies"/"Software"/"Solutions" alone instead. If mechanical suffix guessing has already failed and the company looks Indian, try other concatenated legal-name shapes along these lines, not just the ones already tried.
- the company's literal domain name, including the TLD, used as the entire token (e.g. "foo.com" or "foo.io")
- an abbreviation, internal product name, or founder-derived string with no obvious relationship to the public company name
- a hyphenated form of a multi-word name

You will be given a company name and a list of tokens already tried and confirmed NOT to work. Suggest new, distinct token candidates not in that list, most-plausible first. Each candidate must be a single lowercase string using only letters, digits, hyphens, and periods - no spaces, no explanation."""

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"candidates": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["candidates"],
}


def _ask_llm_for_candidates(company_name: str, already_tried: list[str], client: LLMClient) -> list[str]:
    """One-shot: propose LLM_CANDIDATES_PER_ROUND new tokens, informed by
    every mechanical guess already ruled out. Raises LLMError or
    ValidationError on failure rather than swallowing it - scout() decides
    that a failed round means "nothing more to try", the same policy
    pipeline.py already applies when an Analyst call fails; the decision
    belongs at the orchestration layer, not buried in this helper."""
    already = {t.lower() for t in already_tried}
    user_message = (
        f"Company name: {company_name}\n"
        f"Already tried and failed (do not repeat these): {', '.join(sorted(already)) or '(none)'}\n"
        f"Suggest {LLM_CANDIDATES_PER_ROUND} new candidate board tokens."
    )
    raw = client.complete(SCOUT_SYSTEM_INSTRUCTION, user_message, _RESPONSE_SCHEMA)
    parsed = _ScoutCandidates.model_validate_json(raw)
    return [c.strip().lower() for c in parsed.candidates if c.strip() and c.strip().lower() not in already]


def _build_failure_conclusion(
    company_name: str, attempts: list[ScoutAttempt], empty_boards: list[ScoutAttempt], max_attempts: int
) -> str:
    tested = len(attempts)
    if empty_boards:
        boards = "; ".join(f"{a.token!r} on {a.source.value}" for a in empty_boards)
        return (
            f"No working board found for {company_name!r} after {tested} attempt(s). Found existing but "
            f"currently EMPTY board(s): {boards} - the board is real but has 0 open jobs right now, so it "
            f"wasn't returned as a working config (see EMPTY_BOARD handling). Worth spot-checking manually "
            f"or re-scouting later."
        )
    cap_note = " (stopped at the attempt cap, not fully exhausted)" if tested >= max_attempts else ""
    return (
        f"{company_name!r} does not appear to be on Greenhouse, Lever, or Ashby after {tested} "
        f"attempt(s){cap_note}. Likely on a different ATS, or not using one with a public board API."
    )


def scout(
    company_name: str,
    llm_client: Optional[LLMClient] = None,
    max_attempts: int = MAX_TOTAL_ATTEMPTS,
    max_llm_rounds: int = MAX_LLM_ROUNDS,
) -> ScoutResult:
    """Mechanical candidates first, always - the LLM is only ever consulted
    once that queue is empty and nothing has worked yet. Each candidate is
    tested against SOURCE_ORDER in turn, short-circuiting on the first
    non-404 response for that candidate (found or empty_board) - it's
    vanishingly unlikely the same token string is independently a *different*
    valid board on a second platform, so testing further sources for it
    adds requests without adding information. Returns as soon as a `found`
    (non-empty) board turns up; runs out via the mechanical+LLM queue going
    empty or max_attempts, whichever comes first."""
    queue = generate_mechanical_candidates(company_name)
    origin_by_token: dict[str, str] = {token: "mechanical" for token in queue}
    tried_tokens: list[str] = []
    attempts: list[ScoutAttempt] = []
    empty_boards: list[ScoutAttempt] = []
    llm_rounds = 0

    while len(attempts) < max_attempts:
        if not queue:
            if llm_client is None or llm_rounds >= max_llm_rounds:
                break
            llm_rounds += 1
            try:
                new_candidates = _ask_llm_for_candidates(company_name, tried_tokens, llm_client)
            except (LLMError, ValidationError):
                new_candidates = []
            if not new_candidates:
                break  # LLM has nothing new to offer - stop, don't loop on empty rounds
            for candidate in new_candidates:
                origin_by_token[candidate] = "llm"
            queue.extend(new_candidates)
            continue

        token = queue.pop(0)
        tried_tokens.append(token)
        origin = origin_by_token.get(token, "mechanical")

        for source in SOURCE_ORDER:
            if len(attempts) >= max_attempts:
                break
            attempt = _test_candidate(company_name, token, source, origin)
            attempts.append(attempt)

            if attempt.outcome == "found":
                config = CompanyConfig(
                    name=company_name,
                    ats=source,
                    token=token,
                    notes=f"found by Scout ({origin} candidate)",
                )
                conclusion = (
                    f"Found: token={token!r} on {source.value} ({attempt.job_count} job(s)), "
                    f"via a {origin} candidate, after {len(attempts)} attempt(s)."
                )
                return ScoutResult(
                    company_name=company_name,
                    success=True,
                    config=config,
                    attempts=attempts,
                    conclusion=conclusion,
                    empty_boards=empty_boards,
                )

            if attempt.outcome == "empty_board":
                empty_boards.append(attempt)
                break  # real board, but not a win - stop probing sources for this token, try the next one

    conclusion = _build_failure_conclusion(company_name, attempts, empty_boards, max_attempts)
    return ScoutResult(
        company_name=company_name,
        success=False,
        config=None,
        attempts=attempts,
        conclusion=conclusion,
        empty_boards=empty_boards,
    )


# Polite delay between companies in a batch, not just within one company's
# own request loop. Distinct from GEMINI_RATE_LIMITS pacing (that governs
# only the LLM fallback calls) and from adapters/base.py's backoff (that
# only reacts to a failure already happening) - this is about not hammering
# Greenhouse/Lever/Ashby with dozens of companies' worth of mechanical
# probing back-to-back. One unsupported company alone is ~45 requests; a
# batch of 50 unsupported companies would be ~2000+ without this.
BATCH_PACING_SECONDS = 3.0


def _load_batch_company_names(path: Path) -> list[str]:
    """One company name per line. Blank lines ignored, leading/trailing
    whitespace stripped - not validated further, generate_mechanical_
    candidates and the LLM fallback both tolerate arbitrary input."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def _is_already_configured(company_name: str, companies: list[CompanyConfig]) -> bool:
    lowered = company_name.strip().lower()
    return any(existing.name.strip().lower() == lowered for existing in companies)


@dataclass
class BatchResult:
    found: list[ScoutResult]
    empty_board: list[ScoutResult]
    not_supported: list[ScoutResult]
    skipped: list[str]  # company names already in companies.yaml - never scouted
    total_requests: int


def scout_batch(
    company_names: list[str],
    existing_companies: list[CompanyConfig],
    llm_client: Optional[LLMClient] = None,
    pacing_seconds: float = BATCH_PACING_SECONDS,
) -> BatchResult:
    """Scout every name not already in existing_companies, pacing between
    companies (not just within one company's own loop - see
    BATCH_PACING_SECONDS). Streams a line of progress per company as it
    completes, same principle as pipeline.py's per-job streaming: a batch
    over dozens of companies takes real wall-clock time, and losing
    visibility into what's already finished while waiting for the rest
    would be the same mistake made once already with the Analyst stage."""
    to_scout = [name for name in company_names if not _is_already_configured(name, existing_companies)]
    skipped = [name for name in company_names if _is_already_configured(name, existing_companies)]

    found: list[ScoutResult] = []
    empty_board: list[ScoutResult] = []
    not_supported: list[ScoutResult] = []
    total_requests = 0

    for i, name in enumerate(to_scout):
        if i > 0:
            time.sleep(pacing_seconds)

        result = scout(name, llm_client=llm_client)
        total_requests += len(result.attempts)

        if result.success:
            found.append(result)
            outcome = f"found: {result.config.token!r} on {result.config.ats.value}"
        elif result.empty_boards:
            empty_board.append(result)
            outcome = "empty board"
        else:
            not_supported.append(result)
            outcome = "not supported"

        print(f"  [{i + 1}/{len(to_scout)}, {len(result.attempts)} req] {name} -> {outcome}")

    return BatchResult(
        found=found, empty_board=empty_board, not_supported=not_supported, skipped=skipped, total_requests=total_requests
    )


def _found_as_yaml(results: list[ScoutResult]) -> str:
    """The `found` group formatted exactly like companies.yaml's own
    entries - safe_dump, not hand-built f-strings, so a company name with a
    colon or quote in it doesn't silently produce invalid YAML."""
    entries = []
    for result in results:
        config = result.config
        entry = {"name": config.name, "ats": config.ats.value, "token": config.token}
        if config.notes:
            entry["notes"] = config.notes
        entries.append(entry)
    return yaml.safe_dump(entries, sort_keys=False, default_flow_style=False, allow_unicode=True)


def print_batch_result(batch: BatchResult) -> None:
    if batch.skipped:
        print()
        print(f"Skipped ({len(batch.skipped)}, already in companies.yaml): {', '.join(batch.skipped)}")

    print()
    print(f"Found ({len(batch.found)}) - paste into companies.yaml:")
    if batch.found:
        print(_found_as_yaml(batch.found))
    else:
        print("  (none)")

    print(f"Empty board ({len(batch.empty_board)}) - a real board exists but has 0 jobs right now:")
    for result in batch.empty_board:
        boards = "; ".join(f"{a.token!r} on {a.source.value}" for a in result.empty_boards)
        print(f"  {result.company_name}: {boards}")

    print()
    print(f"Not on a supported ATS ({len(batch.not_supported)}):")
    for result in batch.not_supported:
        print(f"  {result.company_name}")

    print()
    print(f"Total requests used: {batch.total_requests}")


if __name__ == "__main__":
    import argparse
    import sys

    from config import GEMINI_MODEL_STAGE1, GEMINI_RATE_LIMITS
    from llm import GeminiClient

    # Company names aren't guaranteed ASCII, and Windows defaults stdout to
    # cp1252 when it isn't a real console - same fix as pipeline.py/evaluate.py.
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("companies", nargs="*", help="one or more company names to scout directly")
    parser.add_argument(
        "--batch",
        metavar="FILE",
        help="scout every company name in FILE (one per line), skipping ones already in companies.yaml",
    )
    args = parser.parse_args()

    if not args.companies and not args.batch:
        parser.error("pass one or more company names, or --batch FILE")

    rpm = GEMINI_RATE_LIMITS.get(GEMINI_MODEL_STAGE1, {}).get("rpm")
    client = GeminiClient(model=GEMINI_MODEL_STAGE1, requests_per_minute=rpm)

    if args.batch:
        from config import load_companies

        company_names = _load_batch_company_names(Path(args.batch))
        existing_companies = load_companies()
        print(f"Scouting {len(company_names)} name(s) from {args.batch!r} ({len(existing_companies)} already configured)...")
        batch_result = scout_batch(company_names, existing_companies, llm_client=client)
        print_batch_result(batch_result)
    else:
        for company_name in args.companies:
            result = scout(company_name, llm_client=client)

            print(f"\n=== {company_name} ===")
            for attempt in result.attempts:
                detail = f"jobs={attempt.job_count}" if attempt.job_count is not None else (attempt.error or "")
                print(
                    f"  [{attempt.origin:<10}] {attempt.token:<30} {attempt.source.value:<12} {attempt.outcome:<12} {detail}"
                )

            print(f"  -> {result.conclusion}")
            if result.success and result.config:
                print(f"  config: {result.config.model_dump()}")
            print(f"  requests used: {len(result.attempts)}")
