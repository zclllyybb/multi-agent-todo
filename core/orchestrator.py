"""Orchestrator: dispatches tasks to agents, manages lifecycle and parallelism."""

import logging
import os
import random
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, List, Optional, Set

from agents.coder import CoderAgent
from agents.explorer import ExplorerAgent
from agents.planner import PlannerAgent
from agents.prompts import coder_assign_jira_issue
from agents.reviewer import ReviewerAgent
from agents.slugger import SlugAgent
from core.config_persistence import ConfigPersistenceService
from core.database import Database
from core.explore_service import ExploreService
from core.jira_service import JiraService
from core.task_execution_service import TaskExecutionService
from core.task_view import TaskViewService
from core.dep_tracker import DependencyTracker
from core.models import (
    AgentRun,
    ExploreModule,
    ExploreRun,
    ExploreStatus,
    ModelOutputError,
    Task,
    TaskPriority,
    TaskSource,
    TaskStatus,
    TodoItem,
    TodoItemStatus,
)
from core.model_config import (
    ModelSpec,
    model_spec_list_to_config_value,
    model_spec_map_to_config_value,
    model_spec_to_config_value,
    parse_model_spec,
    parse_model_spec_list,
    parse_model_spec_map,
)
from core.opencode_client import OpenCodeClient
from core.worktree import WorktreeManager

log = logging.getLogger(__name__)


class Orchestrator:
    @staticmethod
    def _planner_spec_from_config(config: dict) -> ModelSpec:
        oc = config.get("opencode", {})
        return parse_model_spec(oc.get("planner", oc.get("planner_model", "")))

    @staticmethod
    def _default_coder_spec_from_config(config: dict) -> ModelSpec:
        oc = config.get("opencode", {})
        return parse_model_spec(
            oc.get(
                "coder_default",
                oc.get("coder_model_default", oc.get("coder_model", "")),
            )
        )

    @staticmethod
    def _coder_specs_by_complexity_from_config(config: dict) -> dict[str, ModelSpec]:
        oc = config.get("opencode", {})
        return parse_model_spec_map(
            oc.get("coder_by_complexity", oc.get("coder_model_by_complexity", {}))
        )

    @staticmethod
    def _reviewer_specs_from_config(config: dict) -> list[ModelSpec]:
        oc = config.get("opencode", {})
        reviewers = oc.get("reviewers")
        if reviewers is not None:
            return parse_model_spec_list(reviewers)
        legacy = oc.get("reviewer_models", [oc.get("reviewer_model", "")])
        return parse_model_spec_list(legacy)

    @staticmethod
    def _explorer_spec_from_config(config: dict) -> ModelSpec:
        explore = config.get("explore", {})
        spec = parse_model_spec(explore.get("explorer"))
        if spec.is_set:
            return spec
        legacy = parse_model_spec(explore.get("explorer_model", ""))
        if legacy.is_set:
            return legacy
        return Orchestrator._planner_spec_from_config(config)

    @staticmethod
    def _map_spec_from_config(config: dict) -> ModelSpec:
        explore = config.get("explore", {})
        spec = parse_model_spec(explore.get("map"))
        if spec.is_set:
            return spec
        legacy = parse_model_spec(explore.get("map_model", ""))
        if legacy.is_set:
            return legacy
        return Orchestrator._explorer_spec_from_config(config)

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
        self.client = OpenCodeClient(
            timeout=config["opencode"]["timeout"],
            config_path=config.get("opencode", {}).get("config_path", ""),
        )
        self.jira = JiraService(self)
        self.task_exec = TaskExecutionService(self)
        self.explore = ExploreService(self)

        # Agents
        planner_spec = self._planner_spec_from_config(config)
        self.planner = PlannerAgent(
            model=planner_spec.model,
            variant=planner_spec.variant,
            agent=planner_spec.agent,
            client=self.client,
        )
        # Coder: one agent per complexity level, keyed by complexity string
        default_coder_spec = self._default_coder_spec_from_config(config)
        complexity_map = self._coder_specs_by_complexity_from_config(config)
        self._coder_by_complexity: Dict[str, CoderAgent] = {}
        for level, spec in complexity_map.items():
            self._coder_by_complexity[level] = CoderAgent(
                model=spec.model,
                variant=spec.variant,
                agent=spec.agent,
                client=self.client,
            )
        self._default_coder = CoderAgent(
            model=default_coder_spec.model,
            variant=default_coder_spec.variant,
            agent=default_coder_spec.agent,
            client=self.client,
        )
        self._slug_agent = SlugAgent(
            model=default_coder_spec.model,
            variant=default_coder_spec.variant,
            agent=default_coder_spec.agent,
            client=self.client,
        )

        # Reviewers: one agent per configured model; all must approve
        reviewer_specs = self._reviewer_specs_from_config(config)
        self.reviewers: List[ReviewerAgent] = [
            ReviewerAgent(
                model=spec.model,
                variant=spec.variant,
                agent=spec.agent,
                client=self.client,
            )
            for spec in reviewer_specs
            if spec.model
        ]

        # Thread pool for parallel execution
        max_parallel = config["orchestrator"]["max_parallel_tasks"]
        self._pool = ThreadPoolExecutor(max_workers=max_parallel)
        log.info(
            "Orchestrator initialized: max_parallel=%d, repo=%s",
            max_parallel,
            config["repo"]["path"],
        )

        # Dependency tracking between sub-tasks (pure in-memory, rebuilt on split)
        self.dep_tracker = DependencyTracker()
        self._rebuild_dep_tracker()
        self._pending_dispatch: List[str] = []

        # Cache for UI resource snapshot (git branches/worktrees) to keep
        # dashboard auto-refresh lightweight.
        self._resource_snapshot_cache = (set(), {})
        self._resource_snapshot_cached_at = 0.0

        # Recovery: reset any TodoItems stuck in ANALYZING from a previous crash.
        # (from_dict already converts ANALYZING → PENDING_ANALYSIS on load, but items
        #  in the DB still have status=analyzing until we overwrite them.)
        self._recover_stuck_analyzing()

        # Exploration scheduling state (separate logical queue, using shared thread pool)
        self._explore_parallel_limit = self._get_explore_parallel_limit()
        self._explore_queue: List[dict] = []
        self._explore_running: Dict[str, dict] = {}
        self._explore_cancel_requested: Set[str] = set()
        self._explore_seq = 0

        # Explore map initialization state (persisted in DB for UI + restart recovery)
        self._explore_map_state_key = "explore_map_init_state"
        self._explore_map_task_id = "__map_init__"
        self._explore_map_state = self._default_explore_map_state()
        self._explore_map_cancel_requested = False
        self._explore_map_future: Optional[Future] = None

        # Recovery: restore persisted explore map state and queue jobs.
        self._load_explore_map_state()
        self._recover_explore_queue_jobs()

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

    def _recover_stuck_exploration(self, active_keys: Optional[Set[str]] = None):
        """Reset stale ExploreModule cells left in IN_PROGRESS by previous daemon exit."""
        active_keys = active_keys or set()
        modules = self.db.get_all_explore_modules()
        recovered = 0
        for m in modules:
            changed = False
            for cat, st in list(m.category_status.items()):
                if st == ExploreStatus.IN_PROGRESS.value:
                    key = self._explore_job_key(m.id, cat)
                    if key in active_keys:
                        continue
                    m.category_status[cat] = ExploreStatus.TODO.value
                    # Keep old note for diagnostics unless emptying is preferred.
                    # We clear to avoid stale "in-progress" semantics in UI.
                    m.category_notes[cat] = ""
                    changed = True
                    recovered += 1
            if changed:
                self.db.save_explore_module(m)
        if recovered:
            log.warning(
                "Recovered %d explore cell(s) stuck in IN_PROGRESS (server restart)",
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
        self._pending_dispatch: List[str] = []
        for t in all_tasks:
            if t.parent_id and TaskStatus.is_dependency_terminal(t.status):
                for uid in self.dep_tracker.on_completed(t.id):
                    unblocked_task = self.db.get_task(uid)
                    if unblocked_task and unblocked_task.status == TaskStatus.PENDING:
                        self._pending_dispatch.append(uid)

        if registered:
            log.info(
                "Rebuilt dep_tracker from DB: %d child task(s) across %d parent(s), "
                "%d task(s) ready to dispatch",
                registered,
                len(parent_children),
                len(self._pending_dispatch),
            )

    def _collect_resource_snapshot(self) -> tuple[set[str], dict[str, list[str]]]:
        return self._task_view_service().collect_resource_snapshot()

    @staticmethod
    def _task_resource_state(
        task: Task,
        local_branches: set[str],
        branch_worktrees: dict[str, list[str]],
    ) -> dict:
        return TaskViewService.task_resource_state(
            task, local_branches, branch_worktrees
        )

    def serialize_tasks_for_ui(self, tasks: List[Task]) -> List[dict]:
        return self._task_view_service().serialize_tasks_for_ui(tasks)

    def serialize_task_for_ui(self, task: Task) -> dict:
        return self._task_view_service().serialize_task_for_ui(task)

    def add_task_comment(self, task_id: str, username: str, content: str) -> dict:
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}

        username = username.strip()
        content = content.strip()
        if not username:
            return {"error": "username required"}
        if not content:
            return {"error": "content required"}

        now = time.time()
        task.comments.append(
            {
                "id": uuid.uuid4().hex[:12],
                "username": username,
                "content": content,
                "created_at": now,
            }
        )
        task.updated_at = now
        self.db.save_task(task)
        return {
            "ok": True,
            "task": self.serialize_task_for_ui(task),
            "comments": list(task.comments),
        }

    # ── Branch Name Generation ──────────────────────────────────────

    def _generate_branch_slug(self, title: str, task_id: str) -> str:
        """Ask the cheapest/simplest model to produce a short git-safe slug from title.

        Falls back to the task_id-only style on any error.
        """
        short_id = task_id[:8]
        try:
            # Use the 'simple' coder model (cheapest configured) or the default
            simple_agent = (
                self._coder_by_complexity.get("simple") or self._default_coder
            )
            prompt = (
                f"Convert the following task title into a concise git branch name slug "
                f"(lowercase, hyphens only, max 5 words, no special chars, no prefix):\n"
                f"{title}\n\n"
                f"Reply with ONLY the slug, nothing else."
            )
            repo_path = self.config["repo"]["path"]
            self._slug_agent.model = simple_agent.model
            self._slug_agent.variant = simple_agent.variant
            self._slug_agent.agent = simple_agent.agent
            agent_run = self._slug_agent.run(prompt, repo_path)
            text = self._slug_agent.get_final_text(agent_run).strip().lower()
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
          planner / planner_model
          coder_default / coder_model_default
          coder_by_complexity / coder_model_by_complexity
          reviewers / reviewer_models
          explorer / explorer_model
          map / map_model

        Each value can be either:
          - a plain model string
          - {model: "...", variant: "...", agent: "..."}
        """
        oc = self.config.setdefault("opencode", {})
        explore = self.config.setdefault("explore", {})

        if "planner" in updates or "planner_model" in updates:
            spec = parse_model_spec(
                updates.get("planner", updates.get("planner_model"))
            )
            if spec.is_set:
                self.planner = PlannerAgent(
                    model=spec.model,
                    variant=spec.variant,
                    agent=spec.agent,
                    client=self.client,
                )
                oc["planner"] = model_spec_to_config_value(spec)
                oc["planner_model"] = spec.model
                log.info(
                    "Updated planner model: %s variant=%s agent=%s",
                    spec.model,
                    spec.variant or "-",
                    spec.agent or "-",
                )

        if "coder_default" in updates or "coder_model_default" in updates:
            spec = parse_model_spec(
                updates.get("coder_default", updates.get("coder_model_default"))
            )
            if spec.is_set:
                self._default_coder = CoderAgent(
                    model=spec.model,
                    variant=spec.variant,
                    agent=spec.agent,
                    client=self.client,
                )
                oc["coder_default"] = model_spec_to_config_value(spec)
                oc["coder_model_default"] = spec.model
                log.info(
                    "Updated default coder model: %s variant=%s agent=%s",
                    spec.model,
                    spec.variant or "-",
                    spec.agent or "-",
                )

        if "coder_by_complexity" in updates or "coder_model_by_complexity" in updates:
            cmap = parse_model_spec_map(
                updates.get(
                    "coder_by_complexity", updates.get("coder_model_by_complexity")
                )
            )
            if cmap:
                self._coder_by_complexity = {
                    level: CoderAgent(
                        model=spec.model,
                        variant=spec.variant,
                        agent=spec.agent,
                        client=self.client,
                    )
                    for level, spec in cmap.items()
                }
                oc["coder_by_complexity"] = model_spec_map_to_config_value(cmap)
                oc["coder_model_by_complexity"] = {
                    level: spec.model for level, spec in cmap.items()
                }
                log.info(
                    "Updated coder complexity map: %s",
                    {
                        level: {
                            "model": spec.model,
                            "variant": spec.variant,
                            "agent": spec.agent,
                        }
                        for level, spec in cmap.items()
                    },
                )

        if "reviewers" in updates or "reviewer_models" in updates:
            specs = parse_model_spec_list(
                updates.get("reviewers", updates.get("reviewer_models"))
            )
            self.reviewers = [
                ReviewerAgent(
                    model=spec.model,
                    variant=spec.variant,
                    agent=spec.agent,
                    client=self.client,
                )
                for spec in specs
            ]
            oc["reviewers"] = model_spec_list_to_config_value(specs)
            oc["reviewer_models"] = [spec.model for spec in specs]
            log.info(
                "Updated reviewer models: %s",
                [
                    {"model": spec.model, "variant": spec.variant, "agent": spec.agent}
                    for spec in specs
                ],
            )

        if "explorer" in updates or "explorer_model" in updates:
            spec = parse_model_spec(
                updates.get("explorer", updates.get("explorer_model"))
            )
            if spec.is_set:
                explore["explorer"] = model_spec_to_config_value(spec)
                explore["explorer_model"] = spec.model
                log.info(
                    "Updated explorer model: %s variant=%s agent=%s",
                    spec.model,
                    spec.variant or "-",
                    spec.agent or "-",
                )

        if "map" in updates or "map_model" in updates:
            spec = parse_model_spec(updates.get("map", updates.get("map_model")))
            if spec.is_set:
                explore["map"] = model_spec_to_config_value(spec)
                explore["map_model"] = spec.model
                log.info(
                    "Updated map model: %s variant=%s agent=%s",
                    spec.model,
                    spec.variant or "-",
                    spec.agent or "-",
                )

        # Persist model config changes so they survive restarts.
        self._save_model_config()

    def _get_jira_config(self) -> dict:
        return self._jira_service().get_jira_config()

    def _jira_service(self) -> JiraService:
        service = getattr(self, "jira", None)
        if service is None:
            service = JiraService(self)
            self.jira = service
        return service

    def _task_exec_service(self) -> TaskExecutionService:
        service = getattr(self, "task_exec", None)
        if service is None:
            service = TaskExecutionService(self)
            self.task_exec = service
        return service

    def _explore_service(self) -> ExploreService:
        service = getattr(self, "explore", None)
        if service is None:
            service = ExploreService(self)
            self.explore = service
        return service

    def _config_persistence_service(self) -> ConfigPersistenceService:
        service = getattr(self, "config_persistence", None)
        if service is None:
            service = ConfigPersistenceService(self)
            self.config_persistence = service
        return service

    def _task_view_service(self) -> TaskViewService:
        service = getattr(self, "task_view", None)
        if service is None:
            service = TaskViewService(self)
            self.task_view = service
        return service

    def _run_jira_agent(self, task: Task) -> tuple[AgentRun, str]:
        return self._jira_service().run_jira_agent(task)

    def _parse_jira_agent_result(self, task: Task, text: str) -> dict:
        return self._jira_service().parse_jira_agent_result(task, text)

    @staticmethod
    def _build_jira_browse_url(jira_base_url: str, issue_key: str) -> str:
        return JiraService.build_jira_browse_url(jira_base_url, issue_key)

    def submit_jira_task(
        self,
        title: str,
        description: str,
        priority: str = "medium",
        source_task_id: str = "",
    ) -> Task:
        return self._jira_service().submit_jira_task(
            title, description, priority, source_task_id
        )

    def assign_jira_for_task(self, source_task_id: str) -> dict:
        return self._jira_service().assign_jira_for_task(source_task_id)

    def _dispatch_jira_task(self, task_id: str) -> bool:
        return self._jira_service().dispatch_jira_task(task_id)

    def _jira_task_pipeline(self, task_id: str):
        return self._jira_service().jira_task_pipeline(task_id)

    def _save_model_config(self):
        return self._config_persistence_service().save_model_config()

    @staticmethod
    def _patch_yaml_lines(
        lines: list, oc: dict, explore: Optional[dict] = None
    ) -> list:
        return ConfigPersistenceService.patch_yaml_lines(lines, oc, explore)

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
        if not TaskStatus.is_cleanable(task.status):
            return {
                "error": f"Cannot clean task in '{task.status.value}' state — it may still be running"
            }
        if not task.branch_name and not task.worktree_path:
            return {"error": "Task has no branch/worktree to clean"}
        removed_branch = task.branch_name
        try:
            if task.branch_name:
                self.worktree_mgr.remove_worktree(
                    task.branch_name, worktree_path=task.worktree_path
                )
                log.info(
                    "Cleaned worktree for task [%s]: %s", task_id, task.branch_name
                )
            else:
                self.worktree_mgr.remove_worktree_path_only(task.worktree_path)
                log.info(
                    "Cleaned worktree(path-only) for task [%s]: %s",
                    task_id,
                    task.worktree_path,
                )
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
            return {
                "cleaned": True,
                "branch": removed_branch,
                "warnings": f"Parent cleaned but some children failed: {'; '.join(child_errors)}",
            }
        return {"cleaned": True, "branch": removed_branch}

    def cancel_task(self, task_id: str) -> dict:
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        already_terminal = TaskStatus.is_cancel_terminal(task.status)
        if not already_terminal:
            task.status = TaskStatus.CANCELLED
            task.updated_at = time.time()
            self.db.save_task(task)
            # Kill any running opencode process for this task immediately
            self.client.kill_task(task_id)
            # Clean up worktree if exists
            if task.branch_name:
                try:
                    self.worktree_mgr.remove_worktree(
                        task.branch_name, worktree_path=task.worktree_path
                    )
                    log.info(
                        "Removed worktree for cancelled task [%s]: %s",
                        task_id,
                        task.branch_name,
                    )
                    task.worktree_path = ""
                    task.branch_name = ""
                    task.updated_at = time.time()
                    self.db.save_task(task)
                except Exception as e:
                    log.warning(
                        "Failed to remove worktree for %s: %s — user can clean manually",
                        task_id,
                        e,
                    )
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
                    log.info(
                        "Auto-reverted todo [%s] after cancelling task [%s]",
                        item.id,
                        task_id,
                    )
            log.info("Cancelled task: [%s]", task_id)
            self._update_parent_status(task_id)
        # Always cascade cancel to non-terminal child tasks (even if this
        # task is already completed/cancelled — descendants may still be running)
        for child in self._get_child_tasks(task_id):
            if not TaskStatus.is_cancel_terminal(child.status):
                child_result = self.cancel_task(child.id)
                if "error" in child_result:
                    log.warning(
                        "Failed to cascade cancel to child [%s]: %s",
                        child.id,
                        child_result["error"],
                    )
        return {"cancelled": True}

    def revise_task(self, task_id: str, feedback: str) -> dict:
        return self._task_exec_service().revise_task(task_id, feedback)

    def resume_task(self, task_id: str, message: str = "Continue") -> dict:
        return self._task_exec_service().resume_task(task_id, message)

    def resolve_arbitration(
        self, task_id: str, action: str, feedback: str = ""
    ) -> dict:
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
        if not TaskStatus.is_awaiting_arbitration(task.status):
            return {
                "error": f"Task is not awaiting arbitration (status={task.status.value})"
            }

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
            return {
                "error": f"Unknown action '{action}'; must be approve/revise/reject"
            }

    def _dispatch_revise(self, task_id: str) -> bool:
        return self._task_exec_service().dispatch_revise(task_id)

    def _dispatch_resume(self, task_id: str, first_message: str) -> bool:
        return self._task_exec_service().dispatch_resume(task_id, first_message)

    def _revise_task_pipeline(
        self,
        task_id: str,
        first_coder_message: str = "",
        first_message_raw: bool = False,
    ):
        return self._task_exec_service().revise_task_pipeline(
            task_id,
            first_coder_message,
            first_message_raw,
        )

    def get_status(self) -> dict:
        """Get overall system status."""
        tasks = self.db.get_all_tasks()
        status_counts = {}
        for t in tasks:
            s = t.status.value
            status_counts[s] = status_counts.get(s, 0) + 1
        active = [t.to_dict() for t in tasks if TaskStatus.is_active(t.status)]
        return {
            "running": self.running,
            "total_tasks": len(tasks),
            "status_counts": status_counts,
            "active_task_count": len(active),
            "active_tasks": active,
            "active_futures": len(self._futures),
        }

    def submit_task(
        self,
        title: str,
        description: str,
        priority: str = "medium",
        file_path: str = "",
        line_number: int = 0,
        parent_id: str = "",
        copy_files: Optional[list] = None,
        force_no_split: bool = False,
    ) -> Task:
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
            force_no_split=force_no_split,
        )
        self.db.save_task(task)
        log.info("Submitted task: [%s] %s", task.id, task.title)
        self.dispatch_task(task.id)
        return task

    def submit_review_task(
        self,
        title: str,
        review_input: str,
        priority: str = "medium",
        copy_files: Optional[list] = None,
    ) -> Task:
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
        return self._task_exec_service().dispatch_review_only(task_id)

    def _review_only_pipeline(self, task_id: str):
        return self._task_exec_service().review_only_pipeline(task_id)

    def _cleanup_review_worktree(self, task):
        return self._task_exec_service().cleanup_review_worktree(task)

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
                    item["file"],
                    item["line"],
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
                todo_id,
                item.task_id,
            )
            return {"error": "already_dispatched", "status": 409}

        # Mark as ANALYZING immediately so concurrent requests see the lock
        prev_status = item.status
        item.status = TodoItemStatus.ANALYZING
        item.updated_at = time.time()
        self.db.save_todo_item(item)
        log.info(
            "analyze_todo_item: starting analysis for todo [%s] (prev_status=%s)",
            todo_id,
            prev_status.value,
        )

        repo_path = self.config["repo"]["path"]
        try:
            run, feasibility, difficulty, note = self._analyze_todo_with_retry(
                item,
                repo_path,
            )
        except Exception as e:
            log.error(
                "analyze_todo_item: analysis failed for todo [%s]: %s",
                todo_id,
                traceback.format_exc(),
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
            todo_id,
            feasibility,
            difficulty,
            note[:80],
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
        if not TaskStatus.is_publishable(task.status):
            return {"error": f"task is not completed (status={task.status.value})"}
        if not task.branch_name:
            return {"error": "task has no branch (was it split into sub-tasks?)"}
        remote = self.config.get("publish", {}).get("remote", "origin")
        ok, msg = self.worktree_mgr.publish_branch(task.branch_name, remote)
        if ok:
            task.published_at = time.time()
            task.updated_at = time.time()
            self.db.save_task(task)
            log.info(
                "Published task [%s] branch %s to %s", task_id, task.branch_name, remote
            )
        return {
            "success": ok,
            "message": msg,
            "branch": task.branch_name,
            "remote": remote,
        }

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
                force_no_split=task.force_no_split,
            )
        except ModelOutputError as first_err:
            log.warning(
                "Task [%s] planner output unparseable, retrying once: %s",
                task.id,
                first_err,
            )
            try:
                return self.planner.analyze_and_split(
                    title=task.title,
                    description=task.description,
                    repo_path=repo_path,
                    task_id=task.id,
                    force_no_split=task.force_no_split,
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
                item.id,
                first_err,
            )
            try:
                return self.planner.analyze_todo(item, repo_path)
            except ModelOutputError as second_err:
                raise ModelOutputError(
                    f"Todo [{item.id}] analyzer failed after retry: {second_err}"
                ) from second_err

    def _latest_coder_session_id(self, task: Task) -> str:
        return self._task_exec_service().latest_coder_session_id(task)

    def _extract_coder_response(self, code_run: AgentRun) -> str:
        return self._task_exec_service().extract_coder_response(code_run)

    def _ensure_coder_run_success(self, code_run: AgentRun, attempt: int):
        return self._task_exec_service().ensure_coder_run_success(code_run, attempt)

    def _execute_task(self, task_id: str):
        return self._task_exec_service().execute_task(task_id)

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
        if not all(TaskStatus.is_dependency_terminal(status) for status in statuses):
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
        log.info(
            "Parent task [%s] updated to %s based on sub-task results",
            parent.id,
            parent.status.value,
        )

    def delete_tasks(
        self, task_ids: list[str], cascade_descendants: bool = True
    ) -> dict:
        deleted: list[str] = []
        errors: dict[str, str] = {}
        all_tasks = self.db.get_all_tasks()
        tasks_by_id = {task.id: task for task in all_tasks}
        children_by_parent: dict[str, list[str]] = {}
        for task in all_tasks:
            if task.parent_id:
                children_by_parent.setdefault(task.parent_id, []).append(task.id)
        local_branches, branch_worktrees = self._collect_resource_snapshot()

        requested_ids = list(dict.fromkeys(task_ids))
        requested_set = set(requested_ids)

        def _has_requested_ancestor(task_id: str) -> bool:
            task = tasks_by_id.get(task_id)
            visited: set[str] = set()
            while task and task.parent_id and task.parent_id not in visited:
                if task.parent_id in requested_set:
                    return True
                visited.add(task.parent_id)
                task = tasks_by_id.get(task.parent_id)
            return False

        def _collect_subtree_ids(root_id: str) -> list[str]:
            ordered: list[str] = []
            stack = [root_id]
            seen: set[str] = set()
            while stack:
                current_id = stack.pop()
                if current_id in seen:
                    continue
                seen.add(current_id)
                ordered.append(current_id)
                for child_id in reversed(children_by_parent.get(current_id, [])):
                    stack.append(child_id)
            return ordered

        def _task_delete_error(root_id: str, failing_task_id: str, message: str) -> str:
            if failing_task_id == root_id:
                return message
            return f"Descendant task {failing_task_id}: {message}"

        root_ids: list[str] = []
        for task_id in requested_ids:
            if (
                task_id in tasks_by_id
                and cascade_descendants
                and _has_requested_ancestor(task_id)
            ):
                continue
            root_ids.append(task_id)

        root_plans: dict[str, list[str]] = {}
        root_plan_sets: dict[str, set[str]] = {}

        for root_id in root_ids:
            root_task = tasks_by_id.get(root_id)
            if not root_task:
                errors[root_id] = "Task not found"
                continue

            plan_ids = (
                _collect_subtree_ids(root_id) if cascade_descendants else [root_id]
            )
            root_plans[root_id] = plan_ids
            root_plan_sets[root_id] = set(plan_ids)

            for planned_id in plan_ids:
                task = tasks_by_id[planned_id]
                if planned_id in getattr(self, "_futures", {}):
                    errors[root_id] = _task_delete_error(
                        root_id, planned_id, "Task is currently running"
                    )
                    break
                if planned_id in getattr(self, "_pending_dispatch", []):
                    errors[root_id] = _task_delete_error(
                        root_id, planned_id, "Task is queued for dispatch"
                    )
                    break
                if getattr(self.client, "_task_procs", {}).get(planned_id):
                    errors[root_id] = _task_delete_error(
                        root_id, planned_id, "Task still has an active process"
                    )
                    break
                resource_state = self._task_resource_state(
                    task, local_branches, branch_worktrees
                )
                if resource_state.get("actual_branch_exists") or resource_state.get(
                    "actual_worktree_exists"
                ):
                    errors[root_id] = _task_delete_error(
                        root_id,
                        planned_id,
                        "Task still has branch/worktree resources; clean it first",
                    )
                    break

        valid_root_ids = [
            root_id
            for root_id in root_ids
            if root_id in root_plans and root_id not in errors
        ]

        while True:
            planned_delete_ids: set[str] = set()
            for root_id in valid_root_ids:
                planned_delete_ids.update(root_plan_sets[root_id])

            newly_invalid: list[str] = []
            for root_id in valid_root_ids:
                reason = ""
                for planned_id in root_plans[root_id]:
                    external_child = next(
                        (
                            other.id
                            for other in all_tasks
                            if other.id not in planned_delete_ids
                            and other.parent_id == planned_id
                        ),
                        "",
                    )
                    if external_child:
                        reason = _task_delete_error(
                            root_id,
                            planned_id,
                            f"Task has child task {external_child}; delete it first",
                        )
                        break

                    external_dep = next(
                        (
                            other.id
                            for other in all_tasks
                            if other.id not in planned_delete_ids
                            and planned_id in other.depends_on
                        ),
                        "",
                    )
                    if external_dep:
                        reason = _task_delete_error(
                            root_id,
                            planned_id,
                            f"Task is referenced by dependent task {external_dep}; delete it first",
                        )
                        break

                    external_jira = next(
                        (
                            other.id
                            for other in all_tasks
                            if other.id not in planned_delete_ids
                            and other.task_mode == "jira"
                            and other.jira_source_task_id == planned_id
                        ),
                        "",
                    )
                    if external_jira:
                        reason = _task_delete_error(
                            root_id,
                            planned_id,
                            f"Task is referenced by jira-mode task {external_jira}; delete it first",
                        )
                        break

                if reason:
                    errors[root_id] = reason
                    newly_invalid.append(root_id)

            if not newly_invalid:
                break
            valid_root_ids = [
                root_id for root_id in valid_root_ids if root_id not in newly_invalid
            ]

        depth_cache: dict[str, int] = {}

        def _task_depth(task_id: str) -> int:
            if task_id in depth_cache:
                return depth_cache[task_id]
            task = tasks_by_id.get(task_id)
            if not task or not task.parent_id:
                depth_cache[task_id] = 0
                return 0
            depth_cache[task_id] = 1 + _task_depth(task.parent_id)
            return depth_cache[task_id]

        planned_delete_ids: set[str] = set()
        for root_id in valid_root_ids:
            planned_delete_ids.update(root_plan_sets[root_id])

        delete_order = sorted(planned_delete_ids, key=_task_depth, reverse=True)

        for task_id in delete_order:
            task = tasks_by_id[task_id]
            self.client.kill_task(task_id)
            self.dep_tracker.cleanup(task_id)
            self._pending_dispatch = [
                tid for tid in self._pending_dispatch if tid != task_id
            ]

            for item in self.db.get_all_todo_items():
                if item.task_id != task_id:
                    continue
                if item.status == TodoItemStatus.DISPATCHED:
                    item.status = TodoItemStatus.ANALYZED
                item.task_id = ""
                item.updated_at = time.time()
                self.db.save_todo_item(item)

            self.db.delete_agent_runs_for_task(task_id)
            self.db.delete_task(task_id)
            deleted.append(task_id)
            log.warning("Deleted task record: [%s] %s", task_id, task.title)

        return {
            "deleted": len(deleted),
            "deleted_ids": deleted,
            "errors": errors,
        }

    def dispatch_task(self, task_id: str) -> bool:
        """Submit a single task for execution.

        Returns False (without dispatching) if the task has unmet dependencies.
        """
        if self.dep_tracker.is_blocked(task_id):
            log.info("Task [%s] blocked by dependencies — not dispatching yet", task_id)
            return False
        task = self.db.get_task(task_id)
        if not task:
            log.warning("Task not found for dispatch: %s", task_id)
            return False
        if task.task_mode == "review":
            return self._dispatch_review_only(task_id)
        if task.task_mode == "jira":
            return self._dispatch_jira_task(task_id)
        with self._lock:
            if task_id in self._futures:
                log.warning("Task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self._futures) >= max_p:
                log.warning("Max parallel tasks reached (%d)", max_p)
                if task_id not in self._pending_dispatch:
                    self._pending_dispatch.append(task_id)
                    log.info("Queued task [%s] for deferred dispatch", task_id)
                return False
            if task_id in self._pending_dispatch:
                self._pending_dispatch.remove(task_id)
            future = self._pool.submit(self._execute_task, task_id)
            self._futures[task_id] = future
            log.info("Dispatched task: %s", task_id)
            return True

    def _queue_pending_dispatch(self, task_id: str):
        """Retry dispatch later when capacity frees up."""
        with self._lock:
            if task_id in self._pending_dispatch:
                return
            self._pending_dispatch.append(task_id)
        log.info("Queued task [%s] for deferred dispatch", task_id)

    def _flush_pending_dispatches(self):
        """Dispatch queued tasks after a running slot becomes available."""
        while True:
            with self._lock:
                if not self._pending_dispatch:
                    return
                max_p = self.config["orchestrator"]["max_parallel_tasks"]
                if len(self._futures) >= max_p:
                    return
                task_id = self._pending_dispatch.pop(0)
            if not self.dispatch_task(task_id):
                with self._lock:
                    if task_id not in self._pending_dispatch:
                        self._pending_dispatch.insert(0, task_id)
                return

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
        self.cancel_init_explore_map()
        self.cancel_exploration(include_running=True)
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
        return self._explore_service().get_explore_categories()

    def _get_explorer_model(self) -> str:
        return self._explore_service().get_explorer_model()

    def _get_explorer_spec(self) -> ModelSpec:
        return self._explore_service().get_explorer_spec()

    def _get_map_spec(self) -> ModelSpec:
        return self._explore_service().get_map_spec()

    def _get_explore_parallel_limit(self) -> int:
        return self._explore_service().get_explore_parallel_limit()

    def _repo_name(self) -> str:
        return self._explore_service().repo_name()

    @staticmethod
    def _trim_stream_output(output: str, max_chars: int = 240000) -> str:
        return ExploreService.trim_stream_output(output, max_chars)

    def _default_explore_map_state(self) -> dict:
        return self._explore_service().default_explore_map_state()

    def _persist_explore_map_state(self):
        return self._explore_service().persist_explore_map_state()

    def _load_explore_map_state(self):
        return self._explore_service().load_explore_map_state()

    def reset_explore_state(self) -> dict:
        return self._explore_service().reset_explore_state()

    def _persist_explore_job(self, job: dict):
        return self._explore_service().persist_explore_job(job)

    def _recover_explore_queue_jobs(self):
        return self._explore_service().recover_explore_queue_jobs()

    def is_explore_map_ready(self) -> bool:
        return self._explore_service().is_explore_map_ready()

    def get_explore_init_state(self) -> dict:
        return self._explore_service().get_explore_init_state()

    def get_explore_status(self) -> dict:
        return self._explore_service().get_explore_status()

    @staticmethod
    def _explore_job_key(module_id: str, category: str) -> str:
        return ExploreService.explore_job_key(module_id, category)

    def _list_target_modules_for_explore(
        self,
        module_ids: Optional[List[str]],
        leaf_only_when_empty: bool = True,
    ) -> List[ExploreModule]:
        return self._explore_service().list_target_modules_for_explore(
            module_ids,
            leaf_only_when_empty,
        )

    def _validate_explore_categories(
        self, categories: Optional[List[str]]
    ) -> tuple[List[str], List[str]]:
        return self._explore_service().validate_explore_categories(categories)

    def _next_explore_seq_locked(self) -> int:
        return self._explore_service().next_explore_seq_locked()

    def _set_module_category_status(
        self, module_id: str, category: str, status: str, note: Optional[str] = None
    ):
        return self._explore_service().set_module_category_status(
            module_id,
            category,
            status,
            note,
        )

    @staticmethod
    def _append_explore_note(
        existing: str, new_note: str, max_chars: int = 8000
    ) -> str:
        return ExploreService.append_explore_note(existing, new_note, max_chars)

    @staticmethod
    def _build_explore_note_entry(
        summary: str,
        focus_point: str,
        actionability_score: float,
        reliability_score: float,
        explored_scope: str,
        completion_status: str,
        supplemental_note: str,
    ) -> str:
        return ExploreService.build_explore_note_entry(
            summary,
            focus_point,
            actionability_score,
            reliability_score,
            explored_scope,
            completion_status,
            supplemental_note,
        )

    @staticmethod
    def _build_map_review_prompt(review_reason: str) -> str:
        return ExploreService.build_map_review_prompt(review_reason)

    def _request_explore_map_review(
        self, module: ExploreModule, category: str, reason: str
    ):
        return self._explore_service().request_explore_map_review(
            module,
            category,
            reason,
        )

    def _is_explore_cancel_requested(self, key: str) -> bool:
        return self._explore_service().is_explore_cancel_requested(key)

    def _clear_explore_cancel_flag(self, key: str):
        return self._explore_service().clear_explore_cancel_flag(key)

    def _dispatch_explore_queue_locked(self) -> List[dict]:
        return self._explore_service().dispatch_explore_queue_locked()

    def _submit_explore_jobs(self, jobs: List[dict]):
        return self._explore_service().submit_explore_jobs(jobs)

    def _run_exploration_job(self, job: dict):
        return self._explore_service().run_exploration_job(job)

    def get_exploration_queue_state(self) -> dict:
        return self._explore_service().get_exploration_queue_state()

    def cancel_exploration(
        self,
        module_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        include_running: bool = True,
    ) -> dict:
        return self._explore_service().cancel_exploration(
            module_ids,
            categories,
            include_running,
        )

    def _apply_explore_map(
        self, run: AgentRun, modules_data: List[dict], model: str
    ) -> int:
        return self._explore_service().apply_explore_map(run, modules_data, model)

    def init_explore_map(self) -> dict:
        return self._explore_service().init_explore_map()

    def _start_init_explore_map(self, review_reason: str = "") -> dict:
        return self._explore_service().start_init_explore_map(review_reason)

    def start_init_explore_map(self, review_reason: str = "") -> dict:
        return self._start_init_explore_map(review_reason=review_reason)

    def reinitialize_explore_map(self, review_reason: str = "") -> dict:
        return self._explore_service().reinitialize_explore_map(review_reason)

    def cancel_init_explore_map(self) -> dict:
        return self._explore_service().cancel_init_explore_map()

    def _run_init_explore_map_job(
        self,
        model: str,
        variant: str = "",
        review_message: str = "",
        review_session_id: str = "",
    ):
        return self._explore_service().run_init_explore_map_job(
            model,
            variant,
            review_message,
            review_session_id,
        )

    def start_exploration(
        self,
        module_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        focus_point: str = "",
    ) -> dict:
        return self._explore_service().start_exploration(
            module_ids,
            categories,
            focus_point,
        )

    def _pick_personality_for_category(self, category: str) -> str:
        return self._explore_service().pick_personality_for_category(category)

    def _run_exploration(
        self,
        module_id: str,
        category: str,
        personality_key: str,
        job: Optional[dict] = None,
    ):
        return self._explore_service().run_exploration(
            module_id,
            category,
            personality_key,
            job,
        )

    @staticmethod
    def _build_explore_task(
        module_name: str, module_path: str, category: str, finding: dict
    ) -> "Task":
        return ExploreService.build_explore_task(
            module_name,
            module_path,
            category,
            finding,
        )

    def _create_explore_task(self, module: ExploreModule, category: str, finding: dict):
        return self._explore_service().create_explore_task(module, category, finding)

    def update_explore_module(self, module_id: str, updates: dict) -> dict:
        return self._explore_service().update_explore_module(module_id, updates)

    def add_explore_module(
        self, name: str, path: str, parent_id: str = "", description: str = ""
    ) -> dict:
        return self._explore_service().add_explore_module(
            name,
            path,
            parent_id,
            description,
        )

    def delete_explore_module(self, module_id: str) -> dict:
        return self._explore_service().delete_explore_module(module_id)

    def create_task_from_finding(self, run_id: str, finding_index: int) -> dict:
        return self._explore_service().create_task_from_finding(
            run_id,
            finding_index,
        )
