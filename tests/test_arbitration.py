"""Tests for Fix C: NEEDS_ARBITRATION status and resolve_arbitration logic."""

from unittest.mock import MagicMock, patch

import pytest

from core.models import Task, TaskStatus, TaskPriority, TaskSource


def _make_orchestrator(tmp_db):
    """Return a minimal Orchestrator with mocked dependencies."""
    from core.orchestrator import Orchestrator
    cfg = {
        "repo": {"path": "/fake/repo", "base_branch": "master",
                 "worktree_dir": "/fake/wt", "worktree_hooks": []},
        "opencode": {"planner_model": "m", "coder_model_default": "m",
                     "reviewer_models": []},
        "orchestrator": {"max_retries": 1, "max_workers": 1,
                         "max_parallel_tasks": 2},
        "database": {"path": ":memory:"},
        "publish": {"remote": "origin"},
    }
    with patch("core.orchestrator.WorktreeManager"), \
         patch("core.orchestrator.OpenCodeClient"), \
         patch("core.orchestrator.PlannerAgent"), \
         patch("core.orchestrator.Database", return_value=tmp_db):
        orch = Orchestrator.__new__(Orchestrator)
        orch.config = cfg
        orch.db = tmp_db
        orch.worktree_mgr = MagicMock()
        orch.client = MagicMock()
        orch.dep_tracker = MagicMock()
        orch._coder_by_complexity = {}
        orch._default_coder = MagicMock()
        orch.reviewers = []
        orch._executor = MagicMock()
        orch._pool = MagicMock()
        orch._futures = {}
        orch._lock = __import__("threading").Lock()
    return orch


class TestResolveArbitrationApprove:
    """Test the 'approve' action of resolve_arbitration."""

    def test_approve_sets_completed(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            worktree_path="/wt/task",
            branch_name="agent/task-a",
            error="Review failed after 5 attempts — needs human arbitration",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "approve")

        assert result.get("ok") is True
        assert result["action"] == "approve"
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.COMPLETED
        assert saved.review_pass is True
        assert saved.error == ""
        assert saved.completed_at > 0


class TestResolveArbitrationReject:
    """Test the 'reject' action of resolve_arbitration."""

    def test_reject_sets_failed(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            worktree_path="/wt/task",
            branch_name="agent/task-b",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "reject", feedback="Not needed")

        assert result.get("ok") is True
        assert result["action"] == "reject"
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.FAILED
        assert saved.error == "Not needed"

    def test_reject_default_error_message(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.NEEDS_ARBITRATION, worktree_path="/wt")
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "reject")

        saved = tmp_db.get_task(task.id)
        assert saved.error == "Rejected by human arbitration"


class TestResolveArbitrationRevise:
    """Test the 'revise' action of resolve_arbitration."""

    def test_revise_delegates_to_revise_task(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            worktree_path="/wt/task",
            branch_name="agent/task-c",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "revise", feedback="Please fix X")

        # revise_task should have been called, which resets status to PENDING
        assert result.get("ok") is True
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.PENDING
        assert saved.user_feedback == "Please fix X"

    def test_revise_requires_feedback(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.NEEDS_ARBITRATION, worktree_path="/wt")
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "revise", feedback="")

        assert "error" in result
        assert "feedback" in result["error"].lower()


class TestResolveArbitrationErrors:
    """Test error handling in resolve_arbitration."""

    def test_task_not_found(self, tmp_db):
        orch = _make_orchestrator(tmp_db)
        result = orch.resolve_arbitration("nonexistent", "approve")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_wrong_status(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.COMPLETED)
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "approve")

        assert "error" in result
        assert "not awaiting arbitration" in result["error"].lower()

    def test_unknown_action(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.NEEDS_ARBITRATION, worktree_path="/wt")
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "maybe")

        assert "error" in result
        assert "maybe" in result["error"]


class TestNeedsArbitrationInCleanAndRevise:
    """Verify that NEEDS_ARBITRATION tasks can be cleaned and revised."""

    def test_clean_accepts_needs_arbitration(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            branch_name="agent/task-x",
            worktree_path="/wt/agent/task-x",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)
        orch.worktree_mgr.remove_worktree.return_value = None

        result = orch.clean_task(task.id)

        assert result == {"cleaned": True, "branch": "agent/task-x"}

    def test_revise_accepts_needs_arbitration(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            worktree_path="/wt/task",
            branch_name="agent/task-y",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.revise_task(task.id, "Fix the issue please")

        assert result.get("ok") is True
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.PENDING
        assert saved.user_feedback == "Fix the issue please"
