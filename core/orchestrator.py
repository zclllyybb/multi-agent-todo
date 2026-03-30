"""Orchestrator: dispatches tasks to agents, manages lifecycle and parallelism."""

import json
import logging
import os
import random
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, List, Optional, Set

from agents.coder import CoderAgent
from agents.explorer import ExplorerAgent
from agents.planner import PlannerAgent
from agents.reviewer import ReviewerAgent
from core.database import Database
from core.dep_tracker import DependencyTracker
from core.models import (
    AgentRun, ExploreModule, ExploreRun, ExploreStatus, ModelOutputError,
    Task, TaskPriority, TaskSource, TaskStatus, TodoItem, TodoItemStatus,
)
from core.opencode_client import OpenCodeClient
from core.worktree import WorktreeManager

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: dict):
        self.config = config
        self.running = False
        self._lock = threading.Lock()
        self._futures: Dict[str, Future] = {}

        # Core components
        self.db = Database(config["database"]["path"])
        self.worktree_mgr = WorktreeManager(
            repo_path=config["repo"]["path"],
            worktree_dir=config["repo"]["worktree_dir"],
            base_branch=config["repo"]["base_branch"],
            hook_env=config.get("hook_env", {}),
        )
        self.client = OpenCodeClient(timeout=config["opencode"]["timeout"])

        # Agents
        self.planner = PlannerAgent(
            model=config["opencode"]["planner_model"], client=self.client
        )
        # Coder: one agent per complexity level, keyed by complexity string
        oc = config["opencode"]
        default_coder_model = oc.get("coder_model_default", oc.get("coder_model", ""))
        complexity_map: dict = oc.get("coder_model_by_complexity", {})
        self._coder_by_complexity: Dict[str, CoderAgent] = {}
        for level, model in complexity_map.items():
            self._coder_by_complexity[level] = CoderAgent(model=model, client=self.client)
        self._default_coder = CoderAgent(model=default_coder_model, client=self.client)

        # Reviewers: one agent per configured model; all must approve
        reviewer_models: List[str] = oc.get(
            "reviewer_models",
            [oc.get("reviewer_model", "")],
        )
        self.reviewers: List[ReviewerAgent] = [
            ReviewerAgent(model=m, client=self.client) for m in reviewer_models if m
        ]

        # Thread pool for parallel execution
        max_parallel = config["orchestrator"]["max_parallel_tasks"]
        self._pool = ThreadPoolExecutor(max_workers=max_parallel)
        log.info(
            "Orchestrator initialized: max_parallel=%d, repo=%s",
            max_parallel, config["repo"]["path"],
        )

        # Dependency tracking between sub-tasks (pure in-memory, rebuilt on split)
        self.dep_tracker = DependencyTracker()
        self._rebuild_dep_tracker()

        # Cache for UI resource snapshot (git branches/worktrees) to keep
        # dashboard auto-refresh lightweight.
        self._resource_snapshot_cache = (set(), {})
        self._resource_snapshot_cached_at = 0.0

        # Recovery: reset any TodoItems stuck in ANALYZING from a previous crash.
        # (from_dict already converts ANALYZING → PENDING_ANALYSIS on load, but items
        #  in the DB still have status=analyzing until we overwrite them.)
        self._recover_stuck_analyzing()

    def _recover_stuck_analyzing(self):
        """Reset any TodoItems whose status is 'analyzing' (left by a previous crash)."""
        items = self.db.get_all_todo_items()
        recovered = 0
        for item in items:
            if item.status == TodoItemStatus.ANALYZING:
                item.status = TodoItemStatus.PENDING_ANALYSIS
                item.updated_at = time.time()
                self.db.save_todo_item(item)
                recovered += 1
        if recovered:
            log.warning(
                "Recovered %d TODO item(s) stuck in ANALYZING state (server restart)",
                recovered,
            )

    def _rebuild_dep_tracker(self):
        """Reconstruct in-memory dependency graph from persisted tasks.

        On daemon restart the DependencyTracker is empty.  Walk all tasks
        that have a ``parent_id``, group them by parent, and re-register
        them so that ``_update_parent_status`` / ``on_completed`` work
        correctly for existing task trees.

        After registration, replay ``on_completed`` for children that are
        already in a terminal state so their dependents get unblocked.
        """
        all_tasks = self.db.get_all_tasks()
        # Group children by parent_id
        parent_children: Dict[str, List[Task]] = {}
        for t in all_tasks:
            if t.parent_id:
                parent_children.setdefault(t.parent_id, []).append(t)
        registered = 0
        for parent_id, children in parent_children.items():
            self.dep_tracker.register(parent_id, children)
            registered += len(children)

        # Replay completions for tasks already in terminal states so that
        # their dependents' pending-dep sets are updated correctly.
        # Collect newly unblocked pending tasks for auto-dispatch on start().
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        self._pending_dispatch: List[str] = []
        for t in all_tasks:
            if t.parent_id and t.status in terminal:
                for uid in self.dep_tracker.on_completed(t.id):
                    unblocked_task = self.db.get_task(uid)
                    if unblocked_task and unblocked_task.status == TaskStatus.PENDING:
                        self._pending_dispatch.append(uid)

        if registered:
            log.info(
                "Rebuilt dep_tracker from DB: %d child task(s) across %d parent(s), "
                "%d task(s) ready to dispatch",
                registered, len(parent_children), len(self._pending_dispatch),
            )

    def _collect_resource_snapshot(self) -> tuple[set[str], dict[str, list[str]]]:
        """Collect current git resource existence snapshot.

        Returns:
            - local_branches: set of local branch names
            - branch_worktrees: branch -> list of worktree paths from `git worktree list`
        """
        now = time.time()
        if now - self._resource_snapshot_cached_at < 1.0:
            return self._resource_snapshot_cache

        local_branches: set[str] = set()
        branch_worktrees: dict[str, list[str]] = {}

        branch_result = self.worktree_mgr._run_git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads"
        )
        if branch_result.returncode == 0:
            local_branches = {
                line.strip() for line in branch_result.stdout.splitlines() if line.strip()
            }

        for wt in self.worktree_mgr.list_worktrees():
            raw_branch = wt.get("branch", "")
            if raw_branch.startswith("refs/heads/"):
                raw_branch = raw_branch[len("refs/heads/"):]
            raw_path = wt.get("path", "")
            if raw_branch and raw_path:
                branch_worktrees.setdefault(raw_branch, []).append(os.path.abspath(raw_path))

        self._resource_snapshot_cache = (local_branches, branch_worktrees)
        self._resource_snapshot_cached_at = now
        return self._resource_snapshot_cache

    @staticmethod
    def _task_resource_state(
        task: Task,
        local_branches: set[str],
        branch_worktrees: dict[str, list[str]],
    ) -> dict:
        """Compute actual git-resource existence used by UI clean visibility."""
        cleanable_statuses = {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.REVIEW_FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.NEEDS_ARBITRATION,
        }

        actual_branch_exists = bool(task.branch_name and task.branch_name in local_branches)
        recorded_worktree_exists = bool(task.worktree_path and os.path.isdir(task.worktree_path))
        branch_worktree_exists = False
        if task.branch_name:
            for path in branch_worktrees.get(task.branch_name, []):
                if os.path.isdir(path):
                    branch_worktree_exists = True
                    break

        actual_worktree_exists = recorded_worktree_exists or branch_worktree_exists
        clean_available = (
            task.status in cleanable_statuses
            and (actual_branch_exists or actual_worktree_exists)
        )

        return {
            "actual_branch_exists": actual_branch_exists,
            "actual_worktree_exists": actual_worktree_exists,
            "clean_available": clean_available,
        }

    def serialize_tasks_for_ui(self, tasks: List[Task]) -> List[dict]:
        """Serialize tasks with runtime resource-state fields for dashboard UI."""
        local_branches, branch_worktrees = self._collect_resource_snapshot()
        result = []
        for task in tasks:
            td = task.to_dict()
            td.update(self._task_resource_state(task, local_branches, branch_worktrees))
            result.append(td)
        return result

    def serialize_task_for_ui(self, task: Task) -> dict:
        """Serialize a single task with runtime resource-state fields for dashboard UI."""
        return self.serialize_tasks_for_ui([task])[0]

    # ── Branch Name Generation ──────────────────────────────────────

    def _generate_branch_slug(self, title: str, task_id: str) -> str:
        """Ask the cheapest/simplest model to produce a short git-safe slug from title.

        Falls back to the task_id-only style on any error.
        """
        short_id = task_id[:8]
        try:
            # Use the 'simple' coder model (cheapest configured) or the default
            simple_agent = (
                self._coder_by_complexity.get("simple")
                or self._default_coder
            )
            prompt = (
                f"Convert the following task title into a concise git branch name slug "
                f"(lowercase, hyphens only, max 5 words, no special chars, no prefix):\n"
                f"{title}\n\n"
                f"Reply with ONLY the slug, nothing else."
            )
            repo_path = self.config["repo"]["path"]
            agent_run = self.client.run(
                message=prompt,
                work_dir=repo_path,
                model=simple_agent.model,
                agent_type="slug",
                max_continues=0,
            )
            text = self.client.extract_text_response(agent_run.output).strip().lower()
            slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
            slug = re.sub(r"-+", "-", slug)
            slug = slug[:50]  # hard cap
            if slug:
                return f"agent/task-{short_id}-{slug}"
        except Exception as e:
            log.warning("Branch slug generation failed for [%s]: %s", task_id, e)
        return f"agent/task-{short_id}"

    # ── Runtime Configuration ─────────────────────────────────────────

    def update_models(self, updates: dict):
        """Update agent models at runtime without restarting.

        Accepted keys (all optional):
          planner_model: str
          coder_model_default: str
          coder_model_by_complexity: dict  (level -> model)
          reviewer_models: list[str]
        """
        oc = self.config.setdefault("opencode", {})

        if "planner_model" in updates and updates["planner_model"]:
            model = updates["planner_model"].strip()
            self.planner = PlannerAgent(model=model, client=self.client)
            oc["planner_model"] = model
            log.info("Updated planner model: %s", model)

        if "coder_model_default" in updates and updates["coder_model_default"]:
            model = updates["coder_model_default"].strip()
            self._default_coder = CoderAgent(model=model, client=self.client)
            oc["coder_model_default"] = model
            log.info("Updated default coder model: %s", model)

        if "coder_model_by_complexity" in updates:
            cmap = updates["coder_model_by_complexity"]
            if isinstance(cmap, dict):
                new_map = {}
                for level, model in cmap.items():
                    m = model.strip() if isinstance(model, str) else ""
                    if m:
                        new_map[level] = m
                        self._coder_by_complexity[level] = CoderAgent(
                            model=m, client=self.client
                        )
                oc["coder_model_by_complexity"] = new_map
                log.info("Updated coder complexity map: %s", new_map)

        if "reviewer_models" in updates:
            models = updates["reviewer_models"]
            if isinstance(models, list):
                cleaned = [m.strip() for m in models if isinstance(m, str) and m.strip()]
                self.reviewers = [
                    ReviewerAgent(model=m, client=self.client) for m in cleaned
                ]
                oc["reviewer_models"] = cleaned
                log.info("Updated reviewer models: %s", cleaned)

        # Persist the opencode section back to config.yaml so changes survive restarts
        self._save_opencode_config()

    def _save_opencode_config(self):
        """Write model config changes back to config.yaml preserving all comments/formatting.

        Strategy: parse the file line-by-line and replace only the scalar values
        for the four model keys, leaving every comment, blank line, and other
        key intact.  Multi-line structures (coder_model_by_complexity,
        reviewer_models) are replaced block-by-block.
        """
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.yaml",
        )
        try:
            with open(config_path) as f:
                lines = f.readlines()

            oc = self.config["opencode"]
            new_lines = self._patch_yaml_lines(lines, oc)

            with open(config_path, "w") as f:
                f.writelines(new_lines)
            log.info("Persisted opencode model config to %s", config_path)
        except Exception as e:
            log.warning("Could not persist model config to %s: %s", config_path, e)

    @staticmethod
    def _patch_yaml_lines(lines: list, oc: dict) -> list:
        """Return a copy of lines with opencode model values patched in-place."""
        import re as _re

        result = list(lines)
        i = 0
        while i < len(result):
            line = result[i]
            stripped = line.rstrip()

            # ── planner_model ──────────────────────────────────────────
            m = _re.match(r'^(\s*planner_model\s*:\s*)(.*)$', stripped)
            if m and "planner_model" in oc:
                result[i] = m.group(1) + oc["planner_model"] + "\n"
                i += 1
                continue

            # ── coder_model_default ────────────────────────────────────
            m = _re.match(r'^(\s*coder_model_default\s*:\s*)(.*)$', stripped)
            if m and "coder_model_default" in oc:
                result[i] = m.group(1) + oc["coder_model_default"] + "\n"
                i += 1
                continue

            # ── coder_model_by_complexity (block) ──────────────────────
            m = _re.match(r'^(\s*coder_model_by_complexity\s*:)', stripped)
            if m and "coder_model_by_complexity" in oc:
                indent = len(line) - len(line.lstrip())
                # collect block: next lines with greater indentation
                block_end = i + 1
                while block_end < len(result):
                    nxt = result[block_end]
                    if nxt.strip() == "" or nxt.strip().startswith("#"):
                        block_end += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip())
                    if nxt_indent <= indent:
                        break
                    block_end += 1
                # rebuild block preserving original per-level comments
                cmap = oc["coder_model_by_complexity"]
                new_block = [result[i]]  # keep the "coder_model_by_complexity:" line
                child_indent = " " * (indent + 4)
                # update existing level lines, keep comments/blanks
                for j in range(i + 1, block_end):
                    orig = result[j]
                    cm = _re.match(r'^(\s*)([a-zA-Z_]+)(\s*:\s*)(.*)$', orig.rstrip())
                    if cm and cm.group(2) in cmap:
                        new_block.append(
                            cm.group(1) + cm.group(2) + cm.group(3) + cmap[cm.group(2)] + "\n"
                        )
                    else:
                        new_block.append(orig)
                # add any new levels not present in original file
                existing_levels = set()
                for j in range(i + 1, block_end):
                    cm = _re.match(r'^\s*([a-zA-Z_]+)\s*:', result[j].rstrip())
                    if cm:
                        existing_levels.add(cm.group(1))
                for level, model in cmap.items():
                    if level not in existing_levels:
                        new_block.append(f"{child_indent}{level}: {model}\n")
                result[i:block_end] = new_block
                i += len(new_block)
                continue

            # ── reviewer_models (list block) ───────────────────────────
            m = _re.match(r'^(\s*reviewer_models\s*:)', stripped)
            if m and "reviewer_models" in oc:
                indent = len(line) - len(line.lstrip())
                block_end = i + 1
                while block_end < len(result):
                    nxt = result[block_end]
                    if nxt.strip() == "" or nxt.strip().startswith("#"):
                        block_end += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip())
                    if nxt_indent <= indent:
                        break
                    block_end += 1
                child_indent = " " * (indent + 2)
                new_block = [result[i]]  # keep "reviewer_models:" line
                for model in oc["reviewer_models"]:
                    new_block.append(f"{child_indent}- {model}\n")
                result[i:block_end] = new_block
                i += len(new_block)
                continue

            i += 1

        return result

    # ── Task Management ──────────────────────────────────────────────

    def _get_child_tasks(self, parent_id: str) -> list:
        """Return all tasks whose parent_id matches *parent_id*."""
        return [t for t in self.db.get_all_tasks() if t.parent_id == parent_id]

    def clean_task(self, task_id: str) -> dict:
        """Remove the worktree and branch of a completed/failed task to free resources.

        Unlike cancel_task this does NOT change the task status — it only releases
        the git/filesystem resources.  The task remains visible with its original
        status but worktree_path and branch_name are cleared.
        """
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                                TaskStatus.REVIEW_FAILED, TaskStatus.CANCELLED,
                                TaskStatus.NEEDS_ARBITRATION):
            return {"error": f"Cannot clean task in '{task.status.value}' state — it may still be running"}
        if not task.branch_name and not task.worktree_path:
            return {"error": "Task has no branch/worktree to clean"}
        removed_branch = task.branch_name
        try:
            if task.branch_name:
                self.worktree_mgr.remove_worktree(task.branch_name, worktree_path=task.worktree_path)
                log.info("Cleaned worktree for task [%s]: %s", task_id, task.branch_name)
            else:
                self.worktree_mgr.remove_worktree_path_only(task.worktree_path)
                log.info("Cleaned worktree(path-only) for task [%s]: %s", task_id, task.worktree_path)
        except Exception as e:
            log.error("clean_task: remove_worktree failed for [%s]: %s", task_id, e)
            return {"error": f"Failed to remove worktree: {e}"}
        task.worktree_path = ""
        task.branch_name = ""
        task.updated_at = time.time()
        self.db.save_task(task)
        # Cascade clean to child tasks
        child_errors = []
        for child in self._get_child_tasks(task_id):
            if child.branch_name:
                child_result = self.clean_task(child.id)
                if "error" in child_result:
                    child_errors.append(f"[{child.id[:8]}]: {child_result['error']}")
        if child_errors:
            return {"cleaned": True, "branch": removed_branch,
                    "warnings": f"Parent cleaned but some children failed: {'; '.join(child_errors)}"}
        return {"cleaned": True, "branch": removed_branch}

    def cancel_task(self, task_id: str) -> dict:
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        already_terminal = task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
        if not already_terminal:
            task.status = TaskStatus.CANCELLED
            task.updated_at = time.time()
            self.db.save_task(task)
            # Kill any running opencode process for this task immediately
            self.client.kill_task(task_id)
            # Clean up worktree if exists
            if task.branch_name:
                try:
                    self.worktree_mgr.remove_worktree(task.branch_name, worktree_path=task.worktree_path)
                    log.info("Removed worktree for cancelled task [%s]: %s", task_id, task.branch_name)
                    task.worktree_path = ""
                    task.branch_name = ""
                    task.updated_at = time.time()
                    self.db.save_task(task)
                except Exception as e:
                    log.warning("Failed to remove worktree for %s: %s — user can clean manually", task_id, e)
            # Clean dependency tracking maps
            self.dep_tracker.cleanup(task_id)
            # Revert any TODO item linked to this task back to analyzed
            todos = self.db.get_all_todo_items()
            for item in todos:
                if item.task_id == task_id and item.status == TodoItemStatus.DISPATCHED:
                    item.status = TodoItemStatus.ANALYZED
                    item.task_id = ""
                    item.updated_at = time.time()
                    self.db.save_todo_item(item)
                    log.info("Auto-reverted todo [%s] after cancelling task [%s]", item.id, task_id)
            log.info("Cancelled task: [%s]", task_id)
            self._update_parent_status(task_id)
        # Always cascade cancel to non-terminal child tasks (even if this
        # task is already completed/cancelled — descendants may still be running)
        for child in self._get_child_tasks(task_id):
            if child.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                child_result = self.cancel_task(child.id)
                if "error" in child_result:
                    log.warning("Failed to cascade cancel to child [%s]: %s",
                                child.id, child_result["error"])
        return {"cancelled": True}

    def revise_task(self, task_id: str, feedback: str) -> dict:
        """Re-open a completed/failed task with manual review feedback.

        Resets retry counters, stores the feedback in user_feedback, and
        re-dispatches the task through the appropriate pipeline.  The existing
        worktree and coder session are reused.
        """
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                                TaskStatus.REVIEW_FAILED, TaskStatus.NEEDS_ARBITRATION):
            return {"error": f"Cannot revise task in {task.status.value} state"}
        if not task.worktree_path:
            return {"error": "Task has no worktree (was it split into sub-tasks?)"}

        # Record the manual feedback as an AgentRun so it appears in the runs list
        manual_run = AgentRun(
            task_id=task_id,
            agent_type="manual_review",
            model="user",
            prompt="",
            output=feedback,
            exit_code=0,
            duration_sec=0.0,
        )
        self.db.save_agent_run(manual_run)

        # Store feedback and reset counters
        task.user_feedback = feedback
        task.review_pass = False
        task.retry_count = 0
        task.status = TaskStatus.PENDING
        task.error = ""
        task.completed_at = 0.0
        task.updated_at = time.time()
        self.db.save_task(task)

        # Route to the correct pipeline based on task mode
        if task.task_mode == "review":
            self._dispatch_review_only(task_id)
        else:
            self._dispatch_revise(task_id)
        log.info("Revise task [%s] (mode=%s) with manual feedback (%d chars)",
                 task_id, task.task_mode, len(feedback))
        log.debug("Task [%s] revised with feedback: %s", task_id, feedback)
        return {"ok": True, "task_id": task_id}

    def resolve_arbitration(self, task_id: str, action: str,
                            feedback: str = "") -> dict:
        """Resolve a NEEDS_ARBITRATION task via human decision.

        *action* must be one of:
          - ``"approve"``: accept the coder's current work as-is (force-approve).
          - ``"revise"``:  provide *feedback* and restart the coder→reviewer loop.
          - ``"reject"``:  permanently fail the task.

        Returns a status dict.
        """
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        if task.status != TaskStatus.NEEDS_ARBITRATION:
            return {"error": f"Task is not awaiting arbitration (status={task.status.value})"}

        if action == "approve":
            task.status = TaskStatus.COMPLETED
            task.review_pass = True
            task.completed_at = time.time()
            task.error = ""
            task.updated_at = time.time()
            self.db.save_task(task)
            log.info("Arbitration resolved: force-approved [%s]", task_id)
            self._update_parent_status(task_id)
            return {"ok": True, "action": "approve", "task_id": task_id}

        elif action == "revise":
            if not feedback:
                return {"error": "feedback is required for 'revise' action"}
            return self.revise_task(task_id, feedback)

        elif action == "reject":
            task.status = TaskStatus.FAILED
            task.error = feedback or "Rejected by human arbitration"
            task.updated_at = time.time()
            self.db.save_task(task)
            log.info("Arbitration resolved: rejected [%s]", task_id)
            self._update_parent_status(task_id)
            return {"ok": True, "action": "reject", "task_id": task_id}

        else:
            return {"error": f"Unknown action '{action}'; must be approve/revise/reject"}

    def _dispatch_revise(self, task_id: str) -> bool:
        """Submit a revise-task for execution (skips planning, reuses worktree)."""
        with self._lock:
            if task_id in self._futures:
                log.warning("Task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self._futures) >= max_p:
                log.warning("Max parallel tasks reached (%d)", max_p)
                return False
            future = self._pool.submit(self._revise_task_pipeline, task_id)
            self._futures[task_id] = future
            log.info("Dispatched revise for task: %s", task_id)
            return True

    def _revise_task_pipeline(self, task_id: str):
        """Coder→reviewer loop for a revised task (skips planning + worktree creation)."""
        task = self.db.get_task(task_id)
        if not task:
            log.error("Revise: task not found: %s", task_id)
            return

        try:
            worktree_path = task.worktree_path
            coder = self._coder_by_complexity.get(task.complexity, self._default_coder)
            log.info("Revise [%s] using coder model=%s, worktree=%s",
                     task.id, coder.model, worktree_path)

            # Recover the last coder session id so the coder retains full context
            coder_session_id = ""
            coder_sessions = task.session_ids.get("coder", [])
            if coder_sessions:
                coder_session_id = coder_sessions[-1]

            # Read the user's manual feedback (stored separately from model review output)
            user_feedback = task.user_feedback

            for attempt in range(task.max_retries + 1):
                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Revise [%s] was cancelled, aborting", task_id)
                    return

                task.retry_count = attempt
                task.status = TaskStatus.CODING
                task.updated_at = time.time()
                self.db.save_task(task)

                # Always use retry_with_feedback since coder already has task context.
                # First attempt: send user_feedback; subsequent attempts: send model reviewer output.
                coder_feedback = user_feedback if attempt == 0 else task.review_output
                code_run, code_text = coder.retry_with_feedback(
                    task, worktree_path,
                    review_feedback=coder_feedback,
                    session_id=coder_session_id,
                )
                self.db.save_agent_run(code_run)
                task.code_output = code_text
                if code_run.session_id:
                    coder_session_id = code_run.session_id
                    task.session_ids.setdefault("coder", []).append(code_run.session_id)
                task.updated_at = time.time()
                self.db.save_task(task)

                # Re-check cancellation
                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Revise [%s] cancelled before review", task_id)
                    return

                task.status = TaskStatus.REVIEWING
                task.updated_at = time.time()
                self.db.save_task(task)

                reviewer_results = []
                rejection_outputs = []
                all_passed = True
                for reviewer in self.reviewers:
                    review_run, passed, review_text = reviewer.review_changes(
                        task, worktree_path,
                        revision_context=user_feedback,
                        coder_response=code_text,
                    )
                    self.db.save_agent_run(review_run)
                    reviewer_results.append({
                        "model": reviewer.model,
                        "passed": passed,
                        "output": review_text,
                    })
                    if review_run.session_id:
                        task.session_ids.setdefault("reviewer", []).append(review_run.session_id)
                    log.info("Revise [%s] reviewer(%s) passed=%s",
                             task.id, reviewer.model, passed)
                    if not passed:
                        all_passed = False
                        rejection_outputs.append(
                            f"=== Reviewer: {reviewer.model} | REQUEST_CHANGES ===\n"
                            + review_text
                        )
                        log.info("Revise [%s] short-circuiting after first rejection", task.id)
                        break

                if all_passed:
                    task.review_output = "\n\n".join(
                        f"=== Reviewer: {r['model']} | APPROVE ===\n{r['output']}"
                        for r in reviewer_results
                    )
                else:
                    task.review_output = "\n\n".join(rejection_outputs)

                task.reviewer_results = reviewer_results
                task.review_pass = all_passed
                task.updated_at = time.time()
                self.db.save_task(task)

                if all_passed:
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = time.time()
                    task.updated_at = time.time()
                    self.db.save_task(task)
                    log.info("Revise completed: [%s]", task.id)
                    self._update_parent_status(task.id)
                    break
                else:
                    if attempt < task.max_retries:
                        log.info("Revise [%s] review failed, retrying (%d/%d)",
                                 task.id, attempt + 1, task.max_retries)
                        task.status = TaskStatus.REVIEW_FAILED
                        task.updated_at = time.time()
                        self.db.save_task(task)
                    else:
                        task.status = TaskStatus.NEEDS_ARBITRATION
                        task.error = (
                            f"Revise: review failed after {task.max_retries + 1} "
                            f"attempts — needs human arbitration"
                        )
                        task.updated_at = time.time()
                        self.db.save_task(task)
                        log.warning(
                            "Revise [%s] needs arbitration: review failed %d times",
                            task.id, task.max_retries + 1,
                        )

        except Exception as e:
            log.error("Revise failed [%s]: %s\n%s", task_id, e, traceback.format_exc())
            task = self.db.get_task(task_id)
            if task:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                task.updated_at = time.time()
                self.db.save_task(task)
                self._update_parent_status(task_id)
        finally:
            with self._lock:
                self._futures.pop(task_id, None)

    def get_status(self) -> dict:
        """Get overall system status."""
        tasks = self.db.get_all_tasks()
        status_counts = {}
        for t in tasks:
            s = t.status.value
            status_counts[s] = status_counts.get(s, 0) + 1
        active = [t.to_dict() for t in tasks
                  if t.status in (TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.REVIEWING)]
        return {
            "running": self.running,
            "total_tasks": len(tasks),
            "status_counts": status_counts,
            "active_tasks": active,
            "active_futures": len(self._futures),
        }

    def submit_task(self, title: str, description: str,
                    priority: str = "medium",
                    file_path: str = "", line_number: int = 0,
                    parent_id: str = "",
                    copy_files: Optional[list] = None) -> Task:
        """Create a pending task that the planner will analyze+split during execution."""
        max_retries = int(self.config.get("orchestrator", {}).get("max_retries", 4))
        task = Task(
            title=title,
            description=description,
            priority=TaskPriority(priority),
            source=TaskSource.MANUAL,
            file_path=file_path,
            line_number=line_number,
            parent_id=parent_id or None,
            max_retries=max_retries,
            copy_files=copy_files or [],
        )
        self.db.save_task(task)
        log.info("Submitted task: [%s] %s", task.id, task.title)
        self.dispatch_task(task.id)
        return task

    def submit_review_task(self, title: str, review_input: str,
                           priority: str = "medium",
                           copy_files: Optional[list] = None) -> Task:
        """Create a review-only task: runs reviewers on user-supplied material."""
        task = Task(
            title=title,
            description="Review-only task",
            priority=TaskPriority(priority),
            source=TaskSource.MANUAL,
            task_mode="review",
            review_input=review_input,
            max_retries=0,
            copy_files=copy_files or [],
        )
        self.db.save_task(task)
        log.info("Submitted review task: [%s] %s", task.id, task.title)
        self._dispatch_review_only(task.id)
        return task

    def _dispatch_review_only(self, task_id: str) -> bool:
        """Submit a review-only task for execution."""
        with self._lock:
            if task_id in self._futures:
                log.warning("Task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self._futures) >= max_p:
                log.warning("Max parallel tasks reached (%d)", max_p)
                return False
            future = self._pool.submit(self._review_only_pipeline, task_id)
            self._futures[task_id] = future
            log.info("Dispatched review-only task: %s", task_id)
            return True

    def _review_only_pipeline(self, task_id: str):
        """Run reviewers on a user-supplied patch/link (no planner, no coder)."""
        task = self.db.get_task(task_id)
        if not task:
            log.error("Review-only: task not found: %s", task_id)
            return

        try:
            task.status = TaskStatus.REVIEWING
            if not task.started_at:
                task.started_at = time.time()
            task.updated_at = time.time()
            self.db.save_task(task)

            # Create a worktree if not already present (revise reuses existing)
            if not task.worktree_path:
                slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:40]
                branch_name = f"agent/review-{task.id[:8]}-{slug}" if slug else f"agent/review-{task.id[:8]}"
                hooks = self.config.get("repo", {}).get("worktree_hooks", [])
                worktree_path = self.worktree_mgr.create_worktree(branch_name, hooks=hooks)
                task.branch_name = branch_name
                task.worktree_path = worktree_path
                task.updated_at = time.time()
                self.db.save_task(task)

                # Copy user-specified files from main workspace into worktree
                if task.copy_files:
                    self.worktree_mgr.copy_files_into(worktree_path, task.copy_files)

            worktree_path = task.worktree_path

            # If this is a revise, user_feedback holds the user's manual guidance
            revision_context = task.user_feedback

            # Run all reviewers
            reviewer_results = []
            for reviewer in self.reviewers:
                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Review-only [%s] cancelled, aborting", task_id)
                    return

                review_run, passed, review_text = reviewer.review_patch(
                    task, worktree_path,
                    revision_context=revision_context,
                )
                self.db.save_agent_run(review_run)
                reviewer_results.append({
                    "model": reviewer.model,
                    "passed": passed,
                    "output": review_text,
                })
                if review_run.session_id:
                    task.session_ids.setdefault("reviewer", []).append(review_run.session_id)
                log.info("Review-only [%s] reviewer(%s) passed=%s",
                         task.id, reviewer.model, passed)

            all_passed = all(r["passed"] for r in reviewer_results)
            task.reviewer_results = reviewer_results
            task.review_pass = all_passed
            task.review_output = "\n\n".join(
                f"=== Reviewer: {r['model']} | {'APPROVE' if r['passed'] else 'REQUEST_CHANGES'} ===\n{r['output']}"
                for r in reviewer_results
            )
            task.status = TaskStatus.COMPLETED
            task.completed_at = time.time()
            task.updated_at = time.time()
            self.db.save_task(task)
            log.info("Review-only task completed: [%s]", task.id)

            # Remove the worktree immediately — review tasks don't need it after completion
            self._cleanup_review_worktree(task)

        except Exception as e:
            log.error("Review-only failed [%s]: %s\n%s", task_id, e, traceback.format_exc())
            task = self.db.get_task(task_id)
            if task:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                task.updated_at = time.time()
                self.db.save_task(task)
                self._cleanup_review_worktree(task)
        finally:
            with self._lock:
                self._futures.pop(task_id, None)

    def _cleanup_review_worktree(self, task):
        """Remove the worktree and branch for a review-only task, then clear the path on the task."""
        if not task.branch_name:
            return
        try:
            self.worktree_mgr.remove_worktree(task.branch_name)
            log.info("Removed review worktree for task [%s]: %s", task.id, task.branch_name)
            task.branch_name = ""
        except Exception as e:
            log.warning("Could not remove review worktree [%s]: %s", task.id, e)
        task.worktree_path = ""
        task.updated_at = time.time()
        self.db.save_task(task)

    # ── TODO Scanning & Analysis ─────────────────────────────────────

    def scan_todos_raw(self, subdir: str = "", limit: int = 0) -> list:
        """Scan the repo (or a subdir) for TODO comments and store them as TodoItems.

        Args:
            subdir: relative subdirectory within the repo to restrict the scan.
            limit:  maximum number of new TODO items to store (0 = no limit).

        Returns list of new TodoItem dicts (skips duplicates by file+line).
        """
        repo_path = self.config["repo"]["path"]
        raw = self.planner.scan_todos(repo_path, subdir=subdir, limit=limit)

        # Build a set of (file_path, line_number) already in DB to avoid duplicates
        existing = self.db.get_all_todo_items()
        existing_keys = {(t.file_path, t.line_number) for t in existing}

        import re as _re
        new_items = []
        for item in raw:
            # Skip malformed grep output (empty path or line=0)
            if not item["file"].strip() or item["line"] == 0:
                log.warning(
                    "scan_todos_raw: skipping malformed grep result file=%r line=%d",
                    item["file"], item["line"],
                )
                continue
            key = (item["file"], item["line"])
            if key in existing_keys:
                continue
            text = item["text"]
            desc = _re.sub(r"^.*?(TODO|FIXME|HACK|XXX)\s*:?\s*", "", text)
            if len(desc) < 5:
                continue
            todo = TodoItem(
                file_path=item["file"],
                line_number=item["line"],
                raw_text=text,
                description=desc,
                status=TodoItemStatus.PENDING_ANALYSIS,
            )
            self.db.save_todo_item(todo)
            new_items.append(todo)
            existing_keys.add(key)

        log.info("Scanned %d new TODO items (limit=%d)", len(new_items), limit)
        return [t.to_dict() for t in new_items]

    def analyze_todo_item(self, todo_id: str) -> dict:
        """Run analyzer on a single TodoItem and update scores in DB.

        Returns item dict on success, or {"error": ..., "status": 409} when
        the item is already being analyzed or does not exist.
        """
        item = self.db.get_todo_item(todo_id)
        if not item:
            log.warning("analyze_todo_item: todo [%s] not found", todo_id)
            return {"error": "not found", "status": 404}

        if item.status == TodoItemStatus.ANALYZING:
            log.warning(
                "analyze_todo_item: todo [%s] is already being analyzed, rejecting duplicate",
                todo_id,
            )
            return {"error": "already_analyzing", "status": 409}

        if item.status == TodoItemStatus.DISPATCHED:
            log.warning(
                "analyze_todo_item: todo [%s] is already dispatched as task [%s]",
                todo_id, item.task_id,
            )
            return {"error": "already_dispatched", "status": 409}

        # Mark as ANALYZING immediately so concurrent requests see the lock
        prev_status = item.status
        item.status = TodoItemStatus.ANALYZING
        item.updated_at = time.time()
        self.db.save_todo_item(item)
        log.info(
            "analyze_todo_item: starting analysis for todo [%s] (prev_status=%s)",
            todo_id, prev_status.value,
        )

        repo_path = self.config["repo"]["path"]
        try:
            run, feasibility, difficulty, note = self._analyze_todo_with_retry(
                item, repo_path,
            )
        except Exception as e:
            log.error(
                "analyze_todo_item: analysis failed for todo [%s]: %s",
                todo_id, traceback.format_exc(),
            )
            item.status = prev_status
            item.updated_at = time.time()
            self.db.save_todo_item(item)
            return {"error": str(e), "status": 500}

        self.db.save_agent_run(run)
        item.feasibility_score = feasibility
        item.difficulty_score = difficulty
        item.analysis_note = note
        item.analyze_output = self.planner.get_text(run)[:4000]  # cap for storage
        item.status = TodoItemStatus.ANALYZED
        item.updated_at = time.time()
        self.db.save_todo_item(item)
        log.info(
            "analyze_todo_item: completed todo [%s] feasibility=%.1f difficulty=%.1f note=%r",
            todo_id, feasibility, difficulty, note[:80],
        )
        return item.to_dict()

    def dispatch_todos_to_planner(self, todo_ids: list) -> list:
        """Create pending tasks from selected TodoItems and mark them dispatched."""
        repo_path = self.config["repo"]["path"]
        created = []
        for tid in todo_ids:
            item = self.db.get_todo_item(tid)
            if not item or item.status == TodoItemStatus.DISPATCHED:
                continue
            # Use path relative to repo root so agents inside the worktree
            # never see absolute paths that fall outside their sandbox.
            rel_path = os.path.relpath(item.file_path, repo_path)
            task = self.submit_task(
                title=f"TODO: {item.description[:80]}",
                description=(
                    f"Resolve TODO at {rel_path}:{item.line_number}\n\n"
                    f"Original comment: {item.raw_text}"
                ),
                file_path=rel_path,
                line_number=item.line_number,
            )
            item.status = TodoItemStatus.DISPATCHED
            item.task_id = task.id
            item.updated_at = time.time()
            self.db.save_todo_item(item)
            created.append(task.to_dict())
            # submit_task already calls dispatch_task; no extra call needed
        log.info("Dispatched %d TODO items to planner", len(created))
        return created

    def publish_task(self, task_id: str) -> dict:
        """Push a completed task's branch to the configured remote."""
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "not found"}
        if task.status != TaskStatus.COMPLETED:
            return {"error": f"task is not completed (status={task.status.value})"}
        if not task.branch_name:
            return {"error": "task has no branch (was it split into sub-tasks?)"}
        remote = self.config.get("publish", {}).get("remote", "origin")
        ok, msg = self.worktree_mgr.publish_branch(task.branch_name, remote)
        if ok:
            task.published_at = time.time()
            task.updated_at = time.time()
            self.db.save_task(task)
            log.info("Published task [%s] branch %s to %s", task_id, task.branch_name, remote)
        return {"success": ok, "message": msg, "branch": task.branch_name, "remote": remote}

    def revert_todo_items(self, todo_ids: list) -> int:
        """Revert dispatched TodoItems back to ANALYZED status.
        Useful when the associated task failed and the user wants to re-dispatch.
        """
        count = 0
        for tid in todo_ids:
            item = self.db.get_todo_item(tid)
            if not item or item.status != TodoItemStatus.DISPATCHED:
                continue
            item.status = TodoItemStatus.ANALYZED
            item.task_id = ""
            item.updated_at = time.time()
            self.db.save_todo_item(item)
            count += 1
            log.info("Reverted todo [%s] from dispatched to analyzed", tid)
        log.info("Reverted %d TODO item(s) to analyzed", count)
        return count

    def delete_todo_items(self, todo_ids: list) -> int:
        """Hard-delete TodoItems by id."""
        count = 0
        for tid in todo_ids:
            item = self.db.get_todo_item(tid)
            if item:
                self.db.delete_todo_item(tid)
                count += 1
        return count

    # ── Task Execution Pipeline ──────────────────────────────────────

    def _plan_with_retry(self, task: Task, repo_path: str):
        """Call planner.analyze_and_split, retrying once on ModelOutputError.

        Returns the same tuple as analyze_and_split:
            (plan_run, is_split, plan_text, sub_tasks, complexity)

        On the first ModelOutputError the model is called again.  If the
        second attempt also fails, the error propagates and the outer
        handler marks the task FAILED.
        """
        try:
            return self.planner.analyze_and_split(
                title=task.title,
                description=task.description,
                repo_path=repo_path,
                task_id=task.id,
            )
        except ModelOutputError as first_err:
            log.warning(
                "Task [%s] planner output unparseable, retrying once: %s",
                task.id, first_err,
            )
            try:
                return self.planner.analyze_and_split(
                    title=task.title,
                    description=task.description,
                    repo_path=repo_path,
                    task_id=task.id,
                )
            except ModelOutputError as second_err:
                raise ModelOutputError(
                    f"Task [{task.id}] planner failed after retry: {second_err}"
                ) from second_err

    def _analyze_todo_with_retry(self, item, repo_path: str):
        """Call planner.analyze_todo, retrying once on ModelOutputError.

        Returns the same tuple as analyze_todo:
            (agent_run, feasibility, difficulty, note)
        """
        try:
            return self.planner.analyze_todo(item, repo_path)
        except ModelOutputError as first_err:
            log.warning(
                "Todo [%s] analyzer output unparseable, retrying once: %s",
                item.id, first_err,
            )
            try:
                return self.planner.analyze_todo(item, repo_path)
            except ModelOutputError as second_err:
                raise ModelOutputError(
                    f"Todo [{item.id}] analyzer failed after retry: {second_err}"
                ) from second_err

    def _execute_task(self, task_id: str):
        """Full pipeline: plan → code → review (with retry)."""
        task = self.db.get_task(task_id)
        if not task:
            log.error("Task not found: %s", task_id)
            return

        repo_path = self.config["repo"]["path"]
        try:
            # ── Phase 1: Planning (analyze + optionally split) ──
            task.status = TaskStatus.PLANNING
            task.started_at = time.time()
            task.updated_at = time.time()
            self.db.save_task(task)

            plan_run, is_split, plan_text, sub_tasks, complexity = \
                self._plan_with_retry(task, repo_path)
            self.db.save_agent_run(plan_run)
            task.complexity = complexity
            if plan_run.session_id:
                task.session_ids.setdefault("planner", []).append(plan_run.session_id)
                log.info("Task [%s] planner session: %s (complexity=%s)",
                         task.id, plan_run.session_id, complexity)

            if is_split and task.source == TaskSource.PLANNER:
                # Sub-tasks created by the planner must not be split further —
                # force single-task execution to avoid unbounded recursion.
                log.info(
                    "Task [%s] is a planner sub-task; ignoring split=true from planner "
                    "(would create recursive split). Treating as single task.",
                    task.id,
                )
                is_split = False
                plan_text = sub_tasks[0].get("description", plan_text) if sub_tasks else plan_text

            if is_split:
                # Planner decided to decompose — create sub-tasks and mark parent done
                log.info("Task [%s] split into %d sub-tasks", task.id, len(sub_tasks))
                task.plan_output = (
                    f"Split into {len(sub_tasks)} sub-tasks:\n"
                    + "\n".join(f"- {st.get('title','')}" for st in sub_tasks)
                )
                # Pass 1: create all child Task objects (IDs assigned at creation)
                children: List[Task] = []
                for st in sub_tasks:
                    child = Task(
                        title=st.get("title", "Sub-task"),
                        description=st.get("description", ""),
                        priority=TaskPriority(st.get("priority", "medium")),
                        source=TaskSource.PLANNER,
                        parent_id=task.id,
                        max_retries=int(self.config.get("orchestrator", {}).get("max_retries", 4)),
                    )
                    children.append(child)
                # Pass 2: resolve depends_on indices → real IDs, persist
                # (raises ModelOutputError on invalid entries; already retried above)
                child_id_list = [c.id for c in children]
                resolved_deps = self.dep_tracker.resolve_indices(child_id_list, sub_tasks)
                for child, resolved in zip(children, resolved_deps):
                    child.depends_on = resolved
                    self.db.save_task(child)
                    log.info(
                        "Created sub-task [%s] '%s' depends_on=%s",
                        child.id, child.title, resolved,
                    )
                self.dep_tracker.register(task.id, children)
                # Pass 3: dispatch unblocked sub-tasks
                for child in children:
                    if not self.dep_tracker.is_blocked(child.id):
                        self.dispatch_task(child.id)
                    else:
                        log.info(
                            "Sub-task [%s] '%s' blocked by deps=%s, waiting",
                            child.id, child.title, child.depends_on,
                        )
                task.status = TaskStatus.PLANNING  # will be updated when sub-tasks finish
                task.updated_at = time.time()
                self.db.save_task(task)
                log.info("Task [%s] split into sub-tasks, waiting for children", task.id)
                return

            task.plan_output = plan_text
            task.updated_at = time.time()
            self.db.save_task(task)

            # ── Phase 2: Create worktree (then run configured hooks) ──
            branch_name = self._generate_branch_slug(task.title, task.id)
            hooks = self.config.get("repo", {}).get("worktree_hooks", [])
            worktree_path = self.worktree_mgr.create_worktree(branch_name, hooks=hooks)
            task.branch_name = branch_name
            task.worktree_path = worktree_path
            task.updated_at = time.time()
            self.db.save_task(task)

            # Copy user-specified files from main workspace into worktree
            if task.copy_files:
                self.worktree_mgr.copy_files_into(worktree_path, task.copy_files)

            # ── Phase 2b: Merge dependency branches ──
            dep_context = ""
            if task.depends_on:
                dep_branches = []
                for dep_id in task.depends_on:
                    dep_task = self.db.get_task(dep_id)
                    if dep_task and dep_task.branch_name:
                        dep_branches.append(dep_task.branch_name)
                if dep_branches:
                    merge_summaries = self.worktree_mgr.merge_dependency_branches(
                        worktree_path, dep_branches,
                    )
                    if merge_summaries:
                        dep_context = (
                            "## Dependency Commits (already merged into your worktree)\n"
                            "The following commits from prerequisite tasks have been "
                            "cherry-picked into your working tree. Your code should "
                            "build on top of these changes.\n\n"
                            + "\n\n".join(merge_summaries)
                        )
                        log.info(
                            "Task [%s] merged %d dep branch(es): %s",
                            task.id, len(dep_branches), dep_branches,
                        )

            # Select coder model based on complexity assessed by planner
            coder = self._coder_by_complexity.get(task.complexity, self._default_coder)
            log.info("Task [%s] using coder model=%s (complexity=%s)",
                     task.id, coder.model, task.complexity)

            # ── Phase 3: Code → Review loop ──
            # Tracks the coder's opencode session so retries continue in the
            # same session (full context retention via --session <id>).
            coder_session_id = ""
            # Accumulates rejection feedback from all previous rounds so each
            # reviewer in the next round can see what was already raised.
            all_prior_rejections: list[str] = []

            for attempt in range(task.max_retries + 1):
                # Re-read task to detect external cancellation before each attempt
                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Task [%s] was cancelled, aborting loop", task_id)
                    return

                task.retry_count = attempt
                task.status = TaskStatus.CODING
                task.updated_at = time.time()
                self.db.save_task(task)

                if attempt == 0 or not coder_session_id:
                    # First attempt or no session: send the full prompt
                    code_run, code_text = coder.implement_task(
                        task, worktree_path, session_id=coder_session_id,
                        dep_context=dep_context,
                    )
                else:
                    # Retry in continued session: send only review feedback
                    code_run, code_text = coder.retry_with_feedback(
                        task, worktree_path,
                        review_feedback=task.review_output,
                        session_id=coder_session_id,
                    )
                self.db.save_agent_run(code_run)
                task.code_output = code_text
                if code_run.session_id:
                    # Keep the same session id for all retry rounds
                    coder_session_id = code_run.session_id
                    task.session_ids.setdefault("coder", []).append(code_run.session_id)
                    log.info("Task [%s] coder session: %s (attempt %d)",
                             task.id, code_run.session_id, attempt + 1)
                task.updated_at = time.time()
                self.db.save_task(task)

                # Validate coder output: the last step must end with
                # finish_reason='stop'.  A missing stop means the model
                # output was truncated or the process died mid-step.
                if not self.client.is_output_complete(code_run.output):
                    raise RuntimeError(
                        f"Coder output is incomplete — the last step has no "
                        f"'stop' finish reason (session={code_run.session_id}, "
                        f"attempt={attempt + 1}).  The model may have been "
                        f"truncated or crashed mid-step."
                    )

                # Re-check cancellation before starting review
                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Task [%s] was cancelled before review, aborting", task_id)
                    return

                # Extract only the coder's final summary (last text block
                # before stop) — not the entire session transcript.
                coder_last_response = (
                    self.client.extract_last_text_block(code_run.output)
                    if attempt > 0 else ""
                )

                # ── Multi-Reviewer: short-circuit on first REQUEST_CHANGES ──
                task.status = TaskStatus.REVIEWING
                task.updated_at = time.time()
                self.db.save_task(task)

                reviewer_results = []
                rejection_outputs = []   # only from reviewers that rejected
                all_passed = True
                for reviewer in self.reviewers:
                    review_run, passed, review_text = reviewer.review_changes(
                        task, worktree_path,
                        prior_rejections="\n\n".join(all_prior_rejections),
                        coder_response=coder_last_response,
                    )
                    self.db.save_agent_run(review_run)
                    reviewer_results.append({
                        "model": reviewer.model,
                        "passed": passed,
                        "output": review_text,
                    })
                    if review_run.session_id:
                        task.session_ids.setdefault("reviewer", []).append(review_run.session_id)
                    log.info("Task [%s] reviewer(%s) passed=%s",
                             task.id, reviewer.model, passed)
                    if not passed:
                        all_passed = False
                        rejection_outputs.append(
                            f"=== Reviewer: {reviewer.model} | REQUEST_CHANGES ===\n"
                            + review_text
                        )
                        # Short-circuit: don't run remaining reviewers
                        log.info("Task [%s] short-circuiting after first rejection",
                                 task.id)
                        break

                # Build review output: rejections only (or single APPROVE line)
                if all_passed:
                    task.review_output = "\n\n".join(
                        f"=== Reviewer: {r['model']} | APPROVE ===\n{r['output']}"
                        for r in reviewer_results
                    )
                else:
                    # Feed only the rejection feedback because only it is meaningful for fixing
                    task.review_output = "\n\n".join(rejection_outputs)
                    # Append this round's rejections to the cumulative history
                    # so the next round's reviewers can see all prior complaints.
                    all_prior_rejections.extend(rejection_outputs)

                task.reviewer_results = reviewer_results
                task.review_pass = all_passed
                task.updated_at = time.time()
                self.db.save_task(task)

                if all_passed:
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = time.time()
                    task.updated_at = time.time()
                    self.db.save_task(task)
                    log.info("Task completed: [%s] %s", task.id, task.title)
                    self._update_parent_status(task.id)
                    break
                else:
                    if attempt < task.max_retries:
                        log.info(
                            "Review failed for [%s], retrying (%d/%d) with session=%s",
                            task.id, attempt + 1, task.max_retries, coder_session_id,
                        )
                        task.status = TaskStatus.REVIEW_FAILED
                        task.updated_at = time.time()
                        self.db.save_task(task)
                    else:
                        task.status = TaskStatus.NEEDS_ARBITRATION
                        task.error = (
                            f"Review failed after {task.max_retries + 1} attempts — "
                            f"needs human arbitration"
                        )
                        task.updated_at = time.time()
                        self.db.save_task(task)
                        log.warning(
                            "Task [%s] needs arbitration: review failed %d times",
                            task.id, task.max_retries + 1,
                        )

        except Exception as e:
            log.error("Task execution failed [%s]: %s\n%s", task_id, e, traceback.format_exc())
            task = self.db.get_task(task_id)
            if task:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                task.updated_at = time.time()
                self.db.save_task(task)
                self._update_parent_status(task_id)
        finally:
            with self._lock:
                self._futures.pop(task_id, None)

    def _update_parent_status(self, task_id: str):
        """If task has a parent, unblock dependents and check whether all
        children have reached a terminal state to update the parent.

        - All children completed → parent COMPLETED
        - Any child failed → parent FAILED
        - Any child cancelled (and none failed) → parent CANCELLED
        - Otherwise leave parent as-is (still running children)
        """
        task = self.db.get_task(task_id)
        if not task or not task.parent_id:
            return
        if task.status == TaskStatus.COMPLETED:
            for unblocked_id in self.dep_tracker.on_completed(task_id):
                log.info("Unblocking sub-task [%s] — all deps satisfied", unblocked_id)
                self.dispatch_task(unblocked_id)
        parent_id = task.parent_id
        child_ids = self.dep_tracker.get_children(parent_id)
        if not child_ids:
            return
        statuses = set()
        for cid in child_ids:
            child = self.db.get_task(cid)
            statuses.add(child.status)
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        if not statuses.issubset(terminal):
            return  # still running
        parent = self.db.get_task(parent_id)
        if TaskStatus.FAILED in statuses:
            parent.status = TaskStatus.FAILED
            parent.error = "One or more sub-tasks failed"
        elif TaskStatus.CANCELLED in statuses and TaskStatus.COMPLETED not in statuses:
            parent.status = TaskStatus.CANCELLED
        else:
            parent.status = TaskStatus.COMPLETED
            parent.completed_at = time.time()
        parent.updated_at = time.time()
        self.db.save_task(parent)
        log.info("Parent task [%s] updated to %s based on sub-task results",
                 parent.id, parent.status.value)

    def dispatch_task(self, task_id: str) -> bool:
        """Submit a single task for execution.

        Returns False (without dispatching) if the task has unmet dependencies.
        """
        if self.dep_tracker.is_blocked(task_id):
            log.info("Task [%s] blocked by dependencies — not dispatching yet", task_id)
            return False
        with self._lock:
            if task_id in self._futures:
                log.warning("Task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self._futures) >= max_p:
                log.warning("Max parallel tasks reached (%d)", max_p)
                return False
            future = self._pool.submit(self._execute_task, task_id)
            self._futures[task_id] = future
            log.info("Dispatched task: %s", task_id)
            return True

    # ── Main Loop ────────────────────────────────────────────────────

    def start(self):
        """Start the orchestrator main loop in a background thread."""
        if self.running:
            return
        self.running = True
        self._loop_thread = threading.Thread(target=self._main_loop, daemon=True)
        self._loop_thread.start()
        log.info("Orchestrator started")

        # Auto-dispatch tasks that were unblocked during dep_tracker rebuild
        if self._pending_dispatch:
            log.info(
                "Auto-dispatching %d task(s) unblocked during rebuild",
                len(self._pending_dispatch),
            )
            for tid in self._pending_dispatch:
                self.dispatch_task(tid)
            self._pending_dispatch.clear()

    def stop(self):
        """Stop the orchestrator and kill any running opencode processes."""
        self.running = False
        self.client.kill_all()
        self._pool.shutdown(wait=False)
        log.info("Orchestrator stopped")

    def _main_loop(self):
        """Keep the orchestrator alive. Tasks are only dispatched manually by the user."""
        poll_interval = self.config["orchestrator"]["poll_interval"]
        while self.running:
            time.sleep(poll_interval)

    # ── Exploration System ────────────────────────────────────────────────

    def _get_explore_categories(self) -> List[str]:
        from agents.prompts import DEFAULT_EXPLORE_CATEGORIES
        return self.config.get("explore", {}).get(
            "categories", DEFAULT_EXPLORE_CATEGORIES
        )

    def _get_explorer_model(self) -> str:
        return self.config.get("explore", {}).get(
            "explorer_model",
            self.config["opencode"].get("planner_model", ""),
        )

    def init_explore_map(self) -> dict:
        """Run the map-init agent to discover the project module structure.

        Clears existing modules and replaces them with the agent's output.
        Returns ``{"modules_created": N}``.
        """
        repo_path = self.config["repo"]["path"]
        model = self.config.get("explore", {}).get(
            "map_model", self._get_explorer_model()
        )
        explorer = ExplorerAgent(model=model, client=self.client)

        try:
            run, modules_data = explorer.init_map(repo_path)
        except Exception as e:
            log.error("Map init failed: %s", e)
            return {"error": str(e)}

        # Save the agent run for audit
        agent_run = AgentRun(
            task_id="__map_init__",
            agent_type="explorer_map_init",
            model=model,
            prompt=run.prompt,
            output=run.output,
            exit_code=run.exit_code,
            duration_sec=run.duration_sec,
            session_id=run.session_id,
        )
        self.db.save_agent_run(agent_run)

        # Clear old map
        self.db.delete_all_explore_modules()

        categories = self._get_explore_categories()

        def _create_modules(items, parent_id="", depth=0):
            created = []
            for i, item in enumerate(items):
                mod = ExploreModule(
                    name=item.get("name", ""),
                    path=item.get("path", ""),
                    parent_id=parent_id,
                    depth=depth,
                    description=item.get("description", ""),
                    category_status={c: ExploreStatus.TODO.value for c in categories},
                    category_notes={c: "" for c in categories},
                    sort_order=i,
                )
                self.db.save_explore_module(mod)
                created.append(mod)
                children = item.get("children", [])
                if children:
                    created.extend(_create_modules(children, mod.id, depth + 1))
            return created

        all_modules = _create_modules(modules_data)
        log.info("Explore map initialized: %d modules created", len(all_modules))
        return {"modules_created": len(all_modules)}

    def start_exploration(
        self,
        module_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
    ) -> dict:
        """Start exploration on selected modules x categories.

        Picks leaf modules with TODO cells if *module_ids* is empty.
        Returns ``{"started": N}``.
        """
        from agents.prompts import EXPLORER_PERSONALITIES

        all_modules = self.db.get_all_explore_modules()
        if module_ids:
            modules = [m for m in all_modules if m.id in set(module_ids)]
        else:
            # Pick leaf modules (no children) with at least one TODO cell
            child_parent_ids = {m.parent_id for m in all_modules}
            modules = [m for m in all_modules if m.id not in child_parent_ids]

        cats = categories or self._get_explore_categories()
        started = 0

        for mod in modules:
            for cat in cats:
                # Re-read from DB to avoid stale overwrites when runs
                # complete synchronously (or very fast).
                fresh = self.db.get_explore_module(mod.id)
                if fresh.category_status.get(cat) != ExploreStatus.TODO.value:
                    continue
                # Mark IN_PROGRESS
                fresh.category_status[cat] = ExploreStatus.IN_PROGRESS.value
                fresh.updated_at = time.time()
                self.db.save_explore_module(fresh)

                personality_key = self._pick_personality_for_category(cat)
                self._pool.submit(
                    self._run_exploration, mod.id, cat, personality_key
                )
                started += 1

        log.info("Exploration started: %d runs queued", started)
        return {"started": started}

    def _pick_personality_for_category(self, category: str) -> str:
        """Select a personality whose ``category`` matches the given one."""
        from agents.prompts import EXPLORER_PERSONALITIES

        candidates = [
            key for key, info in EXPLORER_PERSONALITIES.items()
            if info.get("category") == category
        ]
        if candidates:
            return random.choice(candidates)
        # Fallback: pick any
        return random.choice(list(EXPLORER_PERSONALITIES.keys()))

    def _run_exploration(self, module_id: str, category: str,
                         personality_key: str):
        """Execute a single exploration run (called in thread pool)."""
        from agents.prompts import EXPLORER_PERSONALITIES

        try:
            module = self.db.get_explore_module(module_id)
            assert module is not None, f"module {module_id} vanished from DB"
            personality = EXPLORER_PERSONALITIES[personality_key]
            repo_path = self.config["repo"]["path"]
            model = self._get_explorer_model()

            explorer = ExplorerAgent(model=model, client=self.client)
            run, findings, summary = explorer.explore_module(
                module=module,
                category=category,
                personality_focus=personality["focus"],
                personality_name=personality["name"],
                repo_path=repo_path,
            )

            # Save ExploreRun
            explore_run = ExploreRun(
                module_id=module_id,
                category=category,
                personality=personality_key,
                model=model,
                prompt=run.prompt,
                output=run.output,
                session_id=run.session_id,
                findings=findings,
                summary=summary,
                issue_count=len(findings),
                exit_code=run.exit_code,
                duration_sec=run.duration_sec,
            )
            self.db.save_explore_run(explore_run)

            # Update module status
            module = self.db.get_explore_module(module_id)
            module.category_status[category] = ExploreStatus.DONE.value
            module.category_notes[category] = summary
            module.updated_at = time.time()
            self.db.save_explore_module(module)

            # Auto-create tasks for severe findings
            auto_severity = self.config.get("explore", {}).get(
                "auto_task_severity", "major"
            )
            severity_levels = ["critical", "major", "minor", "info"]
            threshold_idx = severity_levels.index(auto_severity) if auto_severity in severity_levels else 1
            for finding in findings:
                sev = finding.get("severity", "info")
                if sev in severity_levels[:threshold_idx + 1]:
                    self._create_explore_task(module, category, finding)

            log.info(
                "Exploration complete: module=%s category=%s findings=%d",
                module.name, category, len(findings),
            )

        except Exception as e:
            log.error(
                "Exploration failed: module=%s category=%s: %s\n%s",
                module_id, category, e, traceback.format_exc(),
            )
            # Reset status so it can be retried
            module = self.db.get_explore_module(module_id)
            if module:
                module.category_status[category] = ExploreStatus.TODO.value
                module.updated_at = time.time()
                self.db.save_explore_module(module)

    @staticmethod
    def _build_explore_task(module_name: str, module_path: str,
                            category: str, finding: dict) -> "Task":
        """Build a Task object from an exploration finding (no DB save)."""
        return Task(
            title=f"[Explore/{category}] {finding['title']}",
            description=(
                f"**Found by exploration** in module `{module_name}` ({module_path})\n"
                f"**Category**: {category}\n"
                f"**Severity**: {finding['severity']}\n\n"
                f"{finding['description']}\n\n"
                f"**Suggested fix**: {finding.get('suggested_fix', 'N/A')}"
            ),
            priority=(
                TaskPriority.HIGH if finding["severity"] == "critical"
                else TaskPriority.MEDIUM
            ),
            source=TaskSource.EXPLORE,
            file_path=finding.get("file_path", ""),
            line_number=finding.get("line_number", 0),
        )

    def _create_explore_task(self, module: ExploreModule, category: str,
                             finding: dict):
        """Create and persist a Task from an exploration finding."""
        task = self._build_explore_task(
            module.name, module.path, category, finding
        )
        self.db.save_task(task)
        log.info("Created explore task [%s]: %s", task.id, task.title)

    def update_explore_module(self, module_id: str, updates: dict) -> dict:
        """Update an explore module's editable fields.

        *updates* may contain: name, description, category_status, category_notes.
        """
        module = self.db.get_explore_module(module_id)
        if not module:
            return {"error": "Module not found"}
        if "name" in updates:
            module.name = updates["name"]
        if "description" in updates:
            module.description = updates["description"]
        if "category_status" in updates:
            for cat, status in updates["category_status"].items():
                module.category_status[cat] = status
        if "category_notes" in updates:
            for cat, note in updates["category_notes"].items():
                module.category_notes[cat] = note
        module.updated_at = time.time()
        self.db.save_explore_module(module)
        return module.to_dict()

    def add_explore_module(self, name: str, path: str, parent_id: str = "",
                           description: str = "") -> dict:
        """Manually add a module to the exploration map."""
        # Determine depth from parent
        depth = 0
        if parent_id:
            parent = self.db.get_explore_module(parent_id)
            if not parent:
                return {"error": "Parent module not found"}
            depth = parent.depth + 1

        categories = self._get_explore_categories()
        module = ExploreModule(
            name=name,
            path=path,
            parent_id=parent_id,
            depth=depth,
            description=description,
            category_status={c: ExploreStatus.TODO.value for c in categories},
            category_notes={c: "" for c in categories},
        )
        self.db.save_explore_module(module)
        return module.to_dict()

    def delete_explore_module(self, module_id: str) -> dict:
        """Delete a module and all its descendants from the map."""
        module = self.db.get_explore_module(module_id)
        if not module:
            return {"error": "Module not found"}
        # Recursively delete children
        children = self.db.get_child_modules(module_id)
        for child in children:
            self.delete_explore_module(child.id)
        self.db.delete_explore_module(module_id)
        return {"deleted": True}

    def create_task_from_finding(self, run_id: str, finding_index: int) -> dict:
        """Create a Task from a specific finding in an ExploreRun."""
        explore_run = self.db.get_explore_run(run_id)
        if not explore_run:
            return {"error": "Explore run not found"}
        if finding_index < 0 or finding_index >= len(explore_run.findings):
            return {"error": "Invalid finding index"}

        finding = explore_run.findings[finding_index]
        module = self.db.get_explore_module(explore_run.module_id)
        module_name = module.name if module else "unknown"
        module_path = module.path if module else ""

        task = self._build_explore_task(
            module_name, module_path, explore_run.category, finding
        )
        self.db.save_task(task)
        log.info("Created task [%s] from explore run [%s] finding #%d",
                 task.id, run_id, finding_index)
        return task.to_dict()
