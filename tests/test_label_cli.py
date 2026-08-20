import csv

import pytest

from label_cli import format_row, load_rows, run_labeling_session, save_rows

FIELDNAMES = ["content_hash", "company", "title", "location", "rejected_by", "description_excerpt", "label"]


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _row(**overrides):
    defaults = dict(
        content_hash="a" * 64,
        company="Acme",
        title="Backend Engineer",
        location="Bangalore, Karnataka",
        rejected_by="",
        description_excerpt="We need a backend engineer with Python experience.",
        label="",
    )
    defaults.update(overrides)
    return defaults


def _answers(sequence):
    """A canned input_fn: returns each item in sequence in turn."""
    it = iter(sequence)
    return lambda prompt: next(it)


def test_load_and_save_roundtrip(tmp_path):
    path = tmp_path / "labels.csv"
    rows = [_row(content_hash="a" * 64), _row(content_hash="b" * 64, label="good")]
    _write_csv(path, rows)

    loaded, fieldnames = load_rows(path)
    assert fieldnames == FIELDNAMES
    assert loaded == rows

    save_rows(loaded, fieldnames, path)
    reloaded, _ = load_rows(path)
    assert reloaded == rows


def test_answers_are_written_as_full_words_not_letter_codes(tmp_path):
    """evaluate.py checks label == "good" (the literal word) - the CLI must
    write that, not the single-letter key the user typed."""
    path = tmp_path / "labels.csv"
    rows = [_row(content_hash="a" * 64), _row(content_hash="b" * 64), _row(content_hash="c" * 64)]
    _write_csv(path, rows)

    input_fn = _answers(["g", "w", "n"])
    run_labeling_session(rows, FIELDNAMES, path, input_fn=input_fn, print_fn=lambda s: None)

    assert [row["label"] for row in rows] == ["good", "weak", "no"]


def test_saved_after_every_answer_not_batched_at_the_end(tmp_path):
    """The whole point: if the session stops after the first answer, the
    first row's label must already be on disk, not just held in memory."""
    path = tmp_path / "labels.csv"
    rows = [_row(content_hash="a" * 64), _row(content_hash="b" * 64)]
    _write_csv(path, rows)

    seen_on_disk_after_first_answer = {}

    answers = iter(["g", "w"])

    def input_fn(prompt):
        answer = next(answers)
        if answer == "w":
            # about to answer the second row - check the first row's label
            # already landed on disk before this second answer is even given
            on_disk, _ = load_rows(path)
            seen_on_disk_after_first_answer["label"] = on_disk[0]["label"]
        return answer

    run_labeling_session(rows, FIELDNAMES, path, input_fn=input_fn, print_fn=lambda s: None)

    assert seen_on_disk_after_first_answer["label"] == "good"


def test_interrupt_after_first_answer_preserves_it(tmp_path):
    """A KeyboardInterrupt raised while answering row 2 must not undo the
    save that already happened for row 1."""
    path = tmp_path / "labels.csv"
    rows = [_row(content_hash="a" * 64), _row(content_hash="b" * 64)]
    _write_csv(path, rows)

    answers = iter(["g"])

    def input_fn(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_labeling_session(rows, FIELDNAMES, path, input_fn=input_fn, print_fn=lambda s: None)

    on_disk, _ = load_rows(path)
    assert on_disk[0]["label"] == "good"
    assert on_disk[1]["label"] == ""


def test_skip_leaves_row_unlabeled_and_it_is_re_presented_next_session(tmp_path):
    path = tmp_path / "labels.csv"
    rows = [_row(content_hash="a" * 64), _row(content_hash="b" * 64)]
    _write_csv(path, rows)

    run_labeling_session(rows, FIELDNAMES, path, input_fn=_answers(["s", "n"]), print_fn=lambda s: None)
    assert rows[0]["label"] == ""
    assert rows[1]["label"] == "no"

    prompted = []
    rows_again, fieldnames = load_rows(path)
    run_labeling_session(
        rows_again,
        fieldnames,
        path,
        input_fn=lambda prompt: (prompted.append(True) or "g"),
        print_fn=lambda s: None,
    )
    assert len(prompted) == 1  # only the still-blank row was presented
    assert rows_again[0]["label"] == "good"


def test_already_labeled_rows_are_never_prompted(tmp_path):
    path = tmp_path / "labels.csv"
    rows = [_row(content_hash="a" * 64, label="good"), _row(content_hash="b" * 64)]
    _write_csv(path, rows)

    calls = []
    run_labeling_session(
        rows, FIELDNAMES, path, input_fn=lambda prompt: (calls.append(True) or "n"), print_fn=lambda s: None
    )

    assert len(calls) == 1
    assert rows[0]["label"] == "good"  # untouched
    assert rows[1]["label"] == "no"


def test_invalid_input_reprompts_without_advancing(tmp_path):
    path = tmp_path / "labels.csv"
    rows = [_row(content_hash="a" * 64)]
    _write_csv(path, rows)

    run_labeling_session(rows, FIELDNAMES, path, input_fn=_answers(["x", "?", "g"]), print_fn=lambda s: None)
    assert rows[0]["label"] == "good"


def test_format_row_never_includes_rejected_by():
    """Anti-anchoring-bias check: the filter's own verdict must never appear
    in what's shown to the human labeler, or the label stops being an
    independent check of the filter."""
    row = _row(rejected_by="seniority", title="Senior Backend Engineer")
    output = format_row(row, position=1, total=1)
    assert "seniority" not in output
    assert "rejected" not in output.lower()


def test_format_row_includes_company_title_location_and_description():
    row = _row(company="Acme", title="Backend Engineer", location="Bangalore, Karnataka")
    output = format_row(row, position=3, total=10)
    assert "Acme" in output
    assert "Backend Engineer" in output
    assert "Bangalore, Karnataka" in output
    assert row["description_excerpt"] in output
    assert "3 / 10" in output
