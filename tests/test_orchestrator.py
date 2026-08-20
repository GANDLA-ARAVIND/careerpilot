from datetime import date, timedelta

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

import orchestrator
from orchestrator import (
    PipelineState,
    _find_resumable_thread,
    _thread_id_for,
    build_graph,
    route_after_fetch,
    route_after_stage1,
)


# ---------------------------------------------------------------------------
# Routing logic - the two conditional branches, tested directly (no graph,
# no DB, no LLM: these are plain functions over plain state)
# ---------------------------------------------------------------------------


def test_route_after_fetch_skips_when_nothing_new_or_edited():
    state: PipelineState = {
        "persist_outcomes": {"new": 0, "unchanged": 52, "edited": 0},
        "kept_hashes": ["h1", "h2"],
    }
    assert route_after_fetch(state) == orchestrator.END


def test_route_after_fetch_skips_when_new_jobs_all_rejected_by_filters():
    """New jobs arrived, but none survived rule filtering - still nothing
    for the Analyst to look at."""
    state: PipelineState = {
        "persist_outcomes": {"new": 5, "unchanged": 40, "edited": 0},
        "kept_hashes": [],
    }
    assert route_after_fetch(state) == orchestrator.END


def test_route_after_fetch_proceeds_when_new_jobs_survive_filters():
    state: PipelineState = {
        "persist_outcomes": {"new": 3, "unchanged": 49, "edited": 0},
        "kept_hashes": ["h1"],
    }
    assert route_after_fetch(state) == "stage1_analyze"


def test_route_after_fetch_proceeds_on_edited_jobs_even_with_zero_new():
    """An edited posting can change what the Analyst would say about it
    (different requirements text -> different cache key) even though it
    isn't counted as "new" - edited must count toward "something changed"."""
    state: PipelineState = {
        "persist_outcomes": {"new": 0, "unchanged": 51, "edited": 1},
        "kept_hashes": ["h1"],
    }
    assert route_after_fetch(state) == "stage1_analyze"


def test_route_after_stage1_skips_when_no_results():
    state: PipelineState = {"stage1_ranked_hashes": []}
    assert route_after_stage1(state) == orchestrator.END


def test_route_after_stage1_proceeds_when_results_exist():
    state: PipelineState = {"stage1_ranked_hashes": ["h1", "h2"]}
    assert route_after_stage1(state) == "stage2_analyze"


# ---------------------------------------------------------------------------
# Graph structure
# ---------------------------------------------------------------------------


def test_graph_compiles_and_has_expected_nodes():
    compiled = build_graph().compile()
    node_names = set(compiled.get_graph().nodes)
    assert {"fetch_persist_filter", "stage1_analyze", "stage2_analyze"} <= node_names


def test_graph_declares_both_conditional_destinations():
    compiled = build_graph().compile()
    edges = {(e.source, e.target) for e in compiled.get_graph().edges}
    assert ("fetch_persist_filter", "stage1_analyze") in edges
    assert ("fetch_persist_filter", "__end__") in edges
    assert ("stage1_analyze", "stage2_analyze") in edges
    assert ("stage1_analyze", "__end__") in edges
    assert ("stage2_analyze", "__end__") in edges


# ---------------------------------------------------------------------------
# Thread selection / lookback resume
# ---------------------------------------------------------------------------


def _make_app_with_saver(tmp_path):
    """A minimal 2-node graph standing in for the real one, sharing
    orchestrator's PipelineState shape and a real on-disk SqliteSaver -
    fast enough for tests, but exercising the real checkpoint round trip
    rather than mocking it."""
    from langgraph.graph import END, StateGraph

    def a(state):
        return {"kept_hashes": ["h1"]}

    def b(state):
        raise RuntimeError("simulated crash")

    graph = StateGraph(PipelineState)
    graph.add_node("a", a)
    graph.add_node("b", b)
    graph.set_entry_point("a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)

    db_path = tmp_path / "test_checkpoints.db"
    saver_cm = SqliteSaver.from_conn_string(str(db_path))
    saver = saver_cm.__enter__()
    app = graph.compile(checkpointer=saver)
    return app, saver_cm


def test_find_resumable_thread_returns_none_when_nothing_incomplete(tmp_path):
    app, saver_cm = _make_app_with_saver(tmp_path)
    try:
        today = date(2026, 8, 5)
        assert _find_resumable_thread(app, today) is None
    finally:
        saver_cm.__exit__(None, None, None)


def test_find_resumable_thread_finds_a_crashed_thread_from_yesterday(tmp_path):
    app, saver_cm = _make_app_with_saver(tmp_path)
    try:
        today = date(2026, 8, 5)
        yesterday = today - timedelta(days=1)
        yesterday_thread = _thread_id_for(yesterday)

        try:
            app.invoke({}, config={"configurable": {"thread_id": yesterday_thread}})
        except RuntimeError:
            pass  # expected - node b always raises

        found = _find_resumable_thread(app, today)
        assert found == yesterday_thread
    finally:
        saver_cm.__exit__(None, None, None)


def test_find_resumable_thread_ignores_a_thread_that_completed_normally(tmp_path):
    """A thread that reached END - whether via the full path or an early
    conditional skip - must never be mistaken for "still needs to run"."""
    from langgraph.graph import END, StateGraph

    def a(state):
        return {"kept_hashes": ["h1"]}

    graph = StateGraph(PipelineState)
    graph.add_node("a", a)
    graph.set_entry_point("a")
    graph.add_edge("a", END)

    db_path = tmp_path / "test_checkpoints_2.db"
    with SqliteSaver.from_conn_string(str(db_path)) as saver:
        app = graph.compile(checkpointer=saver)
        today = date(2026, 8, 5)
        yesterday_thread = _thread_id_for(today - timedelta(days=1))
        app.invoke({}, config={"configurable": {"thread_id": yesterday_thread}})

        assert _find_resumable_thread(app, today) is None


def test_find_resumable_thread_respects_lookback_window(tmp_path):
    app, saver_cm = _make_app_with_saver(tmp_path)
    try:
        today = date(2026, 8, 5)
        too_old = today - timedelta(days=10)
        too_old_thread = _thread_id_for(too_old)

        try:
            app.invoke({}, config={"configurable": {"thread_id": too_old_thread}})
        except RuntimeError:
            pass

        # default LOOKBACK_DAYS is 7 - a 10-day-old crash is out of range
        assert _find_resumable_thread(app, today, lookback_days=7) is None
        # but a wider window does find it
        assert _find_resumable_thread(app, today, lookback_days=15) == too_old_thread
    finally:
        saver_cm.__exit__(None, None, None)
