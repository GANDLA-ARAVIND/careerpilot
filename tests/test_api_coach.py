"""Coach endpoints.

POST /api/coach/ask spends an LLM call in production, so market_gap is
monkeypatched here. GET /api/coach/skill-gaps is pure SQL over cached
analyst rows and runs for real.
"""

from config import GEMINI_MODEL_STAGE1
from tests.conftest import add_analyst_result


# ---------------------------------------------------------------------------
# GET /api/coach/skill-gaps
# ---------------------------------------------------------------------------


def test_skill_gaps_empty_when_nothing_scored(client):
    body = client.get("/api/coach/skill-gaps").json()

    assert body["job_count"] == 0
    assert body["skills"] == []


def test_skill_gaps_aggregates_missing_skills_below_threshold(client, seeded, temp_env):
    add_analyst_result(
        temp_env["engine"],
        seeded["survivor_a"],
        model=GEMINI_MODEL_STAGE1,
        fit_score=20,
        missing=["Kubernetes", "Go"],
    )
    add_analyst_result(
        temp_env["engine"],
        seeded["survivor_b"],
        model=GEMINI_MODEL_STAGE1,
        fit_score=30,
        missing=["Kubernetes"],
    )

    body = client.get("/api/coach/skill-gaps", params={"threshold": 40}).json()

    assert body["job_count"] == 2
    counts = {s["skill"]: s["count"] for s in body["skills"]}
    assert counts["Kubernetes"] == 2
    assert counts["Go"] == 1


def test_skill_gaps_excludes_jobs_above_threshold(client, seeded, temp_env):
    add_analyst_result(
        temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1, fit_score=90, missing=["Rust"]
    )

    body = client.get("/api/coach/skill-gaps", params={"threshold": 40}).json()

    assert body["job_count"] == 0


def test_skill_gaps_respects_limit(client, seeded, temp_env):
    add_analyst_result(
        temp_env["engine"],
        seeded["survivor_a"],
        model=GEMINI_MODEL_STAGE1,
        fit_score=10,
        missing=["A", "B", "C", "D"],
    )

    body = client.get("/api/coach/skill-gaps", params={"threshold": 40, "limit": 2}).json()

    assert len(body["skills"]) == 2


def test_skill_gaps_rejects_an_out_of_range_threshold(client):
    assert client.get("/api/coach/skill-gaps", params={"threshold": 500}).status_code == 422


# ---------------------------------------------------------------------------
# POST /api/coach/ask
# ---------------------------------------------------------------------------


class _FakeRetrieval:
    def __init__(self, chunks, pool_size, retrieved_count):
        self.chunks = chunks
        self.pool_size = pool_size
        self.retrieved_count = retrieved_count

    @property
    def is_noop(self):
        return self.retrieved_count >= self.pool_size


class _FakeReport:
    def __init__(self, question, retrieval, answer):
        self.question = question
        self.retrieval = retrieval
        self.answer = answer


def test_ask_returns_the_answer_and_its_evidence(client, seeded, monkeypatch):
    def fake_market_gap(question, session, llm_client, k=12):
        return _FakeReport(
            question=question,
            retrieval=_FakeRetrieval([("Requirements: Go, Kubernetes.", 0.81)], pool_size=50, retrieved_count=1),
            answer="Kubernetes appears most often.",
        )

    monkeypatch.setattr("agents.coach.market_gap", fake_market_gap)
    monkeypatch.setattr("agents.coach.load_market_population", lambda session: [object()] * 50)
    monkeypatch.setattr("llm.GeminiClient", lambda **kwargs: object())

    body = client.post("/api/coach/ask", json={"question": "What skills am I missing?"}).json()

    assert body["answer"] == "Kubernetes appears most often."
    assert body["retrieved"][0]["text"] == "Requirements: Go, Kubernetes."
    assert body["retrieved"][0]["score"] == 0.81
    assert body["population_size"] == 50


def test_ask_surfaces_when_retrieval_was_a_noop(client, seeded, monkeypatch):
    """If k >= the pool, "retrieval" returned everything and ranked
    nothing. That must be visible rather than presented as retrieval."""

    def fake_market_gap(question, session, llm_client, k=12):
        return _FakeReport(
            question=question,
            retrieval=_FakeRetrieval([("chunk", 0.5)], pool_size=1, retrieved_count=1),
            answer="Answer.",
        )

    monkeypatch.setattr("agents.coach.market_gap", fake_market_gap)
    monkeypatch.setattr("agents.coach.load_market_population", lambda session: [object()])
    monkeypatch.setattr("llm.GeminiClient", lambda **kwargs: object())

    body = client.post("/api/coach/ask", json={"question": "q"}).json()

    assert body["retrieval_was_noop"] is True


def test_ask_rejects_an_empty_question(client):
    assert client.post("/api/coach/ask", json={"question": "   "}).status_code == 422


def test_ask_409s_when_there_is_nothing_to_retrieve_from(client, monkeypatch):
    monkeypatch.setattr("agents.coach.load_market_population", lambda session: [])
    monkeypatch.setattr("llm.GeminiClient", lambda **kwargs: object())

    response = client.post("/api/coach/ask", json={"question": "q"})

    assert response.status_code == 409
