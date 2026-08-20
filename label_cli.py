"""Terminal labeling tool for data/labels_todo.csv (see export_labels.py).

Presents each unlabeled row one at a time and writes the label back to the
CSV immediately after every answer - not batched at the end - so quitting
partway (Ctrl+C included) never loses more than the row currently on screen.
The next run picks up exactly where this one left off, since already-labeled
rows are simply skipped over.

Deliberately never shows `rejected_by`: this label set exists to check
whether the filter's own verdicts are correct. Showing the filter's decision
while asking a human to judge the same job would anchor the answer toward
agreeing with it, defeating the point of an independent check.
"""

import csv
import sys
from pathlib import Path
from typing import Callable

LABELS_PATH = Path("data/labels_todo.csv")

_KEY_TO_LABEL = {"g": "good", "w": "weak", "n": "no"}


def load_rows(path: Path = LABELS_PATH) -> tuple[list[dict], list[str]]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames)


def save_rows(rows: list[dict], fieldnames: list[str], path: Path = LABELS_PATH) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def format_row(row: dict, position: int, total: int) -> str:
    return (
        f"\n{'-' * 70}\n"
        f"[{position} / {total}]\n"
        f"{row['company']} | {row['title']} | {row['location']}\n\n"
        f"{row['description_excerpt']}\n"
        f"{'-' * 70}"
    )


def prompt_label(input_fn: Callable[[str], str] = input, print_fn: Callable[[str], None] = print) -> str:
    while True:
        answer = input_fn("g=good  w=weak  n=no  s=skip > ").strip().lower()
        if answer in _KEY_TO_LABEL or answer == "s":
            return answer
        print_fn(f"Unrecognized input {answer!r} - type g, w, n, or s.")


def run_labeling_session(
    rows: list[dict],
    fieldnames: list[str],
    path: Path,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Walks unlabeled rows in file order. A skip leaves the row untouched
    (blank label), so it's simply presented again on the next run - not
    tracked as a distinct "permanently skipped" state."""
    total = len(rows)

    for position, row in enumerate(rows, start=1):
        if row["label"]:
            continue

        print_fn(format_row(row, position, total))
        answer = prompt_label(input_fn, print_fn)

        if answer == "s":
            continue

        row["label"] = _KEY_TO_LABEL[answer]
        save_rows(rows, fieldnames, path)


def main() -> None:
    rows, fieldnames = load_rows()
    total = len(rows)
    unlabeled = sum(1 for row in rows if not row["label"])

    if unlabeled == 0:
        print(f"All {total} rows already labeled.")
        return

    print(f"{unlabeled} of {total} rows need labeling. Ctrl+C anytime - progress is saved after every answer.")

    try:
        run_labeling_session(rows, fieldnames, LABELS_PATH)
    except KeyboardInterrupt:
        print("\nStopped. Progress saved.")
        return

    print(f"\nDone - all {total} rows labeled.")


if __name__ == "__main__":
    # See pipeline.py for why: job titles/descriptions aren't ASCII-only,
    # and Windows defaults non-console stdout to cp1252 rather than UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    main()
