"""Serves the evaluation numbers from a precomputed snapshot.

Computing them live would import sentence-transformers/torch into the API
process (hundreds of megabytes, ~10s) and re-embed every labeled job on
each request. The numbers only change when the labels or the analyst
results change - both rare, both deliberate - so a snapshot regenerated on
demand is the honest shape for this data.

Regenerate with:  python evaluate_stage1.py --json

If the snapshot doesn't exist, the endpoint reports available=False rather
than zeros. A recruiter-facing metrics page showing 0.000 across the board
would read as "measured, and terrible" when the truth is "not yet run".
"""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

EVALUATION_SNAPSHOT_PATH = Path("data/evaluation_results.json")

# Stated with the numbers, every time, rather than left for a reader to
# infer. These are real limitations of this evaluation, and a metrics page
# that omits them is overclaiming.
CAVEATS = [
    "Stage 1 only ever ran over jobs that survived rule filtering, so the labeled jobs it "
    "never saw are excluded from all three rankings - this compares orderings of the same "
    "overlap, not performance on the full labeled set.",
    "The 'good' label count in that overlap is small; MRR and recall@k are correspondingly noisy.",
    "Random is the expected value for a uniformly random ordering of the same overlap, not a "
    "sampled run.",
]


def write_snapshot(report, path: Path = EVALUATION_SNAPSHOT_PATH) -> Path:
    """Serializes an evaluate_stage1.EvaluationReport. Kept here rather
    than in evaluate_stage1.py so the CLI script doesn't grow a dependency
    on where the API happens to keep its cache."""
    payload = asdict(report)
    payload["generated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_snapshot(path: Path = EVALUATION_SNAPSHOT_PATH) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
