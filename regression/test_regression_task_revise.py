"""Black-box regression coverage for revise-task recovery flow."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.models import TaskStatus
from regression.helpers import (
    assert_clean_worktree,
    assert_file_contains,
    assert_python_tests_pass,
)


pytestmark = pytest.mark.regression


def test_real_revise_flow_recovers_existing_task_via_public_api(
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

    create_result = harness.submit_develop_task(
        title="Add multiply helper for revise regression",
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
    task_id = create_result["id"]

    completed = harness.wait_for_task_terminal(task_id)
    assert completed.status == TaskStatus.COMPLETED, harness.describe_task(task_id)
    original_coder_sessions = list(completed.session_ids.get("coder", []))
    assert original_coder_sessions, harness.describe_task(task_id)

    revise_result = harness.revise_task(
        task_id,
        feedback=(
            "Keep the existing multiply implementation and tests. Add a short module-level "
            "docstring line mentioning multiplication support, and add one more pytest case "
            "that covers multiplying by zero. Re-run python -m pytest -q and stop."
        ),
    )
    assert revise_result["ok"] is True, revise_result

    revised = harness.wait_for_task_terminal(task_id)
    assert revised.status == TaskStatus.COMPLETED, harness.describe_task(task_id)

    detail = harness.get_task_detail(task_id)
    run_types = [run["agent_type"] for run in detail["runs"]]
    assert "manual_review" in run_types, detail
    assert run_types.count("coder") >= 2, detail
    assert run_types.count("reviewer") >= 2, detail

    latest_coder_sessions = revised.session_ids.get("coder", [])
    assert len(latest_coder_sessions) >= len(original_coder_sessions) + 1, detail
    assert latest_coder_sessions[-1] == original_coder_sessions[-1], detail

    worktree_path = Path(revised.worktree_path)
    assert_file_contains(worktree_path / "app" / "calculator.py", "multiply")
    test_content = (worktree_path / "tests" / "test_calculator.py").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r"def test_multiply_by_zero\(\):\s+assert multiply\(\s*(?:0\s*,\s*\d+|\d+\s*,\s*0)\s*\)\s*==\s*0",
        test_content,
    ), test_content
    assert_clean_worktree(worktree_path)
    assert_python_tests_pass(worktree_path)
