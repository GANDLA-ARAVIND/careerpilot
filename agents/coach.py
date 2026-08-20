"""Coach agent: runs weekly over the accumulated archive, answering
questions the database can't answer with a single query because the answer
is either buried in prose (needs retrieval) or spread across structured
rows in a way nothing already aggregates (needs SQL). Two fixed report
functions, not an open-ended free-text router - Coach runs weekly per
CLAUDE.md, it isn't a chat interface. See docs/decisions.md for which of
the three original candidate questions needed which approach.
"""

import json
from collections import Counter
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from agents.analyst import prepare_resume_text
from config import GEMINI_MODEL_STAGE1
from db import AnalystResultRow, JobPostingRow, job_posting_from_row
from extraction import extract_jd_requirements
from filters import filter_jobs
from llm import LLMClient
from models import JobPosting
from rag import RetrievalResult, retrieve

DEFAULT_RETRIEVAL_K = 30

# ---------------------------------------------------------------------------
# Q1: "across jobs scored below N, which skills appear most often in
# missing_skills?" - pure SQL/aggregation, no LLM, no retrieval.
# ---------------------------------------------------------------------------


@dataclass
class MissingSkillsReport:
    threshold: int
    job_count: int
    skill_counts: list[tuple[str, int]]  # most common first


def missing_skills_below(session: Session, threshold: int) -> MissingSkillsReport:
    """missing_skills is already structured on AnalystResultRow - this is a
    WHERE clause and a Counter over decoded JSON lists, nothing more.

    Deliberately restricted to model == GEMINI_MODEL_STAGE1, not "whichever
    score is available" or "prefer stage-2 when present": stage 2 only ever
    covers the top STAGE2_TOP_N (15) jobs by stage-1 rank, so mixing stage-1
    and stage-2 scores into one aggregate would mean different rows in the
    same population were judged by different models depending on where they
    ranked - a consistency problem for an aggregate, even though stage-2 is
    individually the more trustworthy score for any single job. Aggregating
    needs every row scored the same way more than it needs each row's best
    available score. See docs/decisions.md.

    Also excludes verdict == "unscored" rows (see agents/analyst.py's
    is_unscored): those have no real fit_score to compare against
    `threshold` in the first place - a posting with no concrete technical
    requirements isn't "scored below 40", it was never really scored, and
    counting it here would silently reintroduce the exact bug this outcome
    exists to prevent."""
    rows = (
        session.query(AnalystResultRow)
        .filter(
            AnalystResultRow.model == GEMINI_MODEL_STAGE1,
            AnalystResultRow.fit_score < threshold,
            AnalystResultRow.verdict != "unscored",
        )
        .all()
    )

    counts: Counter = Counter()
    for row in rows:
        for skill in json.loads(row.missing_skills):
            counts[skill] += 1

    return MissingSkillsReport(threshold=threshold, job_count=len(rows), skill_counts=counts.most_common())


def print_missing_skills_report(report: MissingSkillsReport) -> None:
    print()
    print(f"Jobs scored below {report.threshold} on stage-1 fit_score: {report.job_count}")
    if not report.job_count:
        print("Nothing to aggregate.")
        return

    print()
    print("Most common missing_skills:")
    for skill, count in report.skill_counts[:20]:
        print(f"  {count:>3}  {skill}")


# ---------------------------------------------------------------------------
# Q2/Q3: "which technologies show up repeatedly that I don't have" / "the
# gap between what I have and what the market asks for" - the same
# retrieval-augmented path, since neither has a structured field covering
# the full population (most postings never reach the Analyst at all).
# ---------------------------------------------------------------------------


def load_market_population(session: Session) -> list[JobPosting]:
    """The 'India-based fresher roles' population Q2/Q3 operate over -
    exactly filters.py's rule-filter survivors, recomputed fresh against
    every stored posting rather than trusting the possibly-stale
    filter_passed column. Same policy pipeline.py's --no-fetch mode already
    uses, for the same reason: filters.py can get tuned after a job was
    fetched, and a stored flag has no way to know that happened."""
    rows = session.query(JobPostingRow).all()
    jobs = [job_posting_from_row(row) for row in rows]
    kept, _rejected_by = filter_jobs(jobs)
    return kept


SYSTEM_INSTRUCTION = """You are analyzing a sample of real job postings' extracted requirements text against a candidate's resume, to answer a specific question about skill gaps or market demand.

Base your answer only on the provided job excerpts and resume text - do not invent postings or requirements that are not shown. The excerpts are a sample, not the full population; if the sample is small or doesn't fully answer the question, say so plainly rather than generalizing beyond what's shown.

Answer directly and concretely - name specific technologies or skills where relevant. Do not give a vague summary that could apply to any job search."""

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"answer": {"type": "STRING"}},
    "required": ["answer"],
}


class _MarketGapAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


@dataclass
class MarketGapReport:
    question: str
    retrieval: RetrievalResult
    answer: str


def market_gap(
    question: str, session: Session, llm_client: LLMClient, k: int = DEFAULT_RETRIEVAL_K
) -> MarketGapReport:
    """Q2/Q3: retrieval scoped to the India-fresher-role population
    (deterministic, via filter_jobs - not fuzzy), then ranked by similarity
    to the QUESTION, never the resume - see rag.py's module docstring for
    why resume-similarity retrieval would work against a gap analysis.
    Resume text enters only here, at synthesis, as something the LLM
    compares the retrieved chunks against - it never influences which
    chunks get retrieved."""
    jobs = load_market_population(session)

    # Dedupe on description_hash - the same posting can recur across
    # multiple fetch nights with unchanged text, and a duplicate chunk would
    # just spend two retrieval slots on identical content.
    text_by_hash: dict[str, str] = {}
    for job in jobs:
        if job.description_hash not in text_by_hash:
            requirements_text, _extracted = extract_jd_requirements(job.description)
            text_by_hash[job.description_hash] = requirements_text

    candidate_texts = list(text_by_hash.values())
    retrieval = retrieve(question, candidate_texts, session, k)

    resume_text = prepare_resume_text()
    chunks_block = "\n\n---\n\n".join(text for text, _score in retrieval.chunks)
    user_message = (
        f"QUESTION: {question}\n\n"
        f"CANDIDATE'S RESUME (skills and projects):\n{resume_text}\n\n"
        f"SAMPLE OF {retrieval.retrieved_count} JOB REQUIREMENT EXCERPT(S) "
        f"(retrieved from a pool of {retrieval.pool_size} matching postings):\n{chunks_block}"
    )

    raw = llm_client.complete(SYSTEM_INSTRUCTION, user_message, _RESPONSE_SCHEMA)
    answer = _MarketGapAnswer.model_validate_json(raw).answer

    return MarketGapReport(question=question, retrieval=retrieval, answer=answer)


def print_market_gap_report(report: MarketGapReport) -> None:
    r = report.retrieval
    print()
    noop_note = " - retrieval is a no-op at this scale, the whole pool came back" if r.is_noop else ""
    print(f"Retrieved {r.retrieved_count} of {r.pool_size} candidate posting(s){noop_note}")
    print()
    print(f"Q: {report.question}")
    print()
    print(report.answer)


if __name__ == "__main__":
    import argparse
    import sys

    from config import GEMINI_RATE_LIMITS
    from db import get_engine
    from llm import GeminiClient

    # Skills/tech names and JD text aren't guaranteed ASCII - same fix as
    # pipeline.py/evaluate.py/agents/scout.py.
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--missing-skills", action="store_true", help="Q1: skill frequency among low-scoring jobs")
    parser.add_argument("--threshold", type=int, default=40, help="with --missing-skills, the fit_score cutoff")
    parser.add_argument("--market-gap", metavar="QUESTION", help="Q2/Q3: retrieval-augmented market/gap question")
    parser.add_argument("--k", type=int, default=DEFAULT_RETRIEVAL_K, help="with --market-gap, chunks to retrieve")
    args = parser.parse_args()

    if not args.missing_skills and not args.market_gap:
        parser.error("pass --missing-skills and/or --market-gap")

    engine = get_engine()
    with Session(engine) as session:
        if args.missing_skills:
            report = missing_skills_below(session, args.threshold)
            print_missing_skills_report(report)

        if args.market_gap:
            rpm = GEMINI_RATE_LIMITS.get(GEMINI_MODEL_STAGE1, {}).get("rpm")
            client = GeminiClient(model=GEMINI_MODEL_STAGE1, requests_per_minute=rpm)
            report = market_gap(args.market_gap, session, client, k=args.k)
            print_market_gap_report(report)
