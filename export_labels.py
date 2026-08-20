"""Export a fresh labeling sheet from the current database, for hand-labeling
ground truth to evaluate filters.py and ranking.py against (see evaluate.py).

The previous label set (data/labels.csv) was built by an external process
against a different company roster and never matched this database - 0/96
joined by content_hash. See docs/decisions.md. Ground truth has to come from
the actual system being evaluated: this script reads content_hash straight
out of job_postings, so there's nothing to drift out of sync with.
"""

import csv
import random
from pathlib import Path

from sqlalchemy.orm import Session

from db import JobPostingRow, get_engine

OUTPUT_PATH = Path("data/labels_todo.csv")
DESCRIPTION_EXCERPT_LENGTH = 300

# How many rejected jobs to sample per rule, weighted toward rules most
# likely to be wrong over rules that are almost always obviously correct.
# not_allowlisted is the main judgment call left (a title not matching any
# known allow-pattern) - that's where a human labeling pass finds real
# filter mistakes. seniority rejects ("Senior Software Engineer") are rarely
# wrong, so a uniform draw across thousands of rejects (which would be
# dominated by seniority) would waste most of the label budget confirming
# the obvious. experience_too_high is gone - it's no longer a rejection
# rule at all (see docs/decisions.md); the parsed figure is advisory now,
# displayed alongside every job rather than filtered on.
REJECTED_SAMPLE_TARGETS = {
    "not_allowlisted": 40,
    "non_engineering": 10,
    "not_india": 10,
    "seniority": 10,
}


def sample_rejected(session: Session, rng: random.Random = random) -> list[JobPostingRow]:
    """~100 rejected rows total, weighted per REJECTED_SAMPLE_TARGETS rather
    than drawn uniformly. `rng` defaults to the `random` module itself (its
    module-level functions share the same signature as a Random instance),
    swappable for a seeded random.Random(...) in tests for determinism."""
    sampled: list[JobPostingRow] = []
    for rule, target in REJECTED_SAMPLE_TARGETS.items():
        # order_by is what makes a seeded rng reproducible. rng.sample
        # picks positions, so its output depends on the pool's order as
        # much as on the seed - and Postgres does not guarantee any order
        # without ORDER BY. Unordered, the same seed would produce a
        # different label sheet on every export, quietly undermining the
        # one artifact the whole evaluation rests on (see CLAUDE.md).
        pool = (
            session.query(JobPostingRow)
            .filter(JobPostingRow.rejection_rule == rule)
            .order_by(JobPostingRow.content_hash)
            .all()
        )
        sampled.extend(rng.sample(pool, min(target, len(pool))))
    return sampled


def export(session: Session, path: Path = OUTPUT_PATH, rng: random.Random = random) -> int:
    """Writes the labeling sheet (all passed jobs + the weighted rejected
    sample) and returns the row count written."""
    passed = (
        session.query(JobPostingRow)
        .filter(JobPostingRow.filter_passed.is_(True))
        .order_by(JobPostingRow.content_hash)  # deterministic sheet order - see sample_rejected
        .all()
    )
    rejected = sample_rejected(session, rng)
    rows = passed + rejected

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["content_hash", "company", "title", "location", "rejected_by", "description_excerpt", "label"])
        for row in rows:
            writer.writerow(
                [
                    row.content_hash,
                    row.company,
                    row.title,
                    row.location or "",
                    row.rejection_rule or "",
                    row.description[:DESCRIPTION_EXCERPT_LENGTH],
                    "",
                ]
            )

    return len(rows)


if __name__ == "__main__":
    engine = get_engine()
    with Session(engine) as session:
        count = export(session)
    print(f"Wrote {count} rows to {OUTPUT_PATH}")
