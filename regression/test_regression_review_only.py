"""Black-box regression coverage for review-only tasks."""

from __future__ import annotations

import pytest

from core.models import TaskStatus


pytestmark = pytest.mark.regression


def test_real_review_only_task_runs_reviewer_pipeline_via_public_api(
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
        "tiny_python_app",
        config_overrides={"opencode": stable_task_model_overrides},
    )

    review_result = harness.submit_review_task(
        title="Review a tiny inline patch",
        review_input=(
            "Please review this small inline diff only.\n\n"
            "```diff\n"
            "diff --git a/app/calculator.py b/app/calculator.py\n"
            "--- a/app/calculator.py\n"
            "+++ b/app/calculator.py\n"
            "@@\n"
            "+def multiply(a: int, b: int) -> int:\n"
            "+    return a * b\n"
            "```\n"
        ),
    )
    task_id = review_result["id"]

    completed = harness.wait_for_task_terminal(task_id, timeout_sec=1800)
    assert completed.status == TaskStatus.COMPLETED, harness.describe_task(task_id)
    assert completed.task_mode == "review", harness.describe_task(task_id)
    assert completed.worktree_path == "", harness.describe_task(task_id)
    assert completed.branch_name == "", harness.describe_task(task_id)

    detail = harness.get_task_detail(task_id)
    run_types = {run["agent_type"] for run in detail["runs"]}
    assert "reviewer" in run_types, detail
    assert "planner" not in run_types, detail
    assert "coder" not in run_types, detail
    assert detail["task"]["review_input"], detail
    assert detail["task"]["review_output"], detail
    assert isinstance(detail["task"]["review_pass"], bool), detail
