"""Black-box regression coverage for creating tasks from exploration findings."""

from __future__ import annotations

import pytest

from core.models import TaskStatus


pytestmark = pytest.mark.regression


def test_real_explore_run_can_create_task_from_finding_via_public_api(
    regression_harness_factory,
):
    harness = regression_harness_factory(
        "explore_python_app",
        config_overrides={"explore": {"auto_task_severity": "major"}},
    )

    init_result = harness.init_explore_map()
    assert init_result["accepted"] is True

    map_state = harness.wait_for_explore_map_terminal()
    assert map_state["map_init"]["status"] == "done", harness.describe_explore()

    modules = harness.list_explore_modules()
    target_module_ids = [
        module["id"] for module in modules if module.get("path") == "app/reporting.py"
    ]
    assert target_module_ids, modules

    start_result = harness.start_exploration(
        module_ids=target_module_ids,
        categories=["maintainability"],
        focus_point="Look for duplicated branching and refactorable conditional structure.",
    )
    assert start_result["started"] >= 1, start_result

    queue_state = harness.wait_for_exploration_idle()
    assert queue_state["counts"]["total"] == 0, harness.describe_explore()

    runs = harness.get_explore_runs_api()
    runs_with_findings = [run for run in runs if run.get("findings")]
    assert runs_with_findings, harness.describe_explore()

    selected_run = runs_with_findings[0]
    run_detail = harness.get_explore_run_detail(selected_run["id"])
    assert run_detail["findings"], run_detail

    created_task = harness.create_task_from_finding(selected_run["id"], finding_index=0)
    task_id = created_task["id"]

    task_detail = harness.get_task_detail(task_id)
    task_payload = task_detail["task"]
    assert task_payload["source"] == "explore", task_detail
    assert task_payload["title"].startswith("[Explore/maintainability]"), task_detail
    assert "**Found by exploration**" in task_payload["description"], task_detail
    assert task_payload["file_path"] == "app/reporting.py", task_detail
    assert task_payload["status"] in {
        TaskStatus.PENDING.value,
        *(status.value for status in TaskStatus.active_statuses()),
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.NEEDS_ARBITRATION.value,
    }, task_detail
