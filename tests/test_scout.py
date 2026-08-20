import json

import pytest

import agents.scout as scout_module
from adapters.base import ATSAdapterError, BoardNotFoundError
from agents.scout import (
    ScoutAttempt,
    _found_as_yaml,
    _is_already_configured,
    _load_batch_company_names,
    generate_mechanical_candidates,
    scout,
    scout_batch,
)
from llm import LLMClient, LLMError
from models import ATSSource, CompanyConfig, JobPosting


def _make_job(company: str, title: str = "Software Engineer") -> JobPosting:
    return JobPosting(
        source=ATSSource.ASHBY,
        source_job_id="1",
        company=company,
        title=title,
        location="Bangalore",
        description="Requirements: Python.",
        url="https://example.com/jobs/1",
    )


class FakeLLMClient(LLMClient):
    def __init__(self, batches: list[list[str]]):
        """Each call to complete() returns the next batch in order, as a
        JSON-encoded {"candidates": [...]}. Running out of batches raises,
        same as a real client would on a broken connection - tests that
        expect only N rounds pass exactly N batches."""
        self._batches = list(batches)
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "fake-model"

    def complete(self, system_instruction: str, user_message: str, response_schema: dict) -> str:
        self.calls += 1
        batch = self._batches.pop(0)
        return json.dumps({"candidates": batch})


class RaisingLLMClient(LLMClient):
    @property
    def model_name(self) -> str:
        return "fake-model"

    def complete(self, system_instruction: str, user_message: str, response_schema: dict) -> str:
        raise LLMError("simulated outage")


@pytest.fixture
def patched_adapters(monkeypatch):
    """Replaces scout.ADAPTERS with fakes keyed by (token, source) -> jobs.
    Returns a dict the test populates before calling scout()."""
    working: dict[tuple[str, ATSSource], list[JobPosting]] = {}
    error_tokens: dict[tuple[str, ATSSource], Exception] = {}

    def make_fetch(source: ATSSource):
        def fetch(company: str, token: str) -> list[JobPosting]:
            key = (token, source)
            if key in error_tokens:
                raise error_tokens[key]
            if key in working:
                return working[key]
            raise BoardNotFoundError(source=source.value, company=company, board_token=token)

        return fetch

    fake_adapters = {source: make_fetch(source) for source in ATSSource}
    monkeypatch.setattr(scout_module, "ADAPTERS", fake_adapters)
    return working, error_tokens


# ---------------------------------------------------------------------------
# generate_mechanical_candidates
# ---------------------------------------------------------------------------


def test_mechanical_candidates_include_concatenated_suffix():
    """Harness's real token, "harnessinc", is the company name with "inc"
    concatenated directly - no separator."""
    candidates = generate_mechanical_candidates("Harness")
    assert "harnessinc" in candidates


def test_mechanical_candidates_include_hyphenated_multiword_name():
    """Reo Dev's real token, "reo-dev", is just the two-word name hyphenated."""
    candidates = generate_mechanical_candidates("Reo Dev")
    assert "reo-dev" in candidates


def test_mechanical_candidates_never_guess_domain_suffixes():
    """Flagright's real token, "flagright.com", is a literal domain used as
    the token - deliberately NOT mechanically generated (see module
    docstring); this is the case the LLM fallback exists for."""
    candidates = generate_mechanical_candidates("Flagright")
    assert "flagright.com" not in candidates
    assert all("." not in c for c in candidates)


def test_mechanical_candidates_deduplicated_for_single_word_names():
    candidates = generate_mechanical_candidates("Harness")
    assert len(candidates) == len(set(candidates))


def test_mechanical_candidates_include_indian_legal_entity_suffixes():
    """Razorpay's real Greenhouse token, "razorpaysoftwareprivatelimited",
    is the company name with its full registered legal-entity suffix
    concatenated on - the case that motivated _INDIAN_LEGAL_SUFFIXES."""
    candidates = generate_mechanical_candidates("Razorpay")
    assert "razorpaysoftwareprivatelimited" in candidates


def test_mechanical_candidates_indian_legal_entity_suffixes_are_concatenated_only():
    """Unlike _SUFFIXES, the Indian legal-entity suffixes never get a
    hyphenated variant - real tokens use one continuous string, and a
    hyphenated form would just be a wasted request no real token uses."""
    candidates = generate_mechanical_candidates("Razorpay")
    assert "razorpay-softwareprivatelimited" not in candidates
    assert "razorpay-privatelimited" not in candidates


def test_mechanical_candidates_still_deduplicated_with_indian_suffixes():
    """"labs" appears in both _SUFFIXES and _INDIAN_LEGAL_SUFFIXES by
    design (see the comment on _INDIAN_LEGAL_SUFFIXES) - must not produce a
    duplicate "razorpaylabs" candidate or double-count it as 2 attempts."""
    candidates = generate_mechanical_candidates("Razorpay")
    assert len(candidates) == len(set(candidates))
    assert candidates.count("razorpaylabs") == 1


# ---------------------------------------------------------------------------
# scout() - mechanical success
# ---------------------------------------------------------------------------


def test_scout_succeeds_mechanically_without_calling_llm(patched_adapters):
    working, _ = patched_adapters
    working[("harnessinc", ATSSource.GREENHOUSE)] = [_make_job("Harness")]

    class FailIfCalled(LLMClient):
        @property
        def model_name(self) -> str:
            return "fake-model"

        def complete(self, *args, **kwargs) -> str:
            raise AssertionError("LLM should not be called when mechanical succeeds")

    result = scout("Harness", llm_client=FailIfCalled())

    assert result.success is True
    assert result.config.token == "harnessinc"
    assert result.config.ats == ATSSource.GREENHOUSE
    assert all(a.origin == "mechanical" for a in result.attempts)


def test_scout_source_order_is_ashby_then_lever_then_greenhouse(patched_adapters):
    working, _ = patched_adapters
    working[("reodev", ATSSource.GREENHOUSE)] = [_make_job("Reo Dev")]

    result = scout("Reo Dev")

    # "reodev" is the first mechanical candidate tried; its three attempts
    # must appear in this exact source order before the winning greenhouse hit.
    reodev_attempts = [a for a in result.attempts if a.token == "reodev"]
    assert [a.source for a in reodev_attempts] == [ATSSource.ASHBY, ATSSource.LEVER, ATSSource.GREENHOUSE]
    assert reodev_attempts[0].outcome == "not_found"
    assert reodev_attempts[1].outcome == "not_found"
    assert reodev_attempts[2].outcome == "found"


def test_scout_stops_testing_further_sources_after_a_hit(patched_adapters):
    """Once a token gets a non-404 on one source, the remaining sources for
    that same token are never tried - short-circuit, not just early return."""
    working, _ = patched_adapters
    working[("reodev", ATSSource.ASHBY)] = [_make_job("Reo Dev")]

    result = scout("Reo Dev")

    reodev_attempts = [a for a in result.attempts if a.token == "reodev"]
    assert len(reodev_attempts) == 1
    assert reodev_attempts[0].source == ATSSource.ASHBY


# ---------------------------------------------------------------------------
# scout() - LLM fallback
# ---------------------------------------------------------------------------


def test_scout_falls_back_to_llm_when_mechanical_exhausted(patched_adapters):
    working, _ = patched_adapters
    working[("flagright.com", ATSSource.ASHBY)] = [_make_job("Flagright")]
    llm_client = FakeLLMClient(batches=[["flagright.com", "flagrightapp"]])

    result = scout("Flagright", llm_client=llm_client)

    assert result.success is True
    assert result.config.token == "flagright.com"
    assert llm_client.calls == 1
    found_attempt = next(a for a in result.attempts if a.outcome == "found")
    assert found_attempt.origin == "llm"


def test_scout_only_calls_llm_after_mechanical_queue_is_empty(patched_adapters):
    """The LLM must never be consulted while mechanical candidates remain -
    the core ordering requirement."""
    working, _ = patched_adapters
    working[("flagright.com", ATSSource.ASHBY)] = [_make_job("Flagright")]
    llm_client = FakeLLMClient(batches=[["flagright.com"]])

    mechanical_count = len(generate_mechanical_candidates("Flagright"))
    result = scout("Flagright", llm_client=llm_client)

    # every mechanical candidate was tried (3 sources each) before the LLM's
    # single suggestion appears
    mechanical_attempts = [a for a in result.attempts if a.origin == "mechanical"]
    assert len(mechanical_attempts) == mechanical_count * len(scout_module.SOURCE_ORDER)


def test_scout_gives_up_after_one_llm_round_by_default(patched_adapters):
    """MAX_LLM_ROUNDS=1: a second round is never attempted even if the first
    round's suggestions all fail."""
    llm_client = FakeLLMClient(batches=[["nope-one", "nope-two"]])

    result = scout("Nonexistent Co", llm_client=llm_client)

    assert result.success is False
    assert llm_client.calls == 1


def test_scout_treats_llm_error_as_no_more_candidates(patched_adapters):
    result = scout("Nonexistent Co", llm_client=RaisingLLMClient())

    assert result.success is False
    assert "does not appear to be on" in result.conclusion


def test_scout_without_llm_client_only_tries_mechanical(patched_adapters):
    result = scout("Nonexistent Co", llm_client=None)

    assert result.success is False
    assert all(a.origin == "mechanical" for a in result.attempts)


# ---------------------------------------------------------------------------
# scout() - empty board
# ---------------------------------------------------------------------------


def test_scout_does_not_treat_empty_board_as_success(patched_adapters):
    working, _ = patched_adapters
    no_space = generate_mechanical_candidates("Databricks")[0]
    working[(no_space, ATSSource.ASHBY)] = []  # board exists, zero current jobs

    result = scout("Databricks", llm_client=None)

    assert result.success is False
    assert result.config is None
    empty_attempt = next(a for a in result.attempts if a.token == no_space)
    assert empty_attempt.outcome == "empty_board"
    assert "EMPTY" in result.conclusion


def test_scout_keeps_searching_after_empty_board_and_can_still_succeed(patched_adapters):
    working, _ = patched_adapters
    candidates = generate_mechanical_candidates("Databricks")
    working[(candidates[0], ATSSource.ASHBY)] = []  # empty board, not a win
    working[(candidates[1], ATSSource.LEVER)] = [_make_job("Databricks")]  # real hit, later candidate

    result = scout("Databricks", llm_client=None)

    assert result.success is True
    assert result.config.token == candidates[1]


# ---------------------------------------------------------------------------
# scout() - error outcome vs not_found
# ---------------------------------------------------------------------------


def test_scout_records_non_404_failure_as_error_not_not_found(patched_adapters):
    working, error_tokens = patched_adapters
    no_space = generate_mechanical_candidates("Databricks")[0]
    error_tokens[(no_space, ATSSource.ASHBY)] = ATSAdapterError("500 from upstream")
    working[(no_space, ATSSource.LEVER)] = [_make_job("Databricks")]

    result = scout("Databricks", llm_client=None)

    error_attempt = next(a for a in result.attempts if a.token == no_space and a.source == ATSSource.ASHBY)
    assert error_attempt.outcome == "error"
    assert error_attempt.error is not None
    # search still continues past an error to the next source
    assert result.success is True


# ---------------------------------------------------------------------------
# scout() - attempt cap
# ---------------------------------------------------------------------------


def test_scout_respects_max_attempts_cap(patched_adapters):
    """A pathologically generous fake LLM that always has new candidates must
    still be bounded by max_attempts, not looped forever."""

    class InfiniteLLMClient(LLMClient):
        def __init__(self):
            self._n = 0

        @property
        def model_name(self) -> str:
            return "fake-model"

        def complete(self, system_instruction: str, user_message: str, response_schema: dict) -> str:
            self._n += 1
            return json.dumps({"candidates": [f"guess-{self._n}-{i}" for i in range(5)]})

    result = scout("Nonexistent Co", llm_client=InfiniteLLMClient(), max_attempts=12, max_llm_rounds=5)

    assert len(result.attempts) <= 12
    assert result.success is False


# ---------------------------------------------------------------------------
# batch mode
# ---------------------------------------------------------------------------


def test_load_batch_company_names_strips_blank_lines_and_whitespace(tmp_path):
    path = tmp_path / "companies.txt"
    path.write_text("Harness\n\n  Reo Dev  \n\nFlagright\n", encoding="utf-8")

    assert _load_batch_company_names(path) == ["Harness", "Reo Dev", "Flagright"]


def test_is_already_configured_case_insensitive():
    existing = [CompanyConfig(name="Harness", ats=ATSSource.GREENHOUSE, token="harnessinc")]

    assert _is_already_configured("harness", existing) is True
    assert _is_already_configured("  HARNESS  ", existing) is True
    assert _is_already_configured("Reo Dev", existing) is False


def test_scout_batch_skips_already_configured_companies(patched_adapters):
    working, _ = patched_adapters
    existing = [CompanyConfig(name="Harness", ats=ATSSource.GREENHOUSE, token="harnessinc")]

    batch = scout_batch(["Harness", "Nonexistent Co"], existing, llm_client=None, pacing_seconds=0)

    assert batch.skipped == ["Harness"]
    assert [r.company_name for r in batch.not_supported] == ["Nonexistent Co"]


def test_scout_batch_groups_results_by_outcome(patched_adapters, monkeypatch):
    working, _ = patched_adapters
    working[("acmeinc", ATSSource.GREENHOUSE)] = [_make_job("Acme")]
    no_space = generate_mechanical_candidates("Empty Co")[0]
    working[(no_space, ATSSource.ASHBY)] = []  # empty board, not a win

    batch = scout_batch(["Acme", "Empty Co", "Nowhere Co"], [], llm_client=None, pacing_seconds=0)

    assert [r.company_name for r in batch.found] == ["Acme"]
    assert batch.found[0].config.token == "acmeinc"
    assert [r.company_name for r in batch.empty_board] == ["Empty Co"]
    assert [r.company_name for r in batch.not_supported] == ["Nowhere Co"]
    assert batch.skipped == []


def test_scout_batch_reports_total_requests_across_all_companies(patched_adapters):
    working, _ = patched_adapters
    working[("acmeinc", ATSSource.GREENHOUSE)] = [_make_job("Acme")]

    batch = scout_batch(["Acme", "Nowhere Co"], [], llm_client=None, pacing_seconds=0)

    expected = sum(len(r.attempts) for r in batch.found + batch.empty_board + batch.not_supported)
    assert batch.total_requests == expected
    assert batch.total_requests > 0


def test_scout_batch_paces_between_companies_not_before_the_first(patched_adapters, monkeypatch):
    working, _ = patched_adapters
    sleeps = []
    monkeypatch.setattr(scout_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    scout_batch(["Nowhere Co", "Nowhere Two", "Nowhere Three"], [], llm_client=None, pacing_seconds=2.5)

    assert sleeps == [2.5, 2.5]  # 2 gaps between 3 companies, none before the first


def test_scout_batch_does_not_pace_for_skipped_companies(patched_adapters, monkeypatch):
    """A company already in companies.yaml never calls scout() at all, so it
    must not consume a pacing slot either - pacing is about requests
    actually made, not about list position."""
    working, _ = patched_adapters
    existing = [CompanyConfig(name="Already Configured", ats=ATSSource.GREENHOUSE, token="x")]
    sleeps = []
    monkeypatch.setattr(scout_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    scout_batch(["Already Configured", "Nowhere Co", "Nowhere Two"], existing, llm_client=None, pacing_seconds=2.5)

    assert sleeps == [2.5]  # only one real gap - between the two actually-scouted companies


# ---------------------------------------------------------------------------
# _found_as_yaml
# ---------------------------------------------------------------------------


def test_found_as_yaml_matches_companies_yaml_shape(patched_adapters):
    working, _ = patched_adapters
    working[("acmeinc", ATSSource.GREENHOUSE)] = [_make_job("Acme")]

    result = scout("Acme", llm_client=None)
    yaml_text = _found_as_yaml([result])

    assert "name: Acme" in yaml_text
    assert "ats: greenhouse" in yaml_text
    assert "token: acmeinc" in yaml_text
    assert "notes:" in yaml_text  # scout always sets a notes field


def test_found_as_yaml_empty_list_produces_empty_yaml_list():
    assert _found_as_yaml([]).strip() == "[]"
