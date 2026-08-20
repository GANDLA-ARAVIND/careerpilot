import numpy as np
import pytest
from sqlalchemy.orm import Session

import ranking
from db import get_engine
from rag import retrieve


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    with Session(engine) as session:
        yield session


def _fake_embed_factory(vector_map, default=(0.0, 0.0)):
    """Same pattern as tests/test_ranking.py's fake - real _embed() is a
    slow, network-dependent model call. Vectors here are pre-normalized
    2D unit vectors so a plain dot product gives an exact, predictable
    cosine similarity."""

    def fake_embed(texts):
        return np.array([vector_map.get(text, default) for text in texts], dtype=np.float32)

    return fake_embed


def test_retrieve_orders_by_similarity_to_query_not_insertion_order(session, monkeypatch):
    vector_map = {
        "the question": (1.0, 0.0),
        "close match": (0.9, 0.436),  # ~25 degrees off - high similarity
        "far match": (0.0, 1.0),  # orthogonal - zero similarity
        "opposite match": (-1.0, 0.0),  # anti-aligned - negative similarity
    }
    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory(vector_map))

    result = retrieve(
        "the question", ["far match", "opposite match", "close match"], session, k=10
    )

    ordered_texts = [text for text, _score in result.chunks]
    assert ordered_texts == ["close match", "far match", "opposite match"]
    assert result.chunks[0][1] > result.chunks[1][1] > result.chunks[2][1]


def test_retrieve_truncates_to_k(session, monkeypatch):
    vector_map = {"q": (1.0, 0.0), "a": (1.0, 0.0), "b": (1.0, 0.0), "c": (1.0, 0.0)}
    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory(vector_map))

    result = retrieve("q", ["a", "b", "c"], session, k=2)

    assert len(result.chunks) == 2
    assert result.pool_size == 3
    assert result.retrieved_count == 2
    assert result.is_noop is False


def test_retrieve_is_noop_when_pool_smaller_than_k(session, monkeypatch):
    """The exact scenario this project is in today at n=52: k larger than
    the whole candidate pool means every candidate comes back, and that has
    to be visible as a no-op, not silently indistinguishable from real
    filtering."""
    vector_map = {"q": (1.0, 0.0), "a": (1.0, 0.0), "b": (0.0, 1.0)}
    monkeypatch.setattr(ranking, "_embed", _fake_embed_factory(vector_map))

    result = retrieve("q", ["a", "b"], session, k=30)

    assert result.pool_size == 2
    assert result.retrieved_count == 2
    assert result.is_noop is True


def test_retrieve_empty_pool_returns_empty_result(session):
    result = retrieve("q", [], session, k=30)

    assert result.chunks == []
    assert result.pool_size == 0
    assert result.retrieved_count == 0
