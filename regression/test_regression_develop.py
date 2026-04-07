"""Real regression coverage for the develop-task execution pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import TaskStatus
from regression.helpers import (
    assert_agent_run_types,
    assert_branch_contains_submission,
    assert_clean_worktree,
    assert_file_contains,
    assert_python_tests_pass,
)


pytestmark = pytest.mark.regression


def test_real_develop_task_pipeline_completes_end_to_end(regression_harness_factory):
    harness = regression_harness_factory("tiny_python_app")
    task_response = harness.submit_develop_task(
        title="Add multiply helper to the calculator",
        description=(
            "Add a new multiply(a, b) helper to app/calculator.py, export it from the "
            "same module as the existing arithmetic helpers, and add pytest coverage in "
            "tests/test_calculator.py. Keep the code style and API shape consistent with "
            "the existing add and subtract helpers. After the task changes are committed "
            "and python -m pytest -q passes, stop immediately without extra repository "
            "exploration."
        ),
        file_path="app/calculator.py",
        line_number=1,
    )
    task_id = task_response["id"]

    completed = harness.wait_for_task_terminal(task_id)
    if completed.status != TaskStatus.COMPLETED:
        pytest.fail(harness.describe_task(task_id))

    detail = harness.get_task_detail(task_id)
    runs = harness.get_task_runs(task_id)
    assert_agent_run_types(runs, ["planner", "coder", "reviewer"])
    assert len(detail["runs"]) >= 3, harness.describe_task(task_id)

    planner_sessions = completed.session_ids.get("planner", [])
    coder_sessions = completed.session_ids.get("coder", [])
    reviewer_sessions = completed.session_ids.get("reviewer", [])
    assert planner_sessions, harness.describe_task(task_id)
    assert coder_sessions, harness.describe_task(task_id)
    assert reviewer_sessions, harness.describe_task(task_id)
    assert completed.branch_name.startswith("agent/task-"), harness.describe_task(
        task_id
    )
    assert completed.worktree_path, harness.describe_task(task_id)

    worktree_path = Path(completed.worktree_path)
    assert_file_contains(worktree_path / "app" / "calculator.py", "def multiply(")
    assert_file_contains(
        worktree_path / "tests" / "test_calculator.py", "test_multiply"
    )
    assert_branch_contains_submission(worktree_path)
    assert_clean_worktree(worktree_path)
    assert_python_tests_pass(worktree_path)
