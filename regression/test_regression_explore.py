"""Black-box real regression coverage for project exploration."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.regression


def test_real_explore_pipeline_completes_end_to_end(regression_harness_factory):
    harness = regression_harness_factory("tiny_python_app")

    init_result = harness.init_explore_map()
    assert init_result["accepted"] is True

    map_state = harness.wait_for_explore_map_terminal()
    assert map_state["map_init"]["status"] == "done", map_state
    assert map_state["map_ready"] is True, map_state

    modules = harness.list_explore_modules()
    assert modules, map_state

    start_result = harness.start_exploration(categories=["maintainability"])
    assert start_result["started"] >= 1, start_result

    queue_state = harness.wait_for_exploration_idle()
    assert queue_state["counts"]["total"] == 0, queue_state

    runs = harness.get_explore_runs_api()
    assert runs, "Expected at least one persisted explore run"

    module_ids_with_runs = {run["module_id"] for run in runs if run.get("module_id")}
    assert module_ids_with_runs, runs

    module_detail = harness.get_explore_module_detail(next(iter(module_ids_with_runs)))
    assert module_detail["runs"], module_detail
    assert module_detail["module"]["category_status"]["maintainability"] in {
        "done",
        "stale",
    }, module_detail
    assert any(str(run.get("summary", "")).strip() for run in module_detail["runs"]), (
        module_detail
    )
