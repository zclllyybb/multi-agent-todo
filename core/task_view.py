"""Task UI serialization helpers extracted from Orchestrator."""

import os
import time
from typing import List

from core.models import Task, TaskStatus


class TaskViewService:
    """Own task-to-UI serialization and git resource decoration."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def collect_resource_snapshot(self) -> tuple[set[str], dict[str, list[str]]]:
        """Collect current git resource existence snapshot."""
        now = time.time()
        if now - self.orchestrator._resource_snapshot_cached_at < 1.0:
            return self.orchestrator._resource_snapshot_cache

        local_branches: set[str] = set()
        branch_worktrees: dict[str, list[str]] = {}

        branch_result = self.orchestrator.worktree_mgr._run_git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads"
        )
        if branch_result.returncode == 0:
            local_branches = {
                line.strip()
                for line in branch_result.stdout.splitlines()
                if line.strip()
            }

        for wt in self.orchestrator.worktree_mgr.list_worktrees():
            raw_branch = wt.get("branch", "")
            if raw_branch.startswith("refs/heads/"):
                raw_branch = raw_branch[len("refs/heads/") :]
            raw_path = wt.get("path", "")
            if raw_branch and raw_path:
                branch_worktrees.setdefault(raw_branch, []).append(
                    os.path.abspath(raw_path)
                )

        self.orchestrator._resource_snapshot_cache = (local_branches, branch_worktrees)
        self.orchestrator._resource_snapshot_cached_at = now
        return self.orchestrator._resource_snapshot_cache

    @staticmethod
    def task_resource_state(
        task: Task,
        local_branches: set[str],
        branch_worktrees: dict[str, list[str]],
    ) -> dict:
        """Compute actual git-resource existence used by UI clean visibility."""
        actual_branch_exists = bool(
            task.branch_name and task.branch_name in local_branches
        )
        recorded_worktree_exists = bool(
            task.worktree_path and os.path.isdir(task.worktree_path)
        )
        branch_worktree_exists = False
        if task.branch_name:
            for path in branch_worktrees.get(task.branch_name, []):
                if os.path.isdir(path):
                    branch_worktree_exists = True
                    break

        actual_worktree_exists = recorded_worktree_exists or branch_worktree_exists
        clean_available = TaskStatus.is_cleanable(task.status) and (
            actual_branch_exists or actual_worktree_exists
        )
        can_publish = (
            TaskStatus.is_publishable(task.status)
            and bool(task.branch_name)
            and task.task_mode not in {"review", "jira"}
        )
        can_assign_jira = task.task_mode != "jira"
        can_cancel = not TaskStatus.is_cancel_terminal(task.status)
        can_resume = (
            TaskStatus.is_resumable(task.status)
            and task.task_mode == "develop"
            and bool(task.worktree_path)
        )
        can_revise = TaskStatus.is_revisable(task.status) and bool(task.worktree_path)
        can_arbitrate = TaskStatus.is_awaiting_arbitration(task.status)
        dependency_satisfied = TaskStatus.is_dependency_satisfied(task.status)

        return {
            "actual_branch_exists": actual_branch_exists,
            "actual_worktree_exists": actual_worktree_exists,
            "clean_available": clean_available,
            "can_publish": can_publish,
            "can_assign_jira": can_assign_jira,
            "can_cancel": can_cancel,
            "can_resume": can_resume,
            "can_revise": can_revise,
            "can_arbitrate": can_arbitrate,
            "dependency_satisfied": dependency_satisfied,
        }

    def serialize_tasks_for_ui(self, tasks: List[Task]) -> List[dict]:
        """Serialize tasks with runtime resource-state fields for dashboard UI."""
        local_branches, branch_worktrees = (
            self.orchestrator._collect_resource_snapshot()
        )
        result = []
        for task in tasks:
            task_dict = task.to_dict()
            task_dict["comment_count"] = len(task.comments)
            task_dict["has_comments"] = bool(task.comments)
            task_dict.update(
                self.orchestrator._task_resource_state(
                    task, local_branches, branch_worktrees
                )
            )
            result.append(task_dict)
        return result

    def serialize_task_for_ui(self, task: Task) -> dict:
        """Serialize a single task with runtime resource-state fields for dashboard UI."""
        return self.serialize_tasks_for_ui([task])[0]
