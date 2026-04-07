"""Black-box regression coverage for planner split and dependency execution."""

from __future__ import annotations

import pytest

from core.models import TaskStatus


pytestmark = pytest.mark.regression


def test_real_planner_split_creates_and_completes_dependent_subtasks(
    regression_harness_factory,
):
    stable_task_model_overrides = {
        "planner_model": "github-copilot/gpt-5.4",
        "coder_model_default": "github-copilot/gpt-5.4",
        "coder_model_by_complexity": {
            "simple": "github-copilot/gpt-5.4",
            "medium": "github-copilot/gpt-5.4",
            "complex": "github-copilot/gpt-5.4",
            "very_complex": "github-copilot/gpt-5.4",
        },
        "reviewer_models": ["github-copilot/gpt-5.4"],
        "timeout": 600,
    }
    harness = regression_harness_factory(
        "split_python_app",
        config_overrides={"opencode": stable_task_model_overrides},
    )

    create_result = harness.submit_develop_task(
        title="Split regression parent task",
        description=(
            "You must split this task into exactly 2 sub-tasks and execute them. Return strict "
            "JSON only. Sub-task 0: modify app/arithmetic.py to add multiply(a, b) and add test "
            "coverage in tests/test_arithmetic.py. Sub-task 1: modify app/formatting.py to add "
            "render_product(a, b) that imports and calls multiply, and add test coverage in "
            "tests/test_formatting.py. Sub-task 1 must depend on sub-task 0, because it uses the "
            "new multiply helper. Do not keep this as a single task. The final answer from the "
            "planner must be split=true with exactly two sub_tasks and depends_on=[0] on the "
            "second sub-task."
        ),
        file_path="app/arithmetic.py",
        line_number=1,
    )
    parent_id = create_result["id"]

    parent = harness.wait_for_task_terminal(parent_id)
    assert parent.status == TaskStatus.COMPLETED, harness.describe_task(parent_id)

    tasks = harness.list_tasks()
    child_tasks = [task for task in tasks if task.get("parent_id") == parent_id]
    assert len(child_tasks) >= 2, tasks

    child_ids = {task["id"] for task in child_tasks}
    assert any(task.get("depends_on") for task in child_tasks), child_tasks
    for task in child_tasks:
        assert task["status"] == TaskStatus.COMPLETED.value, task
        for dep_id in task.get("depends_on") or []:
            assert dep_id in child_ids, child_tasks

    parent_detail = harness.get_task_detail(parent_id)
    assert "Split into" in parent_detail["task"]["plan_output"], parent_detail

    child_details = [harness.get_task_detail(task["id"]) for task in child_tasks]
    child_run_types = [
        {run["agent_type"] for run in detail["runs"]} for detail in child_details
    ]
    assert all(
        {"planner", "coder", "reviewer"}.issubset(run_types)
        for run_types in child_run_types
    ), child_details

    blocked_child = next(task for task in child_tasks if task.get("depends_on"))
    blocked_detail = harness.get_task_detail(blocked_child["id"])
    assert blocked_detail["task"]["branch_name"], blocked_detail
    assert blocked_detail["task"]["worktree_path"], blocked_detail
    assert any(
        "multiply" in detail["task"].get("description", "").lower()
        or "render_product" in detail["task"].get("description", "").lower()
        for detail in child_details
    ), child_details
