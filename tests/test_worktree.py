"""Tests for WorktreeManager.remove_worktree and Orchestrator.clean_task.

Key invariant: clean uses the task-stored worktree_path, not the config-derived
path — so a config change after task creation cannot cause a silent miss.
"""

import os
import shutil
from unittest.mock import MagicMock, patch, call

import pytest

from core.worktree import WorktreeManager
from core.models import Task, TaskStatus, TaskPriority, TaskSource


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_wm(repo_path: str, worktree_dir: str) -> WorktreeManager:
    wm = WorktreeManager(repo_path=repo_path, worktree_dir=worktree_dir)
    return wm


def _git_ok() -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


def _git_fail(stderr: str = "error") -> MagicMock:
    r = MagicMock()
    r.returncode = 1
    r.stdout = ""
    r.stderr = stderr
    return r


# ─────────────────────────────────────────────────────────────────────────────
# remove_worktree: uses the correct path
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoveWorktreePathSelection:
    """Verify that remove_worktree acts on the task-provided path, not the
    config-derived path, so a stale/changed config cannot cause a silent miss.
    """

    def test_uses_provided_worktree_path(self, tmp_path):
        """When worktree_path is given, that directory is removed — not the
        config-derived one (which may not exist or may be a different location).
        """
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        # config-derived dir — deliberately different from actual worktree
        config_wt_dir = tmp_path / "config_worktrees"
        config_wt_dir.mkdir()
        # actual worktree is somewhere else (simulates config drift)
        actual_wt = tmp_path / "actual_worktrees" / "agent" / "task-abc"
        actual_wt.mkdir(parents=True)

        wm = _make_wm(str(repo_dir), str(config_wt_dir))

        # git worktree remove deletes the directory (simulate real behaviour),
        # branch -D and rev-parse succeed/fail as expected.
        with patch.object(wm, "_run_git") as mock_git:

            def _side(cmd, *args, cwd=None):
                r = _git_ok()
                if cmd == "worktree" and args and args[0] == "remove":
                    # simulate git actually removing the directory
                    path = args[-1]
                    if os.path.exists(path):
                        shutil.rmtree(path)
                elif cmd == "rev-parse":
                    r.returncode = 1  # branch does not exist → good
                return r

            mock_git.side_effect = _side

            wm.remove_worktree("agent/task-abc", worktree_path=str(actual_wt))

        # The actual worktree directory must have been operated on
        calls_str = " ".join(str(c) for c in mock_git.call_args_list)
        assert str(actual_wt) in calls_str

    def test_config_derived_path_not_used_when_provided(self, tmp_path):
        """The config-derived path must NOT appear in git calls when a real
        worktree_path is provided (it would operate on the wrong location).
        """
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config_wt_dir = tmp_path / "config_worktrees"
        config_wt_dir.mkdir()
        actual_wt = tmp_path / "real" / "agent" / "task-abc"
        actual_wt.mkdir(parents=True)

        wm = _make_wm(str(repo_dir), str(config_wt_dir))
        config_path = os.path.join(str(config_wt_dir), "agent/task-abc")

        with patch.object(wm, "_run_git") as mock_git:

            def _side(cmd, *args, cwd=None):
                r = _git_ok()
                if cmd == "worktree" and args and args[0] == "remove":
                    path = args[-1]
                    if os.path.exists(path):
                        shutil.rmtree(path)
                elif cmd == "rev-parse":
                    r.returncode = 1
                return r

            mock_git.side_effect = _side

            wm.remove_worktree("agent/task-abc", worktree_path=str(actual_wt))

        calls_str = " ".join(str(c) for c in mock_git.call_args_list)
        # config-derived path must not be used in any git call
        assert config_path not in calls_str

    def test_fallback_to_git_query_when_no_path_provided(self, tmp_path):
        """Without worktree_path, falls back to git worktree list to find the
        real path — avoiding silent miss due to stale config.
        """
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config_wt_dir = tmp_path / "config_wt"
        config_wt_dir.mkdir()
        real_wt = tmp_path / "real_wt" / "agent" / "task-xyz"
        real_wt.mkdir(parents=True)

        wm = _make_wm(str(repo_dir), str(config_wt_dir))

        # Patch list_worktrees to return real_wt for this branch
        with (
            patch.object(wm, "list_worktrees") as mock_list,
            patch.object(wm, "_run_git") as mock_git,
        ):
            mock_list.return_value = [
                {"path": str(real_wt), "branch": "refs/heads/agent/task-xyz"},
            ]

            def _side(cmd, *args, cwd=None):
                r = _git_ok()
                if cmd == "worktree" and args and args[0] == "remove":
                    path = args[-1]
                    if os.path.exists(path):
                        shutil.rmtree(path)
                elif cmd == "rev-parse":
                    r.returncode = 1
                return r

            mock_git.side_effect = _side

            wm.remove_worktree("agent/task-xyz")  # no worktree_path provided

        calls_str = " ".join(str(c) for c in mock_git.call_args_list)
        assert str(real_wt) in calls_str


# ─────────────────────────────────────────────────────────────────────────────
# remove_worktree: failure cases raise RuntimeError
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoveWorktreeFailures:
    def test_raises_if_directory_still_exists_after_cleanup(self, tmp_path):
        """If the directory cannot be removed, RuntimeError is raised so callers
        know the operation truly failed — not a silent pass.
        """
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        actual = wt_dir / "agent" / "task-abc"
        actual.mkdir(parents=True)

        wm = _make_wm(str(repo_dir), str(wt_dir))

        # git worktree remove fails AND shutil.rmtree also fails
        with (
            patch.object(wm, "_run_git") as mock_git,
            patch("shutil.rmtree", side_effect=OSError("permission denied")),
        ):
            mock_git.return_value = _git_fail()

            with pytest.raises((RuntimeError, OSError)):
                wm.remove_worktree("agent/task-abc", worktree_path=str(actual))

    def test_raises_if_branch_still_exists_after_deletion(self, tmp_path):
        """If git branch -D fails silently (returncode 0 but branch remains),
        rev-parse verification detects it and raises RuntimeError.
        """
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        # no actual directory — the worktree dir doesn't exist, that's fine
        wm = _make_wm(str(repo_dir), str(wt_dir))

        with (
            patch.object(wm, "_run_git") as mock_git,
            patch.object(wm, "list_worktrees", return_value=[]),
        ):

            def _side(cmd, *args, cwd=None):
                r = _git_ok()
                if cmd == "rev-parse":
                    r.returncode = 0  # branch still exists → should raise
                return r

            mock_git.side_effect = _side

            with pytest.raises(RuntimeError, match="Branch still exists"):
                wm.remove_worktree("agent/task-gone")


class TestRemoveWorktreeManualDeletion:
    """Idempotency when resources were manually removed before clean."""

    def test_succeeds_when_branch_and_worktree_already_removed(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        wm = _make_wm(str(repo_dir), str(wt_dir))

        with (
            patch.object(wm, "list_worktrees", return_value=[]),
            patch.object(wm, "_run_git") as mock_git,
        ):

            def _side(cmd, *args, cwd=None):
                r = _git_ok()
                if cmd == "rev-parse":
                    r.returncode = 1
                return r

            mock_git.side_effect = _side

            wm.remove_worktree("agent/task-missing")

        assert call("branch", "-D", "agent/task-missing") in mock_git.call_args_list

    def test_succeeds_when_branch_removed_but_worktree_still_exists(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        actual = wt_dir / "agent" / "task-manual"
        actual.mkdir(parents=True)

        wm = _make_wm(str(repo_dir), str(wt_dir))

        with patch.object(wm, "_run_git") as mock_git:

            def _side(cmd, *args, cwd=None):
                r = _git_ok()
                if cmd == "worktree" and args and args[0] == "remove":
                    path = args[-1]
                    if os.path.exists(path):
                        shutil.rmtree(path)
                elif cmd == "rev-parse":
                    r.returncode = 1
                return r

            mock_git.side_effect = _side

            wm.remove_worktree("agent/task-manual", worktree_path=str(actual))

        assert not actual.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator.clean_task: error propagation
# ─────────────────────────────────────────────────────────────────────────────


class TestCleanTaskErrorPropagation:
    """Verify that clean_task does NOT clear DB fields and returns an error
    dict when remove_worktree raises — so the Clean button stays visible.
    """

    def _make_orchestrator(self, tmp_db):
        """Return a minimal Orchestrator with mocked worktree_mgr and client."""
        from core.orchestrator import Orchestrator

        cfg = {
            "repo": {
                "path": "/fake/repo",
                "base_branch": "master",
                "worktree_dir": "/fake/wt",
                "worktree_hooks": [],
            },
            "opencode": {
                "planner_model": "m",
                "coder_model_default": "m",
                "reviewer_models": [],
            },
            "orchestrator": {"max_retries": 1, "max_workers": 1},
            "database": {"path": ":memory:"},
            "publish": {"remote": "origin"},
        }
        with (
            patch("core.orchestrator.WorktreeManager"),
            patch("core.orchestrator.OpenCodeClient"),
            patch("core.orchestrator.PlannerAgent"),
            patch("core.orchestrator.Database", return_value=tmp_db),
        ):
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
        return orch

    def test_clean_success_clears_fields(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.COMPLETED,
            branch_name="agent/task-aaa",
            worktree_path="/real/wt/agent/task-aaa",
        )
        tmp_db.save_task(task)
        orch = self._make_orchestrator(tmp_db)
        orch.worktree_mgr.remove_worktree.return_value = None  # success

        result = orch.clean_task(task.id)

        assert result == {"cleaned": True, "branch": "agent/task-aaa"}
        saved = tmp_db.get_task(task.id)
        assert saved.branch_name == ""
        assert saved.worktree_path == ""

    def test_clean_failure_preserves_fields(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.COMPLETED,
            branch_name="agent/task-bbb",
            worktree_path="/real/wt/agent/task-bbb",
        )
        tmp_db.save_task(task)
        orch = self._make_orchestrator(tmp_db)
        orch.worktree_mgr.remove_worktree.side_effect = RuntimeError("disk full")

        result = orch.clean_task(task.id)

        assert "error" in result
        assert "disk full" in result["error"]
        # fields must NOT be cleared — Clean button stays visible
        saved = tmp_db.get_task(task.id)
        assert saved.branch_name == "agent/task-bbb"
        assert saved.worktree_path == "/real/wt/agent/task-bbb"

    def test_clean_passes_task_worktree_path_to_remove(self, tmp_db, make_task):
        """The task-stored worktree_path (not config path) is forwarded."""
        task = make_task(
            status=TaskStatus.FAILED,
            branch_name="agent/task-ccc",
            worktree_path="/task/stored/path/agent/task-ccc",
        )
        tmp_db.save_task(task)
        orch = self._make_orchestrator(tmp_db)
        orch.worktree_mgr.remove_worktree.return_value = None

        orch.clean_task(task.id)

        orch.worktree_mgr.remove_worktree.assert_called_once_with(
            "agent/task-ccc",
            worktree_path="/task/stored/path/agent/task-ccc",
        )

    def test_clean_rejected_for_running_task(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.CODING, branch_name="agent/task-ddd")
        tmp_db.save_task(task)
        orch = self._make_orchestrator(tmp_db)

        result = orch.clean_task(task.id)

        assert "error" in result
        orch.worktree_mgr.remove_worktree.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator.cancel_task: worktree cleanup failure keeps fields intact
# ─────────────────────────────────────────────────────────────────────────────


class TestCancelTaskWorktreeCleanup:
    def _make_orchestrator(self, tmp_db):
        from core.orchestrator import Orchestrator

        with (
            patch("core.orchestrator.WorktreeManager"),
            patch("core.orchestrator.OpenCodeClient"),
            patch("core.orchestrator.PlannerAgent"),
            patch("core.orchestrator.Database", return_value=tmp_db),
        ):
            orch = Orchestrator.__new__(Orchestrator)
            orch.config = {
                "repo": {
                    "path": "/r",
                    "base_branch": "master",
                    "worktree_dir": "/wt",
                    "worktree_hooks": [],
                },
                "opencode": {
                    "planner_model": "m",
                    "coder_model_default": "m",
                    "reviewer_models": [],
                },
                "orchestrator": {"max_retries": 1, "max_workers": 1},
                "database": {"path": ":memory:"},
                "publish": {"remote": "origin"},
            }
            orch.db = tmp_db
            orch.worktree_mgr = MagicMock()
            orch.client = MagicMock()
            orch.dep_tracker = MagicMock()
            orch._coder_by_complexity = {}
            orch._default_coder = MagicMock()
            orch.reviewers = []
            orch._executor = MagicMock()
        return orch

    def test_cancel_clears_fields_on_successful_worktree_removal(
        self, tmp_db, make_task
    ):
        task = make_task(
            status=TaskStatus.CODING,
            branch_name="agent/task-eee",
            worktree_path="/wt/agent/task-eee",
        )
        tmp_db.save_task(task)
        orch = self._make_orchestrator(tmp_db)
        orch.worktree_mgr.remove_worktree.return_value = None

        result = orch.cancel_task(task.id)

        assert result == {"cancelled": True}
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.CANCELLED
        assert saved.branch_name == ""
        assert saved.worktree_path == ""

    def test_cancel_preserves_fields_on_failed_worktree_removal(
        self, tmp_db, make_task
    ):
        task = make_task(
            status=TaskStatus.REVIEWING,
            branch_name="agent/task-fff",
            worktree_path="/wt/agent/task-fff",
        )
        tmp_db.save_task(task)
        orch = self._make_orchestrator(tmp_db)
        orch.worktree_mgr.remove_worktree.side_effect = RuntimeError("locked")

        result = orch.cancel_task(task.id)

        assert result == {"cancelled": True}  # cancel itself succeeds
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.CANCELLED
        # fields preserved — Clean button will remain visible
        assert saved.branch_name == "agent/task-fff"
        assert saved.worktree_path == "/wt/agent/task-fff"

    def test_cancel_passes_task_worktree_path_to_remove(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.CODING,
            branch_name="agent/task-ggg",
            worktree_path="/actual/stored/path/agent/task-ggg",
        )
        tmp_db.save_task(task)
        orch = self._make_orchestrator(tmp_db)
        orch.worktree_mgr.remove_worktree.return_value = None

        orch.cancel_task(task.id)

        orch.worktree_mgr.remove_worktree.assert_called_once_with(
            "agent/task-ggg",
            worktree_path="/actual/stored/path/agent/task-ggg",
        )


class TestReviewOnlyTaskCleanup:
    def test_review_cleanup_success_clears_branch_and_worktree(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.COMPLETED,
            task_mode="review",
            branch_name="agent/review-aaa",
            worktree_path="/wt/agent/review-aaa",
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)
        orch.worktree_mgr.remove_worktree.return_value = None

        orch._cleanup_review_worktree(task)

        saved = tmp_db.get_task(task.id)
        assert saved.branch_name == ""
        assert saved.worktree_path == ""
        orch.worktree_mgr.remove_worktree.assert_called_once_with("agent/review-aaa")

    def test_review_cleanup_failure_keeps_branch_for_manual_clean(
        self, tmp_db, make_task
    ):
        task = make_task(
            status=TaskStatus.COMPLETED,
            task_mode="review",
            branch_name="agent/review-bbb",
            worktree_path="/wt/agent/review-bbb",
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)
        orch.worktree_mgr.remove_worktree.side_effect = RuntimeError("locked")

        orch._cleanup_review_worktree(task)

        saved = tmp_db.get_task(task.id)
        assert saved.branch_name == "agent/review-bbb"
        assert saved.worktree_path == ""


class TestCleanVisibilityByActualResources:
    def test_clean_hidden_when_stale_record_has_no_real_resources(
        self, tmp_db, make_task
    ):
        from core.orchestrator import Orchestrator

        task = make_task(
            status=TaskStatus.COMPLETED,
            branch_name="agent/task-stale",
            worktree_path="/missing/worktree",
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)

        with (
            patch.object(
                Orchestrator, "_collect_resource_snapshot", return_value=(set(), {})
            ),
            patch("core.orchestrator.os.path.isdir", return_value=False),
        ):
            ui_task = orch.serialize_task_for_ui(task)

        assert ui_task["actual_branch_exists"] is False
        assert ui_task["actual_worktree_exists"] is False
        assert ui_task["clean_available"] is False

    def test_serialize_task_for_ui_includes_comment_metadata(self, tmp_db, make_task):
        from core.orchestrator import Orchestrator

        task = make_task(
            comments=[
                {
                    "id": "c1",
                    "username": "alice",
                    "content": "please verify",
                    "created_at": 1.0,
                }
            ]
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)

        with (
            patch.object(
                Orchestrator, "_collect_resource_snapshot", return_value=(set(), {})
            ),
            patch("core.orchestrator.os.path.isdir", return_value=False),
        ):
            ui_task = orch.serialize_task_for_ui(task)

        assert ui_task["has_comments"] is True
        assert ui_task["comment_count"] == 1

    def test_clean_visible_when_only_branch_exists(self, tmp_db, make_task):
        from core.orchestrator import Orchestrator

        task = make_task(
            status=TaskStatus.COMPLETED,
            branch_name="agent/task-branch-only",
            worktree_path="/missing/worktree",
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)

        with (
            patch.object(
                Orchestrator,
                "_collect_resource_snapshot",
                return_value=({"agent/task-branch-only"}, {}),
            ),
            patch("core.orchestrator.os.path.isdir", return_value=False),
        ):
            ui_task = orch.serialize_task_for_ui(task)

        assert ui_task["actual_branch_exists"] is True
        assert ui_task["actual_worktree_exists"] is False
        assert ui_task["clean_available"] is True

    def test_clean_visible_when_only_worktree_exists(self, tmp_db, make_task):
        from core.orchestrator import Orchestrator

        task = make_task(
            status=TaskStatus.FAILED,
            branch_name="agent/task-worktree-only",
            worktree_path="/present/worktree",
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)

        def _isdir(path):
            return path == "/present/worktree"

        with (
            patch.object(
                Orchestrator, "_collect_resource_snapshot", return_value=(set(), {})
            ),
            patch("core.orchestrator.os.path.isdir", side_effect=_isdir),
        ):
            ui_task = orch.serialize_task_for_ui(task)

        assert ui_task["actual_branch_exists"] is False
        assert ui_task["actual_worktree_exists"] is True
        assert ui_task["clean_available"] is True

    def test_clean_visible_when_branch_name_missing_but_worktree_exists(
        self, tmp_db, make_task
    ):
        from core.orchestrator import Orchestrator

        task = make_task(
            status=TaskStatus.CANCELLED,
            branch_name="",
            worktree_path="/present/worktree-only",
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)

        def _isdir(path):
            return path == "/present/worktree-only"

        with (
            patch.object(
                Orchestrator, "_collect_resource_snapshot", return_value=(set(), {})
            ),
            patch("core.orchestrator.os.path.isdir", side_effect=_isdir),
        ):
            ui_task = orch.serialize_task_for_ui(task)

        assert ui_task["actual_branch_exists"] is False
        assert ui_task["actual_worktree_exists"] is True
        assert ui_task["clean_available"] is True

    def test_clean_success_clears_record_then_hides_button(self, tmp_db, make_task):
        from core.orchestrator import Orchestrator

        task = make_task(
            status=TaskStatus.COMPLETED,
            branch_name="agent/task-cleaned",
            worktree_path="/wt/agent/task-cleaned",
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)
        orch.worktree_mgr.remove_worktree.return_value = None

        result = orch.clean_task(task.id)
        assert result == {"cleaned": True, "branch": "agent/task-cleaned"}

        saved = tmp_db.get_task(task.id)
        assert saved.branch_name == ""
        assert saved.worktree_path == ""

        with (
            patch.object(
                Orchestrator, "_collect_resource_snapshot", return_value=(set(), {})
            ),
            patch("core.orchestrator.os.path.isdir", return_value=False),
        ):
            ui_task = orch.serialize_task_for_ui(saved)

        assert ui_task["clean_available"] is False

    def test_clean_worktree_only_without_branch_name(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.CANCELLED,
            branch_name="",
            worktree_path="/wt/orphan-worktree",
        )
        tmp_db.save_task(task)
        orch = _orch_helper(tmp_db)
        orch.worktree_mgr.remove_worktree_path_only.return_value = None

        result = orch.clean_task(task.id)

        assert result == {"cleaned": True, "branch": ""}
        saved = tmp_db.get_task(task.id)
        assert saved.branch_name == ""
        assert saved.worktree_path == ""
        orch.worktree_mgr.remove_worktree_path_only.assert_called_once_with(
            "/wt/orphan-worktree"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Cascading cancel / clean to child tasks
# ─────────────────────────────────────────────────────────────────────────────


def _orch_helper(tmp_db):
    from core.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {
        "repo": {
            "path": "/r",
            "base_branch": "master",
            "worktree_dir": "/wt",
            "worktree_hooks": [],
        },
        "opencode": {
            "planner_model": "m",
            "coder_model_default": "m",
            "reviewer_models": [],
        },
        "orchestrator": {"max_retries": 1, "max_workers": 1},
        "database": {"path": ":memory:"},
        "publish": {"remote": "origin"},
    }
    orch.db = tmp_db
    orch.worktree_mgr = MagicMock()
    orch.worktree_mgr.remove_worktree.return_value = None
    orch.client = MagicMock()
    orch.dep_tracker = MagicMock()
    orch.dep_tracker.get_children.return_value = set()
    orch._coder_by_complexity = {}
    orch._default_coder = MagicMock()
    orch.reviewers = []
    orch._executor = MagicMock()
    return orch


class TestCascadingCancel:
    def test_cancel_cascades_to_children(self, tmp_db, make_task):
        parent = make_task(status=TaskStatus.PLANNING)
        tmp_db.save_task(parent)
        c1 = make_task(
            status=TaskStatus.CODING,
            parent_id=parent.id,
            branch_name="agent/c1",
            worktree_path="/wt/c1",
        )
        c2 = make_task(status=TaskStatus.PLANNING, parent_id=parent.id)
        tmp_db.save_task(c1)
        tmp_db.save_task(c2)
        orch = _orch_helper(tmp_db)
        orch.cancel_task(parent.id)
        assert tmp_db.get_task(c1.id).status == TaskStatus.CANCELLED
        assert tmp_db.get_task(c2.id).status == TaskStatus.CANCELLED

    def test_cancel_cascades_recursively(self, tmp_db, make_task):
        gp = make_task(status=TaskStatus.PLANNING)
        tmp_db.save_task(gp)
        p = make_task(
            status=TaskStatus.CODING,
            parent_id=gp.id,
            branch_name="agent/p",
            worktree_path="/wt/p",
        )
        tmp_db.save_task(p)
        c = make_task(
            status=TaskStatus.CODING,
            parent_id=p.id,
            branch_name="agent/c",
            worktree_path="/wt/c",
        )
        tmp_db.save_task(c)
        orch = _orch_helper(tmp_db)
        orch.cancel_task(gp.id)
        assert tmp_db.get_task(p.id).status == TaskStatus.CANCELLED
        assert tmp_db.get_task(c.id).status == TaskStatus.CANCELLED

    def test_cancel_skips_completed_children(self, tmp_db, make_task):
        parent = make_task(status=TaskStatus.PLANNING)
        tmp_db.save_task(parent)
        done = make_task(status=TaskStatus.COMPLETED, parent_id=parent.id)
        active = make_task(
            status=TaskStatus.CODING,
            parent_id=parent.id,
            branch_name="a/x",
            worktree_path="/wt/x",
        )
        tmp_db.save_task(done)
        tmp_db.save_task(active)
        orch = _orch_helper(tmp_db)
        orch.cancel_task(parent.id)
        assert tmp_db.get_task(done.id).status == TaskStatus.COMPLETED
        assert tmp_db.get_task(active.id).status == TaskStatus.CANCELLED


class TestCascadingClean:
    def test_clean_cascades_to_children(self, tmp_db, make_task):
        parent = make_task(
            status=TaskStatus.COMPLETED, branch_name="agent/p", worktree_path="/wt/p"
        )
        tmp_db.save_task(parent)
        c = make_task(
            status=TaskStatus.COMPLETED,
            parent_id=parent.id,
            branch_name="agent/c",
            worktree_path="/wt/c",
        )
        tmp_db.save_task(c)
        orch = _orch_helper(tmp_db)
        orch.clean_task(parent.id)
        assert tmp_db.get_task(parent.id).branch_name == ""
        assert tmp_db.get_task(c.id).branch_name == ""

    def test_clean_skips_children_without_branch(self, tmp_db, make_task):
        parent = make_task(
            status=TaskStatus.CANCELLED, branch_name="agent/p", worktree_path="/wt/p"
        )
        tmp_db.save_task(parent)
        c = make_task(status=TaskStatus.CANCELLED, parent_id=parent.id)
        tmp_db.save_task(c)
        orch = _orch_helper(tmp_db)
        result = orch.clean_task(parent.id)
        assert result.get("cleaned") is True
        orch.worktree_mgr.remove_worktree.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Planner sub-tasks must not be split further (no recursive decomposition)
# ─────────────────────────────────────────────────────────────────────────────


class TestNoRecursiveSplit:
    """_execute_task: a task with source=PLANNER must never be split again,
    even if analyze_and_split returns is_split=True."""

    def _make_full_orchestrator(self, tmp_db):
        """Orchestrator with planner mocked but real _execute_task logic."""
        from core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {
            "repo": {
                "path": "/r",
                "base_branch": "master",
                "worktree_dir": "/wt",
                "worktree_hooks": [],
            },
            "opencode": {
                "planner_model": "m",
                "coder_model_default": "m",
                "reviewer_models": [],
                "coder_model_by_complexity": {},
            },
            "orchestrator": {
                "max_retries": 0,
                "max_workers": 1,
                "max_parallel_tasks": 4,
            },
            "database": {"path": ":memory:"},
            "publish": {"remote": "origin"},
        }
        orch.db = tmp_db
        orch.worktree_mgr = MagicMock()
        orch.worktree_mgr.create_worktree.return_value = "/wt/branch"
        orch.client = MagicMock()
        orch.dep_tracker = MagicMock()
        orch.dep_tracker.get_children.return_value = set()
        orch.dep_tracker.is_blocked.return_value = False
        orch.dep_tracker.resolve_indices.return_value = [[], []]
        orch._coder_by_complexity = {}
        orch._default_coder = MagicMock()
        orch._default_coder.implement_task.return_value = (
            self._make_coder_run(),
            "done",
        )
        orch.reviewers = []
        orch.planner = MagicMock()
        orch._lock = __import__("threading").Lock()
        orch._futures = {}
        orch._pending_dispatch = []
        orch._executor = MagicMock()
        return orch

    def _make_plan_run(self, task_id=""):
        from core.models import AgentRun

        return AgentRun(
            task_id=task_id,
            agent_type="planner",
            model="m",
            prompt="p",
            output="o",
            exit_code=0,
            session_id="",
        )

    def _make_coder_run(self, task_id=""):
        from core.models import AgentRun

        run = AgentRun(
            task_id=task_id,
            agent_type="coder",
            model="m",
            prompt="p",
            output="o",
            exit_code=0,
            session_id="s1",
        )
        return run

    def test_planner_subtask_ignores_split_true(self, tmp_db, make_task):
        """When source=PLANNER and planner returns split=true, is_split is forced False."""
        task = make_task(status=TaskStatus.PENDING, source=TaskSource.PLANNER)
        tmp_db.save_task(task)
        orch = self._make_full_orchestrator(tmp_db)

        # planner claims it wants to split into 2 sub-tasks
        orch.planner.analyze_and_split.return_value = (
            self._make_plan_run(),
            True,  # is_split = True — should be ignored for PLANNER tasks
            "",
            [
                {
                    "title": "A",
                    "description": "a",
                    "priority": "medium",
                    "depends_on": [],
                },
                {
                    "title": "B",
                    "description": "b",
                    "priority": "medium",
                    "depends_on": [],
                },
            ],
            "complex",
        )

        orch._execute_task(task.id)

        # No child tasks should have been created
        children = [t for t in tmp_db.get_all_tasks() if t.parent_id == task.id]
        assert children == [], f"Expected no children, got {[c.id for c in children]}"
        # Coder should have been invoked (task executed as single task)
        orch._default_coder.implement_task.assert_called_once()

    def test_manual_task_can_still_split(self, tmp_db, make_task):
        """Tasks submitted by users (source=MANUAL) should still be splittable."""
        task = make_task(status=TaskStatus.PENDING, source=TaskSource.MANUAL)
        tmp_db.save_task(task)
        orch = self._make_full_orchestrator(tmp_db)
        orch._pending_dispatch = []

        orch.planner.analyze_and_split.return_value = (
            self._make_plan_run(),
            True,
            "",
            [
                {
                    "title": "A",
                    "description": "a",
                    "priority": "medium",
                    "depends_on": [],
                },
                {
                    "title": "B",
                    "description": "b",
                    "priority": "medium",
                    "depends_on": [1],
                },
            ],
            "complex",
        )
        orch.dep_tracker.resolve_indices.return_value = [[], ["child-id-placeholder"]]

        orch._execute_task(task.id)

        # Children should have been created
        children = [t for t in tmp_db.get_all_tasks() if t.parent_id == task.id]
        assert len(children) == 2
        # Coder should NOT have been called on the parent
        orch._default_coder.implement_task.assert_not_called()

    def test_split_queues_ready_child_when_parent_occupies_only_slot(
        self, tmp_db, make_task
    ):
        """A split parent should not deadlock child dispatch at max_parallel_tasks=1."""
        from core.orchestrator import Orchestrator

        task = make_task(status=TaskStatus.PENDING, source=TaskSource.MANUAL)
        tmp_db.save_task(task)
        orch = self._make_full_orchestrator(tmp_db)
        orch.config["orchestrator"]["max_parallel_tasks"] = 1
        orch._pending_dispatch = []
        orch._futures = {task.id: MagicMock()}

        def _dispatch(task_id):
            if task_id not in orch._pending_dispatch:
                orch._pending_dispatch.append(task_id)
            return False

        orch.dispatch_task = MagicMock(side_effect=_dispatch)

        orch.planner.analyze_and_split.return_value = (
            self._make_plan_run(),
            True,
            "",
            [
                {
                    "title": "A",
                    "description": "a",
                    "priority": "medium",
                    "depends_on": [],
                },
                {
                    "title": "B",
                    "description": "b",
                    "priority": "medium",
                    "depends_on": [0],
                },
            ],
            "complex",
        )
        orch.dep_tracker.resolve_indices.return_value = [[], ["child-b"]]
        orch.dep_tracker.is_blocked.side_effect = [False, True]

        orch._execute_task(task.id)

        children = [t for t in tmp_db.get_all_tasks() if t.parent_id == task.id]
        ready_children = [t for t in children if not t.depends_on]
        assert len(ready_children) == 1
        assert orch._pending_dispatch == [ready_children[0].id]


class TestDeferredDispatch:
    def test_flush_pending_dispatches_after_slot_frees(self):
        from core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {"orchestrator": {"max_parallel_tasks": 1}}
        orch._lock = __import__("threading").Lock()
        orch._futures = {}
        orch._pending_dispatch = ["child-1"]

        called = []

        def _dispatch(task_id):
            called.append(task_id)
            return True

        orch.dispatch_task = _dispatch

        orch._flush_pending_dispatches()

        assert called == ["child-1"]
        assert orch._pending_dispatch == []

    def test_dispatch_task_queues_manual_task_when_parallel_limit_is_full(self):
        from core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {"orchestrator": {"max_parallel_tasks": 1}}
        orch._lock = __import__("threading").Lock()
        orch._futures = {"running-1": object()}
        orch._pending_dispatch = []
        orch._pool = MagicMock()
        orch.dep_tracker = MagicMock()
        orch.dep_tracker.is_blocked.return_value = False
        orch.db = MagicMock()
        orch.db.get_task.return_value = Task(id="manual-1", title="Manual task")

        dispatched = orch.dispatch_task("manual-1")

        assert dispatched is False
        assert orch._pending_dispatch == ["manual-1"]

    def test_dispatch_task_removes_task_from_pending_queue_when_started(self):
        from core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {"orchestrator": {"max_parallel_tasks": 1}}
        orch._lock = __import__("threading").Lock()
        orch._futures = {}
        orch._pending_dispatch = ["manual-2"]
        orch._pool = MagicMock()
        orch.dep_tracker = MagicMock()
        orch.dep_tracker.is_blocked.return_value = False
        orch.db = MagicMock()
        orch.db.get_task.return_value = Task(id="manual-2", title="Manual task")

        future = object()
        orch._pool.submit.return_value = future

        dispatched = orch.dispatch_task("manual-2")

        assert dispatched is True
        assert orch._pending_dispatch == []
        assert orch._futures["manual-2"] is future


class TestCoderRunFailureClassification:
    def _make_execute_orchestrator(self, tmp_db):
        from core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {
            "repo": {
                "path": "/r",
                "base_branch": "master",
                "worktree_dir": "/wt",
                "worktree_hooks": [],
            },
            "opencode": {
                "planner_model": "m",
                "coder_model_default": "m",
                "reviewer_models": [],
                "coder_model_by_complexity": {},
            },
            "orchestrator": {
                "max_retries": 0,
                "max_workers": 1,
                "max_parallel_tasks": 4,
            },
            "database": {"path": ":memory:"},
            "publish": {"remote": "origin"},
        }
        orch.db = tmp_db
        orch.worktree_mgr = MagicMock()
        orch.worktree_mgr.create_worktree.return_value = "/wt/branch"
        orch.client = MagicMock()
        orch.dep_tracker = MagicMock()
        orch.dep_tracker.get_children.return_value = set()
        orch.dep_tracker.is_blocked.return_value = False
        orch.dep_tracker.resolve_indices.return_value = [[], []]
        orch._coder_by_complexity = {}
        orch._default_coder = MagicMock()
        orch.reviewers = []
        orch.planner = MagicMock()
        orch._lock = __import__("threading").Lock()
        orch._futures = {}
        orch._pending_dispatch = []
        orch._executor = MagicMock()
        return orch

    def test_timeout_error_not_reported_as_incomplete(self, tmp_db, make_task):
        from core.models import AgentRun

        task = make_task(status=TaskStatus.PENDING, source=TaskSource.MANUAL)
        tmp_db.save_task(task)
        orch = self._make_execute_orchestrator(tmp_db)

        plan_run = AgentRun(
            task_id=task.id,
            agent_type="planner",
            model="m",
            prompt="p",
            output="o",
            exit_code=0,
        )
        orch.planner.analyze_and_split.return_value = (
            plan_run,
            False,
            "plan text",
            [],
            "simple",
        )

        timeout_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="p",
            output='{"sessionID":"ses_timeout_case"}\nTIMEOUT after 7200s',
            exit_code=-1,
            session_id="ses_timeout_case",
        )
        orch._default_coder.implement_task.return_value = (timeout_run, "")
        orch.client.is_output_complete.return_value = False

        orch._execute_task(task.id)

        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.FAILED
        assert "TIMEOUT after 7200s" in saved.error
        assert "timed out" in saved.error.lower()
        assert "incomplete" not in saved.error.lower()


class TestReviewerCoderResponseSelection:
    def _make_execute_orchestrator(self, tmp_db):
        from core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {
            "repo": {
                "path": "/r",
                "base_branch": "master",
                "worktree_dir": "/wt",
                "worktree_hooks": [],
            },
            "opencode": {
                "planner_model": "m",
                "coder_model_default": "m",
                "reviewer_models": [],
                "coder_model_by_complexity": {},
            },
            "orchestrator": {
                "max_retries": 0,
                "max_workers": 1,
                "max_parallel_tasks": 4,
            },
            "database": {"path": ":memory:"},
            "publish": {"remote": "origin"},
        }
        orch.db = tmp_db
        orch.worktree_mgr = MagicMock()
        orch.worktree_mgr.create_worktree.return_value = "/wt/branch"
        orch.client = MagicMock()
        orch.dep_tracker = MagicMock()
        orch.dep_tracker.get_children.return_value = set()
        orch.dep_tracker.is_blocked.return_value = False
        orch.dep_tracker.resolve_indices.return_value = [[], []]
        orch._coder_by_complexity = {}
        orch._default_coder = MagicMock()
        orch.reviewers = []
        orch.planner = MagicMock()
        orch._lock = __import__("threading").Lock()
        orch._futures = {}
        orch._pending_dispatch = []
        orch._executor = MagicMock()
        return orch

    def _make_plan_run(self, task_id=""):
        from core.models import AgentRun

        return AgentRun(
            task_id=task_id,
            agent_type="planner",
            model="m",
            prompt="p",
            output="o",
            exit_code=0,
            session_id="",
        )

    def test_execute_task_passes_only_last_coder_text_block_to_reviewer(
        self, tmp_db, make_task
    ):
        from core.models import AgentRun

        task = make_task(status=TaskStatus.PENDING, source=TaskSource.MANUAL)
        tmp_db.save_task(task)
        orch = self._make_execute_orchestrator(tmp_db)

        orch.planner.analyze_and_split.return_value = (
            self._make_plan_run(task.id),
            False,
            "plan text",
            [],
            "simple",
        )

        code_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="p",
            output="full coder transcript with many steps",
            exit_code=0,
            session_id="ses_code",
        )
        orch._default_coder.implement_task.return_value = (
            code_run,
            "full coder transcript with many steps",
        )
        orch.client.is_output_complete.return_value = True
        orch.client.extract_last_text_block.return_value = "final coder summary"

        reviewer = MagicMock()
        reviewer.model = "rev-m"
        review_run = AgentRun(
            task_id=task.id,
            agent_type="reviewer",
            model="rev-m",
            prompt="",
            output="APPROVE",
            exit_code=0,
            session_id="ses_review",
        )
        reviewer.review_changes.return_value = (review_run, True, "APPROVE")
        orch.reviewers = [reviewer]

        orch._execute_task(task.id)

        orch.client.extract_last_text_block.assert_called_once_with(code_run.output)
        assert reviewer.review_changes.call_args.kwargs["coder_response"] == (
            "final coder summary"
        )
        assert (
            reviewer.review_changes.call_args.kwargs["coder_response"]
            != "full coder transcript with many steps"
        )
