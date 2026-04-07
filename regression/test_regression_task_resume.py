"""Black-box regression coverage for FAILED -> resume recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import TaskStatus
from regression.helpers import (
    assert_clean_worktree,
    assert_file_contains,
    assert_python_tests_pass,
)


pytestmark = pytest.mark.regression


def test_real_resume_flow_recovers_failed_task_via_public_api(
    regression_harness_factory,
):
    stable_model_overrides = {
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
        config_overrides={"opencode": dict(stable_model_overrides)},
    )

    create_result = harness.submit_develop_task(
        title="Add multiply helper before forced resume regression",
        description=(
            "This is a small single-file task. Do not split it. Add a new multiply(a, b) "
            "helper to app/calculator.py, keep the implementation as a direct `return a * b`, "
            "and add one focused pytest named test_multiply in tests/test_calculator.py that "
            "asserts multiply(6, 7) == 42. Do not make any other behavioral changes. Keep the "
            "style consistent with the existing add and subtract helpers. After the task changes "
            "are committed and python -m pytest -q passes, stop immediately without extra "
            "repository exploration."
        ),
        file_path="app/calculator.py",
        line_number=1,
    )
    task_id = create_result["id"]

    completed = harness.wait_for_task_terminal(task_id, timeout_sec=1800)
    assert completed.status == TaskStatus.COMPLETED, harness.describe_task(task_id)

    initial_coder_sessions = list(completed.session_ids.get("coder", []))
    assert initial_coder_sessions, harness.describe_task(task_id)

    break_result = harness.exec_in_task_worktree(
        task_id,
        command=(
            "grep -q 'def test_forced_failure():' tests/test_calculator.py || "
            "printf '\n\ndef test_forced_failure():\n    assert multiply(2, 2) == 999\n' >> tests/test_calculator.py"
        ),
    )
    assert break_result["exit_code"] == 0, break_result

    timeout_overrides = dict(stable_model_overrides)
    timeout_overrides["timeout"] = 5
    harness.restart(config_overrides={"opencode": timeout_overrides})

    revise_result = harness.revise_task(
        task_id,
        feedback=(
            "The repository has been intentionally broken by a failing regression test. "
            "Fix the failing test so multiply coverage is correct again, rerun python -m pytest -q, and stop."
        ),
    )
    assert revise_result["ok"] is True, revise_result

    failed = harness.wait_for_task_terminal(task_id, timeout_sec=300)
    assert failed.status == TaskStatus.FAILED, harness.describe_task(task_id)
    assert "TIMEOUT after 5s" in failed.error, harness.describe_task(task_id)

    failed_sessions = list(failed.session_ids.get("coder", []))
    assert failed_sessions, harness.describe_task(task_id)
    assert failed_sessions[-1] == initial_coder_sessions[-1], harness.describe_task(
        task_id
    )

    harness.restart(config_overrides={"opencode": dict(stable_model_overrides)})

    resume_result = harness.resume_task(
        task_id, message="Continue and finish the task."
    )
    assert resume_result["ok"] is True, resume_result
    assert resume_result["session_id"] == failed_sessions[-1], resume_result

    completed = harness.wait_for_task_terminal(task_id, timeout_sec=1800)
    assert completed.status == TaskStatus.COMPLETED, harness.describe_task(task_id)

    detail = harness.get_task_detail(task_id)
    run_types = [run["agent_type"] for run in detail["runs"]]
    assert "manual_review" in run_types, detail
    assert run_types.count("coder") >= 3, detail
    assert run_types.count("reviewer") >= 2, detail

    latest_coder_sessions = completed.session_ids.get("coder", [])
    assert latest_coder_sessions[-1] == failed_sessions[-1], detail

    worktree_path = Path(completed.worktree_path)
    assert_file_contains(worktree_path / "app" / "calculator.py", "def multiply(")
    assert_file_contains(
        worktree_path / "tests" / "test_calculator.py", "test_multiply"
    )
    assert "test_forced_failure" not in (
        worktree_path / "tests" / "test_calculator.py"
    ).read_text(encoding="utf-8")
    assert_clean_worktree(worktree_path)
    assert_python_tests_pass(worktree_path)
