"""Black-box regression coverage for exploration auto-task creation."""

from __future__ import annotations

import pytest

from core.models import TaskStatus


pytestmark = pytest.mark.regression


def test_real_explore_run_auto_creates_task_for_major_finding(
    regression_harness_factory,
):
    explore_model_overrides = {
        "explorer_model": "github-copilot/gpt-5.4",
        "map_model": "github-copilot/gpt-5.4",
    }
    harness = regression_harness_factory(
        "explore_python_app",
        config_overrides={
            "explore": {
                "auto_task_severity": "major",
                **explore_model_overrides,
            }
        },
    )

    target_module = harness.add_explore_module(
        name="Status Reporting",
        path="app/reporting.py",
        description="Reporting helper with intentionally duplicated branching.",
    )

    preexisting_task_ids = {task["id"] for task in harness.list_tasks()}

    start_result = harness.start_exploration(
        module_ids=[target_module["id"]],
        categories=["maintainability"],
        focus_point="Look for duplicated branching severe enough to justify task creation.",
    )
    assert start_result["started"] >= 1, start_result

    queue_state = harness.wait_for_exploration_idle()
    assert queue_state["counts"]["total"] == 0, harness.describe_explore()

    runs = harness.get_explore_runs_api()
    matching_runs = [
        run
        for run in runs
        if run.get("module_id") == target_module["id"] and run.get("findings")
    ]
    assert matching_runs, harness.describe_explore()

    tasks = harness.list_tasks()
    new_tasks = [task for task in tasks if task["id"] not in preexisting_task_ids]
    explore_tasks = [task for task in new_tasks if task.get("source") == "explore"]
    assert explore_tasks, {"tasks": tasks, "runs": matching_runs}

    explore_task_details = [
        harness.get_task_detail(task["id"]) for task in explore_tasks
    ]
    auto_task_detail = next(
        (
            detail
            for detail in explore_task_details
            if detail["task"].get("file_path") == "app/reporting.py"
        ),
        None,
    )
    assert auto_task_detail is not None, explore_task_details
    auto_task = auto_task_detail["task"]
    assert auto_task["title"].startswith("[Explore/maintainability]"), auto_task
    assert "**Found by exploration**" in auto_task["description"], auto_task
    assert auto_task["status"] in {
        TaskStatus.PENDING.value,
        *(status.value for status in TaskStatus.active_statuses()),
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.NEEDS_ARBITRATION.value,
    }, auto_task
