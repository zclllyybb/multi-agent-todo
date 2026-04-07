"""Reusable assertions for regression end-to-end validations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core.models import AgentRun


def assert_agent_run_types(runs: list[AgentRun], expected_types: list[str]) -> None:
    present = {run.agent_type for run in runs}
    missing = [agent_type for agent_type in expected_types if agent_type not in present]
    assert not missing, (
        f"Missing required agent runs: {missing}; present={sorted(present)}"
    )


def assert_file_contains(path: Path, expected_snippet: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert expected_snippet in content, (
        f"Expected to find {expected_snippet!r} in {path}"
    )


def assert_branch_contains_submission(
    worktree_path: Path, base_ref: str = "origin/master"
) -> None:
    result = _run_checked(
        ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
        cwd=worktree_path,
        description="counting task commits",
    )
    commit_count = int(result.stdout.strip() or "0")
    assert commit_count > 0, (
        f"Expected at least one task commit ahead of {base_ref}, got {commit_count}"
    )


def assert_clean_worktree(worktree_path: Path) -> None:
    result = _run_checked(
        ["git", "status", "--short"],
        cwd=worktree_path,
        description="checking worktree cleanliness",
    )
    assert result.stdout.strip() == "", (
        f"Expected a clean worktree after task completion, got:\n{result.stdout}"
    )


def assert_python_tests_pass(repo_path: Path) -> None:
    _run_checked(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo_path,
        description="running fixture project tests",
        timeout=300,
    )


def assert_file_exists(path: Path) -> None:
    assert path.exists(), f"Expected path to exist: {path}"


def _run_checked(
    cmd: list[str],
    *,
    cwd: Path,
    description: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Command failed while {description}: {cmd!r}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result
