"""Evaluate ranking.py's embedding ranking against hand-labeled data.

Labels come from data/labels_todo.csv (see export_labels.py, label_cli.py) -
generated directly from job_postings, so every label's content_hash is the
full 64-character identity hash and should match this database exactly. The
earlier label set (data/labels.csv) was an external export that never joined
to this database at all; see docs/decisions.md.

With a small number of 'good' labels, top-10 precision is too noisy to trust
- a single job moving in or out of the top 10 swings it by a large margin.
Instead: raw positions of the 'good' jobs (so clustering vs. scattering is
visible, not just an aggregate), mean reciprocal rank, and recall@10/@20,
each compared against the closed-form expectation under a uniformly random
ranking of the same pool - not a single simulated shuffle, which would carry
its own luck.
"""

import csv
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from db import JobPostingRow, get_engine, job_posting_from_row
from models import JobPosting
from ranking import rank_jobs

LABELS_PATH = Path("data/labels_todo.csv")


@dataclass
class Label:
    content_hash: str
    company: str
    title: str
    label: str  # "good" | "weak" | "no"


def load_labels(path: Path = LABELS_PATH) -> list[Label]:
    with path.open(encoding="utf-8") as f:
        return [
            Label(row["content_hash"], row["company"], row["title"], row["label"])
            for row in csv.DictReader(f)
            if row["label"]  # skip any row left unlabeled
        ]


def match_labels_to_db(session: Session, labels: list[Label]) -> tuple[dict[str, JobPostingRow], list[Label]]:
    """Exact match on the full content_hash - labels_todo.csv is generated
    directly from this same table, so every label should match. Anything
    missing means the database changed since the export (a posting closed,
    a re-fetch altered something), not a join-format problem.

    Returns (found, missing): found maps content_hash -> the matching
    JobPostingRow (kept as the row, not converted to JobPosting yet, so its
    filter_passed/rejection_rule stay available for the filter-error check).
    """
    found: dict[str, JobPostingRow] = {}
    missing: list[Label] = []
    for label in labels:
        row = session.get(JobPostingRow, label.content_hash)
        if row is None:
            missing.append(label)
        else:
            found[label.content_hash] = row
    return found, missing


@dataclass
class FilterError:
    label: Label
    company: str
    title: str
    rejection_rule: Optional[str]


def find_filter_errors(
    found: dict[str, JobPostingRow], labels: list[Label]
) -> tuple[list[FilterError], list[FilterError]]:
    """Cross-checks human labels against the filter's actual current
    verdict (from the database, not the possibly-stale rejected_by column
    baked into the CSV at export time). Copies the fields it needs out of
    each JobPostingRow into a plain FilterError rather than holding onto the
    ORM row itself - the row is only valid while its session is open, and
    this result is meant to be printed after that session has closed.

    Returns (false_rejects, false_accepts):
    - false_rejects: labeled good/weak, but the filter rejected it
    - false_accepts: labeled no, but the filter passed it
    """
    label_by_hash = {label.content_hash: label for label in labels}
    false_rejects = []
    false_accepts = []
    for content_hash, row in found.items():
        label = label_by_hash[content_hash]
        if label.label in ("good", "weak") and not row.filter_passed:
            false_rejects.append(FilterError(label, row.company, row.title, row.rejection_rule))
        elif label.label == "no" and row.filter_passed:
            false_accepts.append(FilterError(label, row.company, row.title, row.rejection_rule))
    return false_rejects, false_accepts


def rank_positions(ranked: list[tuple[JobPosting, float]]) -> dict[str, int]:
    """1-indexed rank of every job in `ranked`, keyed by content_hash."""
    return {job.content_hash: position for position, (job, _) in enumerate(ranked, start=1)}


def mean_reciprocal_rank(positions: list[int]) -> float:
    """Mean of 1/rank across the given positions. Not the textbook
    single-relevant-item-per-query MRR - there's one ranked list here with
    several relevant ("good") items in it, not one query per item - but it's
    the natural analogue: how close to the top does a 'good' job land, on
    average, penalizing low ranks harshly (rank 20 contributes 0.05, not a
    gentle partial credit)."""
    return sum(1 / p for p in positions) / len(positions)


def recall_at_k(positions: list[int], k: int, total_relevant: int) -> float:
    return sum(1 for p in positions if p <= k) / total_relevant


def expected_recall_at_k_random(k: int, n: int) -> float:
    """Closed-form expectation of recall@k under a uniformly random ranking
    of n items: each item has an equal min(k, n)/n chance of landing in the
    top k, so this is exact and reproducible - not a single simulated
    shuffle, which would carry its own luck and not be worth comparing
    against as if it were the baseline."""
    return min(k, n) / n


def expected_mrr_random(n: int) -> float:
    """Closed-form expectation of 1/rank for a single item under a uniformly
    random ranking of n items is H_n / n (H_n = the nth harmonic number) -
    every item's rank is uniform over 1..n regardless of how many other
    relevant items exist, so this is also the expected MRR across any number
    of 'good' items, by symmetry."""
    harmonic_n = sum(1 / r for r in range(1, n + 1))
    return harmonic_n / n


def print_filter_errors(false_rejects: list[FilterError], false_accepts: list[FilterError]) -> None:
    print()
    print(f"Filter errors: {len(false_rejects)} false reject(s), {len(false_accepts)} false accept(s)")

    if false_rejects:
        print()
        print(f"Labeled good/weak but REJECTED by filters ({len(false_rejects)}):")
        for error in false_rejects:
            print(f"  ! [{error.label.label}] {error.company} | {error.title} | rejected_by={error.rejection_rule}")

    if false_accepts:
        print()
        print(f"Labeled no but PASSED filters ({len(false_accepts)}):")
        for error in false_accepts:
            print(f"  ! {error.company} | {error.title}")


def main() -> None:
    labels = load_labels()
    print(f"Loaded {len(labels)} labeled jobs: {dict(Counter(label.label for label in labels))}")

    engine = get_engine()
    with Session(engine) as session:
        found, missing = match_labels_to_db(session, labels)

        print()
        print(f"Found in DB and rankable: {len(found)} / {len(labels)}")
        if missing:
            print(f"Missing ({len(missing)}):")
            for label in missing:
                print(f"  ! {label.company} | {label.title} | labeled={label.label}")

        if not found:
            print("Nothing left to rank.")
            return

        false_rejects, false_accepts = find_filter_errors(found, labels)

        jobs_by_hash = {content_hash: job_posting_from_row(row) for content_hash, row in found.items()}
        ranked, _ = rank_jobs(list(jobs_by_hash.values()), session)

    n = len(ranked)
    positions_by_hash = rank_positions(ranked)
    label_by_hash = {label.content_hash: label for label in labels}

    scored = [(label_by_hash[content_hash], positions_by_hash[job.content_hash]) for content_hash, job in jobs_by_hash.items()]

    good = sorted((pair for pair in scored if pair[0].label == "good"), key=lambda pair: pair[1])

    total_good = Counter(label.label for label in labels)["good"]
    if len(good) < total_good:
        print()
        print(f"NOTE: only {len(good)}/{total_good} labeled 'good' jobs were found in the DB - metrics below are computed over those {len(good)}, not all {total_good}.")

    print()
    print(f"'good' job positions out of {n} ranked labeled jobs:")
    for label, pos in good:
        print(f"  #{pos:>3} / {n}  {label.company} | {label.title}")

    good_positions = [pos for _, pos in good]
    mrr = mean_reciprocal_rank(good_positions)
    r10 = recall_at_k(good_positions, 10, len(good_positions))
    r20 = recall_at_k(good_positions, 20, len(good_positions))

    expected_mrr = expected_mrr_random(n)
    expected_r10 = expected_recall_at_k_random(10, n)
    expected_r20 = expected_recall_at_k_random(20, n)

    print()
    print(f"{'Metric':<20}{'Embedding rank':>16}{'Random (expected)':>20}")
    print(f"{'MRR (good jobs)':<20}{mrr:>16.3f}{expected_mrr:>20.3f}")
    print(f"{'Recall@10':<20}{r10:>16.3f}{expected_r10:>20.3f}")
    print(f"{'Recall@20':<20}{r20:>16.3f}{expected_r20:>20.3f}")

    print_filter_errors(false_rejects, false_accepts)


if __name__ == "__main__":
    # company/title text from job boards isn't guaranteed ASCII (em-dashes,
    # curly quotes, etc.) - Windows' console defaults to cp1252, which can't
    # encode all of it, so this would otherwise crash mid-report on some
    # job's title rather than print the rest of the evaluation.
    sys.stdout.reconfigure(encoding="utf-8")
    main()
