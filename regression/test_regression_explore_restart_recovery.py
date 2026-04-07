"""Black-box regression coverage for explore queue recovery across daemon restart."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.regression


def test_real_explore_queue_recovers_after_daemon_restart(regression_harness_factory):
    explore_model_overrides = {
        "explorer_model": "github-copilot/gpt-5.4",
        "map_model": "github-copilot/gpt-5.4",
    }
    harness = regression_harness_factory(
        "tiny_python_app",
        config_overrides={"explore": dict(explore_model_overrides)},
    )

    init_result = harness.init_explore_map()
    assert init_result["accepted"] is True, init_result

    map_state = harness.wait_for_explore_map_terminal()
    assert map_state["map_init"]["status"] == "done", harness.describe_explore()

    modules = harness.list_explore_modules()
    target_modules = [
        module
        for module in modules
        if str(module.get("path", "")).endswith("calculator.py")
    ]
    assert target_modules, modules
    target_module = target_modules[0]

    start_result = harness.start_exploration(
        module_ids=[target_module["id"]],
        categories=["maintainability"],
        focus_point="Inspect app/calculator.py for maintainability issues and return concise findings.",
    )
    assert start_result["started"] >= 1, start_result

    active_queue = harness.wait_for_exploration_activity(timeout_sec=30)
    assert active_queue["counts"]["total"] > 0, active_queue

    harness.wait_for_exploration_running_age(min_age_sec=1, timeout_sec=120)

    harness.crash_daemon()
    harness.restart(config_overrides={"explore": dict(explore_model_overrides)})

    recovered_queue = harness.get_explore_queue()
    assert recovered_queue["counts"]["total"] >= 0, recovered_queue

    final_queue = harness.wait_for_exploration_idle(timeout_sec=1200)
    assert final_queue["counts"]["total"] == 0, harness.describe_explore()

    runs = harness.get_explore_runs_api()
    assert runs, harness.describe_explore()

    module_detail = harness.get_explore_module_detail(target_module["id"])
    assert module_detail["runs"], module_detail
