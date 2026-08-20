"""Compare stage-1 Analyst fit_score against ranking.py's embedding cosine
similarity and a random baseline, on the subset of hand-labeled jobs that
have a cached stage-1 result.

That subset is not the full label set - stage 1 only ever ran over jobs that
survived rule-filtering (see pipeline.py's print_analyst_stage1), so a job
labeled 'good' but rejected by a filter has no stage-1 score to compare.
Restricting all three rankings (embedding, stage-1, random) to exactly the
same overlap keeps the comparison apples-to-apples - see main()'s overlap-size
report for exactly how much of the label set that leaves out.

Reuses evaluate.py's label loading and metric helpers rather than
duplicating them - MRR, recall@k, and their random-baseline expectations
don't change meaning just because the ranking being scored does.
"""

import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from agents.analyst import SYSTEM_INSTRUCTION, _text_hash, prepare_resume_text
from config import GEMINI_MODEL_STAGE1
from db import AnalystResultRow, get_engine, job_posting_from_row
from extraction import extract_jd_requirements
from evaluate import (
    expected_mrr_random,
    expected_recall_at_k_random,
    load_labels,
    match_labels_to_db,
    mean_reciprocal_rank,
    rank_positions,
    recall_at_k,
)
from models import JobPosting
from ranking import rank_jobs


@dataclass
class ScoredJob:
    job: JobPosting
    fit_score: int


def stage1_overlap(session: Session, jobs_by_hash: dict[str, JobPosting]) -> dict[str, ScoredJob]:
    """Of the given jobs, keep only those with a cached, real stage-1
    result - recomputing the exact same cache key agents/analyst.py::
    analyze() would, not guessing at one. Read-only: never calls the LLM,
    so checking this costs nothing.

    Rows with verdict == "unscored" (see agents/analyst.py's is_unscored)
    are excluded, not included with their fabricated fit_score - a posting
    with no concrete technical requirements has nothing for the ranking
    comparison to measure, and letting it in would corrupt MRR/recall with
    a number that was never a real fit judgment."""
    resume_text = prepare_resume_text()
    overlap: dict[str, ScoredJob] = {}
    for content_hash, job in jobs_by_hash.items():
        requirements_text, _ = extract_jd_requirements(job.description)
        text_hash = _text_hash(GEMINI_MODEL_STAGE1, SYSTEM_INSTRUCTION, resume_text, requirements_text)
        row = session.get(AnalystResultRow, text_hash)
        if row is not None and row.verdict != "unscored":
            overlap[content_hash] = ScoredJob(job=job, fit_score=row.fit_score)
    return overlap


def stage1_rank(scored: dict[str, ScoredJob]) -> list[tuple[JobPosting, int]]:
    """Descending by fit_score. Ties keep insertion order (dict iteration
    order) rather than being broken by anything meaningful - fit_score is an
    integer 0-100, so ties are common and there's no secondary signal worth
    inventing here."""
    return sorted(((sj.job, sj.fit_score) for sj in scored.values()), key=lambda pair: pair[1], reverse=True)


RANKING_NAMES = ["Embedding", "Stage-1"]
RANDOM_BASELINE_NAME = "Random (expected)"


def print_metrics_table(report: "EvaluationReport") -> None:
    """Prints the same table as before, now reading from the computed
    report rather than recomputing the metrics itself - so the printed
    numbers and the ones GET /api/meta/evaluation serves are the same
    values, not two independent calculations that could drift."""
    print()
    header = f"{'Metric':<14}"
    for name in RANKING_NAMES:
        header += f"{name:>16}"
    header += f"{RANDOM_BASELINE_NAME:>20}"
    print(header)

    def row(label: str, values: dict[str, float]) -> str:
        line = f"{label:<14}"
        for name in RANKING_NAMES:
            line += f"{values[name]:>16.3f}"
        line += f"{values[RANDOM_BASELINE_NAME]:>20.3f}"
        return line

    print(row("MRR (good)", report.mrr))
    print(row("Recall@10", report.recall_at_10))
    print(row("Recall@20", report.recall_at_20))


@dataclass
class GoodJobPosition:
    """Where one 'good'-labeled job landed under each ranking."""

    company: str
    title: str
    embedding_position: int
    stage1_position: int


@dataclass
class EvaluationReport:
    """Everything main() prints, as data - so the same numbers can be served
    over HTTP (GET /api/meta/evaluation) without a second implementation of
    the evaluation, and without parsing stdout.

    Extracted from what was previously main()'s inline body; main() now
    calls this and prints exactly what it printed before, in the same order.
    The one behavioral difference: computation all happens before any
    printing rather than interleaved, so the first line appears after
    rank_jobs finishes instead of before it. Same text, same order, later.

    n == 0 means no labeled job has a cached stage-1 result to compare -
    a real state (nothing has been analyzed yet), not an error, and the
    metrics fields are left at their empty defaults rather than filled with
    zeros that would read as measured results."""

    label_counts: dict[str, int]
    total_labels: int
    overlap_counts: dict[str, int]
    n: int
    is_minority_of_label_set: bool

    good_positions: list[GoodJobPosition] = field(default_factory=list)
    mrr: dict[str, float] = field(default_factory=dict)  # {"Embedding": ..., "Stage-1": ..., "Random (expected)": ...}
    recall_at_10: dict[str, float] = field(default_factory=dict)
    recall_at_20: dict[str, float] = field(default_factory=dict)

    top_stage1_fit_score: Optional[int] = None
    top_stage1_company: Optional[str] = None
    top_stage1_title: Optional[str] = None
    top_stage1_label: Optional[str] = None


def compute_evaluation(session: Session) -> EvaluationReport:
    """The whole evaluation as data. Takes an open Session rather than
    opening its own, so a caller already inside one (the API) doesn't nest
    connections and a test can hand it a temp database.

    Heavy: rank_jobs imports sentence-transformers/torch and embeds every
    overlap job. That's why the API serves a precomputed JSON snapshot
    rather than calling this per request - see api/services/evaluation.py."""
    labels = load_labels()
    label_by_hash = {label.content_hash: label for label in labels}

    found, _missing = match_labels_to_db(session, labels)
    jobs_by_hash = {content_hash: job_posting_from_row(row) for content_hash, row in found.items()}

    overlap = stage1_overlap(session, jobs_by_hash)

    overlap_labels = Counter(label_by_hash[h].label for h in overlap)
    total_labels = Counter(label.label for label in labels)
    n = len(overlap)

    report = EvaluationReport(
        label_counts=dict(total_labels),
        total_labels=len(labels),
        overlap_counts=dict(overlap_labels),
        n=n,
        is_minority_of_label_set=n < len(labels) * 0.6,
    )
    if n == 0:
        return report

    overlap_jobs = [sj.job for sj in overlap.values()]
    embedding_ranked, _diagnostics = rank_jobs(overlap_jobs, session)

    embedding_positions = rank_positions(embedding_ranked)
    stage1_ranked = stage1_rank(overlap)
    stage1_positions = {job.content_hash: pos for pos, (job, _) in enumerate(stage1_ranked, start=1)}

    good_hashes = [h for h in overlap if label_by_hash[h].label == "good"]
    good_embedding_positions = sorted(embedding_positions[h] for h in good_hashes)
    good_stage1_positions = sorted(stage1_positions[h] for h in good_hashes)

    report.good_positions = [
        GoodJobPosition(
            company=label_by_hash[h].company,
            title=label_by_hash[h].title,
            embedding_position=embedding_positions[h],
            stage1_position=stage1_positions[h],
        )
        for h in sorted(good_hashes, key=lambda h: stage1_positions[h])
    ]

    by_ranking = {"Embedding": good_embedding_positions, "Stage-1": good_stage1_positions}
    report.mrr = {name: mean_reciprocal_rank(pos) for name, pos in by_ranking.items()}
    report.mrr["Random (expected)"] = expected_mrr_random(n)
    report.recall_at_10 = {name: recall_at_k(pos, 10, len(pos)) for name, pos in by_ranking.items()}
    report.recall_at_10["Random (expected)"] = expected_recall_at_k_random(10, n)
    report.recall_at_20 = {name: recall_at_k(pos, 20, len(pos)) for name, pos in by_ranking.items()}
    report.recall_at_20["Random (expected)"] = expected_recall_at_k_random(20, n)

    strong_hash = max(overlap, key=lambda h: overlap[h].fit_score)
    strong = overlap[strong_hash]
    report.top_stage1_fit_score = strong.fit_score
    report.top_stage1_company = strong.job.company
    report.top_stage1_title = strong.job.title
    report.top_stage1_label = label_by_hash[strong_hash].label

    return report


def _print_report(report: EvaluationReport) -> None:
    """Everything main() used to print inline. Split out so `--json` can
    print the same report it writes, from the same single computation,
    rather than either running the evaluation twice or having the two
    outputs drift."""
    print(f"Labels: {report.label_counts} ({report.total_labels} total)")
    print(
        f"Overlap with a stage-1 score: {report.n} / {report.total_labels} "
        f"({report.n / report.total_labels * 100:.0f}%) - "
        f"good {report.overlap_counts.get('good', 0)}/{report.label_counts.get('good', 0)}, "
        f"weak {report.overlap_counts.get('weak', 0)}/{report.label_counts.get('weak', 0)}, "
        f"no {report.overlap_counts.get('no', 0)}/{report.label_counts.get('no', 0)}"
    )
    if report.is_minority_of_label_set:
        print(
            "This is a minority of the label set - stage 1 only ever saw jobs that survived "
            "rule-filtering. Treat what follows as indicative, not conclusive: it describes how "
            "the rankers order the jobs both methods actually got to see, not the full labeled pool."
        )

    if report.n == 0:
        print("\nNo overlap - nothing to compare.")
        return

    print()
    print(f"'good' job positions out of {report.n} ranked overlap jobs:")
    print()
    print(f"{'company | title':<55}{'embedding':>12}{'stage-1':>10}")
    for pos in report.good_positions:
        name = f"{pos.company} | {pos.title}"[:54]
        print(f"{name:<55}{pos.embedding_position:>12}{pos.stage1_position:>10}")

    print_metrics_table(report)

    # The specific question asked: what did GoHighLevel's lone 'strong'
    # verdict get labeled?
    print()
    print(
        f"Highest stage-1 score: {report.top_stage1_fit_score} | {report.top_stage1_company} | "
        f"{report.top_stage1_title} | labeled: {report.top_stage1_label}"
    )


def main() -> None:
    engine = get_engine()
    with Session(engine) as session:
        report = compute_evaluation(session)
    _print_report(report)


if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "also write the computed metrics to data/evaluation_results.json, the snapshot "
            "GET /api/meta/evaluation serves (computing them per request would import torch "
            "and re-embed every labeled job - see api/services/evaluation.py)"
        ),
    )
    args = parser.parse_args()

    if args.json:
        # Computes once and both prints and writes, rather than running the
        # evaluation twice for the two outputs.
        from api.services.evaluation import write_snapshot

        engine = get_engine()
        with Session(engine) as session:
            report = compute_evaluation(session)
        _print_report(report)
        path = write_snapshot(report)
        print()
        print(f"Wrote snapshot to {path}")
    else:
        main()
