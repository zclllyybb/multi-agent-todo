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
from core.database import Database
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
            self._coder_by_complexity[level] = CoderAgent(
                model=model, client=self.client
            )
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
                registered,
                len(parent_children),
                len(self._pending_dispatch),
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
                line.strip()
                for line in branch_result.stdout.splitlines()
                if line.strip()
            }

        for wt in self.worktree_mgr.list_worktrees():
            raw_branch = wt.get("branch", "")
            if raw_branch.startswith("refs/heads/"):
                raw_branch = raw_branch[len("refs/heads/") :]
            raw_path = wt.get("path", "")
            if raw_branch and raw_path:
                branch_worktrees.setdefault(raw_branch, []).append(
                    os.path.abspath(raw_path)
                )

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
        clean_available = task.status in cleanable_statuses and (
            actual_branch_exists or actual_worktree_exists
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
            td["comment_count"] = len(task.comments)
            td["has_comments"] = bool(task.comments)
            td.update(self._task_resource_state(task, local_branches, branch_worktrees))
            result.append(td)
        return result

    def serialize_task_for_ui(self, task: Task) -> dict:
        """Serialize a single task with runtime resource-state fields for dashboard UI."""
        return self.serialize_tasks_for_ui([task])[0]

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
          explorer_model: str
          map_model: str
        """
        oc = self.config.setdefault("opencode", {})
        explore = self.config.setdefault("explore", {})

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
                cleaned = [
                    m.strip() for m in models if isinstance(m, str) and m.strip()
                ]
                self.reviewers = [
                    ReviewerAgent(model=m, client=self.client) for m in cleaned
                ]
                oc["reviewer_models"] = cleaned
                log.info("Updated reviewer models: %s", cleaned)

        if "explorer_model" in updates and updates["explorer_model"]:
            model = updates["explorer_model"].strip()
            explore["explorer_model"] = model
            log.info("Updated explorer model: %s", model)

        if "map_model" in updates and updates["map_model"]:
            model = updates["map_model"].strip()
            explore["map_model"] = model
            log.info("Updated map model: %s", model)

        # Persist model config changes so they survive restarts.
        self._save_model_config()

    def _get_jira_config(self) -> dict:
        jira = self.config.setdefault("jira", {})
        default_skill = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "skills",
            "jira-issue",
        )
        skill_path = jira.get("skill_path") or default_skill
        if not os.path.isabs(skill_path):
            skill_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                skill_path,
            )
        merged = {
            "url": jira.get("url", ""),
            "token": jira.get("token", ""),
            "user": jira.get("user", ""),
            "project_key": jira.get("project_key", ""),
            "epic": str(jira.get("epic", "")).strip(),
            "issue_types": (
                list(jira.get("issue_type", []))
                if isinstance(jira.get("issue_type", []), list)
                else [str(jira.get("issue_type", "")).strip()]
            ),
            "priorities": (
                list(jira.get("priority", []))
                if isinstance(jira.get("priority", []), list)
                else [str(jira.get("priority", "")).strip()]
            ),
            "routing_hints": [],
            "timeout": int(jira.get("timeout", 120) or 120),
            "skill_path": skill_path,
        }
        merged["issue_types"] = [v for v in merged["issue_types"] if str(v).strip()]
        merged["priorities"] = [v for v in merged["priorities"] if str(v).strip()]
        for raw_hint in jira.get("routing_hints", []):
            if not isinstance(raw_hint, dict):
                continue
            hint = dict(raw_hint)
            labels = hint.get("labels")
            if labels is None:
                hint.pop("labels", None)
            elif isinstance(labels, list):
                cleaned_labels = [str(v).strip() for v in labels if str(v).strip()]
                if cleaned_labels:
                    hint["labels"] = cleaned_labels
                else:
                    hint.pop("labels", None)
            else:
                single = str(labels).strip()
                if single:
                    hint["labels"] = [single]
                else:
                    hint.pop("labels", None)
            component = str(hint.get("component", "")).strip()
            if component:
                hint["component"] = component
            else:
                hint.pop("component", None)
            assignee = str(hint.get("assignee", "")).strip()
            if assignee:
                hint["assignee"] = assignee
            else:
                hint.pop("assignee", None)
            about = str(hint.get("about", "")).strip()
            if about:
                hint["about"] = about
            merged["routing_hints"].append(hint)
        return merged

    def _run_jira_agent(self, task: Task) -> tuple[AgentRun, str]:
        jira = self._get_jira_config()
        simple_agent = self._coder_by_complexity.get("simple") or self._default_coder
        source_task_id = (task.jira_source_task_id or task.id).strip()
        regression_cfg = self.config.get("regression", {})
        dry_run_enabled = bool(regression_cfg.get("dry_run_jira", False))
        component_hint_count = sum(
            1
            for hint in jira["routing_hints"]
            if str(hint.get("component", "")).strip()
        )
        log.info(
            "Preparing jira agent run: jira_task=%s source_task=%s model=%s project=%s epic=%s issue_type_candidates=%s priority_candidates=%s routing_hint_count=%d routing_component_count=%d skill_path=%s",
            task.id,
            source_task_id,
            simple_agent.model,
            jira["project_key"],
            jira["epic"] or "-",
            jira["issue_types"],
            jira["priorities"],
            len(jira["routing_hints"]),
            component_hint_count,
            jira["skill_path"],
        )
        prompt = coder_assign_jira_issue(
            source_task_id=source_task_id,
            title=task.title,
            description=task.description,
            project_key=jira["project_key"],
            jira_url=jira["url"],
            jira_epic=jira["epic"],
            available_issue_types=jira["issue_types"],
            available_priorities=jira["priorities"],
            routing_hints=jira["routing_hints"],
            dry_run=dry_run_enabled,
        )
        repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {
            "JIRA_URL": jira["url"],
            "JIRA_TOKEN": jira["token"],
            "JIRA_PROJECT": jira["project_key"],
        }
        if jira.get("user"):
            env["JIRA_USER"] = jira["user"]
        if dry_run_enabled:
            env["MULTI_AGENT_TODO_JIRA_DRY_RUN"] = "1"
        run = self.client.run(
            message=prompt,
            work_dir=repo_path,
            model=simple_agent.model,
            agent_type="jira_assign",
            task_id=task.id,
            max_continues=8,
            env=env,
            require_stop=True,
        )
        text = self.client.extract_last_text_block_or_raw(run.output).strip()
        log.info(
            "Jira agent run completed: jira_task=%s source_task=%s session=%s exit=%s extracted_text_len=%d",
            task.id,
            source_task_id,
            run.session_id or "-",
            run.exit_code,
            len(text),
        )
        return run, text

    def _parse_jira_agent_result(self, task: Task, text: str) -> dict:
        key = ""
        issue_url = ""
        payload_lines: list[str] = []
        for line in (text or "").splitlines():
            if line.startswith("key="):
                key = line.split("=", 1)[1].strip()
            elif line.startswith("self="):
                issue_url = line.split("=", 1)[1].strip()
            elif line.startswith("payload="):
                payload_lines.append(line.split("=", 1)[1])
        if not key:
            raise RuntimeError(
                f"Jira agent [{task.id}] did not return issue key. Output: {text[:500]}"
            )
        log.info(
            "Parsed jira agent result: jira_task=%s source_task=%s key=%s self=%s",
            task.id,
            task.jira_source_task_id or task.id,
            key,
            issue_url,
        )
        return {
            "key": key,
            "self": issue_url,
            "payload": "\n".join(payload_lines).strip(),
        }

    @staticmethod
    def _build_jira_browse_url(jira_base_url: str, issue_key: str) -> str:
        base = str(jira_base_url or "").strip().rstrip("/")
        key = str(issue_key or "").strip()
        if not base or not key:
            return ""
        return f"{base}/browse/{key}"

    def submit_jira_task(
        self,
        title: str,
        description: str,
        priority: str = "medium",
        source_task_id: str = "",
    ) -> Task:
        jira = self._get_jira_config()
        task = Task(
            title=title,
            description=description,
            priority=TaskPriority(priority),
            source=TaskSource.MANUAL,
            task_mode="jira",
            jira_source_task_id=source_task_id.strip(),
            max_retries=0,
        )
        task.plan_output = (
            f"Jira target: {jira['project_key'] or '-'} / epic={jira['epic'] or '-'} / issue types={jira['issue_types'] or ['-']} "
            f"/ priorities={jira['priorities'] or ['-']} / routing hints={len(jira['routing_hints'])}"
        )
        task.jira_status = "pending"
        self.db.save_task(task)
        log.info(
            "Submitted jira task: jira_task=%s source_task=%s priority=%s title=%s",
            task.id,
            task.jira_source_task_id or "-",
            task.priority.value,
            task.title,
        )
        dispatched = self._dispatch_jira_task(task.id)
        log.info(
            "Jira task dispatch result: jira_task=%s dispatched=%s",
            task.id,
            dispatched,
        )
        return task

    def assign_jira_for_task(self, source_task_id: str) -> dict:
        source_task = self.db.get_task(source_task_id)
        if not source_task:
            log.warning(
                "Assign jira requested for missing source task: %s", source_task_id
            )
            return {"error": "Task not found"}
        if source_task.task_mode == "jira":
            log.warning(
                "Assign jira rejected for jira-mode task: source_task=%s",
                source_task_id,
            )
            return {"error": "Cannot assign Jira from a jira-mode task"}

        log.info(
            "Assign jira requested: source_task=%s status=%s priority=%s title=%s existing_key=%s",
            source_task.id,
            source_task.status.value,
            source_task.priority.value,
            source_task.title,
            source_task.jira_issue_key or "-",
        )

        if source_task.status == TaskStatus.JIRA_ASSIGNING:
            log.warning(
                "Assign jira rejected for already-running task: source_task=%s",
                source_task.id,
            )
            return {"error": "Jira assignment already in progress for this task"}

        source_task.jira_status = "pending"
        source_task.jira_error = ""
        source_task.updated_at = time.time()
        self.db.save_task(source_task)
        dispatched = self._dispatch_jira_task(source_task.id)
        log.info(
            "Assign jira dispatched in-place: source_task=%s dispatched=%s",
            source_task.id,
            dispatched,
        )
        if not dispatched:
            source_task.jira_status = ""
            source_task.updated_at = time.time()
            self.db.save_task(source_task)
            return {"error": "Failed to dispatch Jira assignment"}
        return {"ok": True, "task": source_task.to_dict()}

    def _dispatch_jira_task(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._futures:
                log.warning("Jira task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self._futures) >= max_p:
                log.warning("Max parallel tasks reached for jira dispatch (%d)", max_p)
                return False
            future = self._pool.submit(self._jira_task_pipeline, task_id)
            self._futures[task_id] = future
            log.info("Dispatched jira task: jira_task=%s", task_id)
            return True

    def _jira_task_pipeline(self, task_id: str):
        task = self.db.get_task(task_id)
        if not task:
            log.error("Jira task not found: %s", task_id)
            return

        try:
            jira = self._get_jira_config()
            if not jira["url"]:
                raise RuntimeError("Jira config missing url")
            if not jira["token"]:
                raise RuntimeError("Jira config missing token")
            if not jira["project_key"]:
                raise RuntimeError("Jira config missing project_key")

            log.info(
                "Starting jira task pipeline: jira_task=%s source_task=%s current_status=%s project=%s",
                task.id,
                task.jira_source_task_id or "-",
                task.status.value,
                jira["project_key"],
            )

            task.status = TaskStatus.JIRA_ASSIGNING
            task.started_at = task.started_at or time.time()
            task.updated_at = time.time()
            task.jira_status = "assigning"
            task.jira_error = ""
            task.code_output = ""
            task.review_output = ""
            task.jira_agent_output = ""
            task.jira_payload_preview = ""
            self.db.save_task(task)

            agent_run, agent_text = self._run_jira_agent(task)
            self.db.save_agent_run(agent_run)
            task.jira_agent_output = agent_text
            if agent_run.session_id:
                task.session_ids.setdefault("coder", []).append(agent_run.session_id)
            task.updated_at = time.time()
            self.db.save_task(task)
            log.info(
                "Stored jira agent output: jira_task=%s session=%s output_len=%d",
                task.id,
                agent_run.session_id or "-",
                len(agent_text),
            )

            result = self._parse_jira_agent_result(task, agent_text)
            key = str(result.get("key", "")).strip()
            issue_url = self._build_jira_browse_url(jira["url"], key)
            task.jira_payload_preview = str(result.get("payload", "")).strip()

            task.jira_issue_key = key
            task.jira_issue_url = issue_url
            task.jira_status = "created"
            task.review_pass = True
            task.reviewer_results = []
            task.status = TaskStatus.COMPLETED
            task.completed_at = time.time()
            task.updated_at = time.time()
            self.db.save_task(task)

            if task.task_mode == "jira" and task.jira_source_task_id:
                source_task = self.db.get_task(task.jira_source_task_id)
                if source_task:
                    source_task.jira_issue_key = key
                    source_task.jira_issue_url = issue_url
                    source_task.jira_status = "created"
                    source_task.jira_error = ""
                    source_task.updated_at = time.time()
                    self.db.save_task(source_task)
                    log.info(
                        "Synced jira result back to source task: source_task=%s jira_task=%s key=%s",
                        source_task.id,
                        task.id,
                        key,
                    )
                else:
                    log.warning(
                        "Source task missing during jira success sync: source_task=%s jira_task=%s key=%s",
                        task.jira_source_task_id,
                        task.id,
                        key,
                    )

            self._update_parent_status(task.id)
            log.info(
                "Jira task completed: jira_task=%s source_task=%s key=%s url=%s",
                task.id,
                task.jira_source_task_id or "-",
                key or "-",
                issue_url or "-",
            )
        except Exception as e:
            log.error(
                "Jira task failed [%s]: %s\n%s", task_id, e, traceback.format_exc()
            )
            task = self.db.get_task(task_id)
            if task:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                task.jira_status = "failed"
                task.jira_error = str(e)
                task.updated_at = time.time()
                self.db.save_task(task)

                if task.task_mode == "jira" and task.jira_source_task_id:
                    source_task = self.db.get_task(task.jira_source_task_id)
                    if source_task:
                        source_task.jira_status = "failed"
                        source_task.jira_error = str(e)
                        source_task.updated_at = time.time()
                        self.db.save_task(source_task)
                        log.info(
                            "Synced jira failure back to source task: source_task=%s jira_task=%s error=%s",
                            source_task.id,
                            task.id,
                            str(e),
                        )
                    else:
                        log.warning(
                            "Source task missing during jira failure sync: source_task=%s jira_task=%s error=%s",
                            task.jira_source_task_id,
                            task.id,
                            str(e),
                        )

                self._update_parent_status(task_id)
        finally:
            with self._lock:
                self._futures.pop(task_id, None)

    def _save_model_config(self):
        """Write model config changes back to config.yaml preserving all comments/formatting.

        Strategy: parse the file line-by-line and replace only the scalar values
        for the model keys, leaving every comment, blank line, and other key
        intact. Multi-line structures (coder_model_by_complexity,
        reviewer_models) are replaced block-by-block.
        """
        meta = self.config.get("_meta", {}) if isinstance(self.config, dict) else {}
        config_path = meta.get("config_path") if isinstance(meta, dict) else None
        if not config_path:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config.yaml",
            )
        try:
            with open(config_path) as f:
                lines = f.readlines()

            oc = self.config["opencode"]
            explore = self.config.get("explore", {})
            new_lines = self._patch_yaml_lines(lines, oc, explore)

            with open(config_path, "w") as f:
                f.writelines(new_lines)
            log.info("Persisted model config to %s", config_path)
        except Exception as e:
            log.warning("Could not persist model config to %s: %s", config_path, e)

    @staticmethod
    def _patch_yaml_lines(
        lines: list, oc: dict, explore: Optional[dict] = None
    ) -> list:
        """Return a copy of lines with opencode model values patched in-place."""
        import re as _re

        explore = explore or {}
        result = list(lines)
        i = 0
        current_top_level_section = ""

        while i < len(result):
            line = result[i]
            stripped = line.rstrip()

            if stripped and not stripped.lstrip().startswith("#"):
                section_match = _re.match(
                    r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*$", stripped
                )
                if section_match:
                    current_top_level_section = section_match.group(1)

            # ── planner_model ──────────────────────────────────────────
            m = _re.match(r"^(\s*planner_model\s*:\s*)(.*)$", stripped)
            if m and current_top_level_section == "opencode" and "planner_model" in oc:
                result[i] = m.group(1) + oc["planner_model"] + "\n"
                i += 1
                continue

            # ── coder_model_default ────────────────────────────────────
            m = _re.match(r"^(\s*coder_model_default\s*:\s*)(.*)$", stripped)
            if (
                m
                and current_top_level_section == "opencode"
                and "coder_model_default" in oc
            ):
                result[i] = m.group(1) + oc["coder_model_default"] + "\n"
                i += 1
                continue

            # ── coder_model_by_complexity (block) ──────────────────────
            m = _re.match(r"^(\s*coder_model_by_complexity\s*:)", stripped)
            if (
                m
                and current_top_level_section == "opencode"
                and "coder_model_by_complexity" in oc
            ):
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
                    cm = _re.match(r"^(\s*)([a-zA-Z_]+)(\s*:\s*)(.*)$", orig.rstrip())
                    if cm and cm.group(2) in cmap:
                        new_block.append(
                            cm.group(1)
                            + cm.group(2)
                            + cm.group(3)
                            + cmap[cm.group(2)]
                            + "\n"
                        )
                    else:
                        new_block.append(orig)
                # add any new levels not present in original file
                existing_levels = set()
                for j in range(i + 1, block_end):
                    cm = _re.match(r"^\s*([a-zA-Z_]+)\s*:", result[j].rstrip())
                    if cm:
                        existing_levels.add(cm.group(1))
                for level, model in cmap.items():
                    if level not in existing_levels:
                        new_block.append(f"{child_indent}{level}: {model}\n")
                result[i:block_end] = new_block
                i += len(new_block)
                continue

            # ── reviewer_models (list block) ───────────────────────────
            m = _re.match(r"^(\s*reviewer_models\s*:)", stripped)
            if (
                m
                and current_top_level_section == "opencode"
                and "reviewer_models" in oc
            ):
                indent = len(line) - len(line.lstrip())
                block_end = i + 1
                while block_end < len(result):
                    nxt = result[block_end]
                    if nxt.strip() == "" or nxt.strip().startswith("#"):
                        block_end += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip())
                    if nxt_indent < indent:
                        break
                    if nxt_indent == indent and not nxt.lstrip().startswith("-"):
                        break
                    block_end += 1
                child_indent = " " * (indent + 2)
                new_block = [result[i]]  # keep "reviewer_models:" line
                for model in oc["reviewer_models"]:
                    new_block.append(f"{child_indent}- {model}\n")
                result[i:block_end] = new_block
                i += len(new_block)
                continue

            # ── explorer_model ─────────────────────────────────────────
            m = _re.match(r"^(\s*explorer_model\s*:\s*)(.*)$", stripped)
            if (
                m
                and current_top_level_section == "explore"
                and "explorer_model" in explore
            ):
                result[i] = m.group(1) + explore["explorer_model"] + "\n"
                i += 1
                continue

            # ── map_model ──────────────────────────────────────────────
            m = _re.match(r"^(\s*map_model\s*:\s*)(.*)$", stripped)
            if m and current_top_level_section == "explore" and "map_model" in explore:
                result[i] = m.group(1) + explore["map_model"] + "\n"
                i += 1
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
        if task.status not in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.REVIEW_FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.NEEDS_ARBITRATION,
        ):
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
            if child.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                child_result = self.cancel_task(child.id)
                if "error" in child_result:
                    log.warning(
                        "Failed to cascade cancel to child [%s]: %s",
                        child.id,
                        child_result["error"],
                    )
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
        if task.status not in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.REVIEW_FAILED,
            TaskStatus.NEEDS_ARBITRATION,
        ):
            return {"error": f"Cannot revise task in {task.status.value} state"}
        if task.task_mode == "jira":
            return {"error": "Revise is not supported for jira-mode tasks"}
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
        log.info(
            "Revise task [%s] (mode=%s) with manual feedback (%d chars)",
            task_id,
            task.task_mode,
            len(feedback),
        )
        log.debug("Task [%s] revised with feedback: %s", task_id, feedback)
        return {"ok": True, "task_id": task_id}

    def resume_task(self, task_id: str, message: str = "Continue") -> dict:
        """Resume a failed task from the last coder session with user input.

        This is intended for mid-run interruptions (timeout/process crash).
        The first resumed coder invocation sends *message* directly into the
        existing session (e.g. "Continue"), then proceeds with normal
        code→review flow.
        """
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        if task.status != TaskStatus.FAILED:
            return {"error": f"Cannot resume task in {task.status.value} state"}
        if task.task_mode != "develop":
            return {"error": "Resume is only supported for develop-mode tasks"}
        if not task.worktree_path:
            return {"error": "Task has no worktree; cannot resume coder session"}

        coder_session_id = self._latest_coder_session_id(task)
        if not coder_session_id:
            return {
                "error": (
                    "No coder session found for this task. "
                    "Use Revise with feedback to restart the loop."
                )
            }

        resume_message = (message or "").strip() or "Continue"

        manual_run = AgentRun(
            task_id=task_id,
            agent_type="manual_review",
            model="user",
            prompt="",
            output=resume_message,
            exit_code=0,
            duration_sec=0.0,
        )
        self.db.save_agent_run(manual_run)

        task.user_feedback = resume_message
        task.review_pass = False
        task.status = TaskStatus.PENDING
        task.error = ""
        task.completed_at = 0.0
        task.updated_at = time.time()
        self.db.save_task(task)

        if not self._dispatch_resume(task_id, resume_message):
            return {
                "error": "Task could not be resumed right now (already running or at parallel limit)"
            }

        log.info(
            "Resume task [%s] using coder session=%s message=%r",
            task_id,
            coder_session_id,
            resume_message,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "session_id": coder_session_id,
        }

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
        if task.status != TaskStatus.NEEDS_ARBITRATION:
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

    def _dispatch_resume(self, task_id: str, first_message: str) -> bool:
        """Submit a resume-task: continue coder session with a raw first message."""
        with self._lock:
            if task_id in self._futures:
                log.warning("Task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self._futures) >= max_p:
                log.warning("Max parallel tasks reached (%d)", max_p)
                return False
            future = self._pool.submit(
                self._revise_task_pipeline,
                task_id,
                first_message,
                True,
            )
            self._futures[task_id] = future
            log.info("Dispatched resume for task: %s", task_id)
            return True

    def _revise_task_pipeline(
        self,
        task_id: str,
        first_coder_message: str = "",
        first_message_raw: bool = False,
    ):
        """Coder→reviewer loop for a revised task (skips planning + worktree creation)."""
        task = self.db.get_task(task_id)
        if not task:
            log.error("Revise: task not found: %s", task_id)
            return

        try:
            worktree_path = task.worktree_path
            coder = self._coder_by_complexity.get(task.complexity, self._default_coder)
            log.info(
                "Revise [%s] using coder model=%s, worktree=%s",
                task.id,
                coder.model,
                worktree_path,
            )

            # Recover the last coder session id so the coder retains full context
            coder_session_id = self._latest_coder_session_id(task)

            # Read the user's manual feedback (stored separately from model review output)
            user_feedback = first_coder_message or task.user_feedback

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
                if attempt == 0 and first_message_raw:
                    code_run, code_text = coder.continue_session(
                        task,
                        worktree_path,
                        user_message=coder_feedback,
                        session_id=coder_session_id,
                    )
                else:
                    code_run, code_text = coder.retry_with_feedback(
                        task,
                        worktree_path,
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

                self._ensure_coder_run_success(code_run, attempt + 1)

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
                        task,
                        worktree_path,
                        revision_context=user_feedback,
                        coder_response=code_text,
                    )
                    self.db.save_agent_run(review_run)
                    reviewer_results.append(
                        {
                            "model": reviewer.model,
                            "passed": passed,
                            "output": review_text,
                        }
                    )
                    if review_run.session_id:
                        task.session_ids.setdefault("reviewer", []).append(
                            review_run.session_id
                        )
                    log.info(
                        "Revise [%s] reviewer(%s) passed=%s",
                        task.id,
                        reviewer.model,
                        passed,
                    )
                    if not passed:
                        all_passed = False
                        rejection_outputs.append(
                            f"=== Reviewer: {reviewer.model} | REQUEST_CHANGES ===\n"
                            + review_text
                        )
                        log.info(
                            "Revise [%s] short-circuiting after first rejection",
                            task.id,
                        )
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
                        log.info(
                            "Revise [%s] review failed, retrying (%d/%d)",
                            task.id,
                            attempt + 1,
                            task.max_retries,
                        )
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
                            task.id,
                            task.max_retries + 1,
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
        active = [
            t.to_dict()
            for t in tasks
            if t.status
            in (
                TaskStatus.PLANNING,
                TaskStatus.CODING,
                TaskStatus.JIRA_ASSIGNING,
                TaskStatus.REVIEWING,
            )
        ]
        return {
            "running": self.running,
            "total_tasks": len(tasks),
            "status_counts": status_counts,
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
                branch_name = (
                    f"agent/review-{task.id[:8]}-{slug}"
                    if slug
                    else f"agent/review-{task.id[:8]}"
                )
                hooks = self.config.get("repo", {}).get("worktree_hooks", [])
                worktree_path = self.worktree_mgr.create_worktree(
                    branch_name, hooks=hooks
                )
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
                    task,
                    worktree_path,
                    revision_context=revision_context,
                )
                self.db.save_agent_run(review_run)
                reviewer_results.append(
                    {
                        "model": reviewer.model,
                        "passed": passed,
                        "output": review_text,
                    }
                )
                if review_run.session_id:
                    task.session_ids.setdefault("reviewer", []).append(
                        review_run.session_id
                    )
                log.info(
                    "Review-only [%s] reviewer(%s) passed=%s",
                    task.id,
                    reviewer.model,
                    passed,
                )

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
            log.error(
                "Review-only failed [%s]: %s\n%s", task_id, e, traceback.format_exc()
            )
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
            log.info(
                "Removed review worktree for task [%s]: %s", task.id, task.branch_name
            )
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
        """Return the latest known coder session id for a task."""
        coder_sessions = task.session_ids.get("coder", [])
        if coder_sessions:
            return coder_sessions[-1]

        runs = self.db.get_runs_for_task(task.id)
        for run in sorted(runs, key=lambda r: r.created_at, reverse=True):
            if run.agent_type == "coder" and run.session_id:
                return run.session_id
        return ""

    def _ensure_coder_run_success(self, code_run: AgentRun, attempt: int):
        """Validate coder run result and raise an accurate failure error."""
        if code_run.exit_code == 0:
            if self.client.is_output_complete(code_run.output):
                return
            raise ModelOutputError(
                f"Coder output is incomplete — the last step has no "
                f"'stop' finish reason (session={code_run.session_id}, "
                f"attempt={attempt}). The model may have been truncated "
                f"or crashed mid-step."
            )

        timeout_marker = ""
        for line in reversed(code_run.output.splitlines()):
            if line.startswith("TIMEOUT after "):
                timeout_marker = line.strip()
                break
        if timeout_marker:
            raise RuntimeError(
                f"Coder run timed out: {timeout_marker} "
                f"(session={code_run.session_id}, attempt={attempt})."
            )

        raise RuntimeError(
            f"Coder run failed with exit_code={code_run.exit_code} "
            f"(session={code_run.session_id}, attempt={attempt})."
        )

    def _execute_task(self, task_id: str):
        """Full pipeline: plan → code → review (with retry)."""
        task = self.db.get_task(task_id)
        if not task:
            log.error("Task not found: %s", task_id)
            return
        if task.task_mode == "jira":
            self._jira_task_pipeline(task_id)
            return

        repo_path = self.config["repo"]["path"]
        try:
            # ── Phase 1: Planning (analyze + optionally split) ──
            task.status = TaskStatus.PLANNING
            task.started_at = time.time()
            task.updated_at = time.time()
            self.db.save_task(task)

            plan_run, is_split, plan_text, sub_tasks, complexity = (
                self._plan_with_retry(task, repo_path)
            )
            self.db.save_agent_run(plan_run)
            task.complexity = complexity
            if plan_run.session_id:
                task.session_ids.setdefault("planner", []).append(plan_run.session_id)
                log.info(
                    "Task [%s] planner session: %s (complexity=%s)",
                    task.id,
                    plan_run.session_id,
                    complexity,
                )

            if is_split and task.source == TaskSource.PLANNER:
                # Sub-tasks created by the planner must not be split further —
                # force single-task execution to avoid unbounded recursion.
                log.info(
                    "Task [%s] is a planner sub-task; ignoring split=true from planner "
                    "(would create recursive split). Treating as single task.",
                    task.id,
                )
                is_split = False
                plan_text = (
                    sub_tasks[0].get("description", plan_text)
                    if sub_tasks
                    else plan_text
                )

            if is_split:
                # Planner decided to decompose — create sub-tasks and mark parent done
                log.info("Task [%s] split into %d sub-tasks", task.id, len(sub_tasks))
                task.plan_output = (
                    f"Split into {len(sub_tasks)} sub-tasks:\n"
                    + "\n".join(f"- {st.get('title', '')}" for st in sub_tasks)
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
                        max_retries=int(
                            self.config.get("orchestrator", {}).get("max_retries", 4)
                        ),
                    )
                    children.append(child)
                # Pass 2: resolve depends_on indices → real IDs, persist
                # (raises ModelOutputError on invalid entries; already retried above)
                child_id_list = [c.id for c in children]
                resolved_deps = self.dep_tracker.resolve_indices(
                    child_id_list, sub_tasks
                )
                for child, resolved in zip(children, resolved_deps):
                    child.depends_on = resolved
                    self.db.save_task(child)
                    log.info(
                        "Created sub-task [%s] '%s' depends_on=%s",
                        child.id,
                        child.title,
                        resolved,
                    )
                self.dep_tracker.register(task.id, children)
                # Pass 3: dispatch unblocked sub-tasks
                for child in children:
                    if not self.dep_tracker.is_blocked(child.id):
                        self.dispatch_task(child.id)
                    else:
                        log.info(
                            "Sub-task [%s] '%s' blocked by deps=%s, waiting",
                            child.id,
                            child.title,
                            child.depends_on,
                        )
                task.status = (
                    TaskStatus.PLANNING
                )  # will be updated when sub-tasks finish
                task.updated_at = time.time()
                self.db.save_task(task)
                log.info(
                    "Task [%s] split into sub-tasks, waiting for children", task.id
                )
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
                        worktree_path,
                        dep_branches,
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
                            task.id,
                            len(dep_branches),
                            dep_branches,
                        )

            # Select coder model based on complexity assessed by planner
            coder = self._coder_by_complexity.get(task.complexity, self._default_coder)
            log.info(
                "Task [%s] using coder model=%s (complexity=%s)",
                task.id,
                coder.model,
                task.complexity,
            )

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
                        task,
                        worktree_path,
                        session_id=coder_session_id,
                        dep_context=dep_context,
                    )
                else:
                    # Retry in continued session: send only review feedback
                    code_run, code_text = coder.retry_with_feedback(
                        task,
                        worktree_path,
                        review_feedback=task.review_output,
                        session_id=coder_session_id,
                    )
                self.db.save_agent_run(code_run)
                task.code_output = code_text
                if code_run.session_id:
                    # Keep the same session id for all retry rounds
                    coder_session_id = code_run.session_id
                    task.session_ids.setdefault("coder", []).append(code_run.session_id)
                    log.info(
                        "Task [%s] coder session: %s (attempt %d)",
                        task.id,
                        code_run.session_id,
                        attempt + 1,
                    )
                task.updated_at = time.time()
                self.db.save_task(task)

                self._ensure_coder_run_success(code_run, attempt + 1)

                # Re-check cancellation before starting review
                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Task [%s] was cancelled before review, aborting", task_id)
                    return

                # Extract only the coder's final summary (last text block
                # before stop) — not the entire session transcript.
                coder_last_response = (
                    self.client.extract_last_text_block(code_run.output)
                    if attempt > 0
                    else ""
                )

                # ── Multi-Reviewer: short-circuit on first REQUEST_CHANGES ──
                task.status = TaskStatus.REVIEWING
                task.updated_at = time.time()
                self.db.save_task(task)

                reviewer_results = []
                rejection_outputs = []  # only from reviewers that rejected
                all_passed = True
                for reviewer in self.reviewers:
                    review_run, passed, review_text = reviewer.review_changes(
                        task,
                        worktree_path,
                        prior_rejections="\n\n".join(all_prior_rejections),
                        coder_response=coder_last_response,
                    )
                    self.db.save_agent_run(review_run)
                    reviewer_results.append(
                        {
                            "model": reviewer.model,
                            "passed": passed,
                            "output": review_text,
                        }
                    )
                    if review_run.session_id:
                        task.session_ids.setdefault("reviewer", []).append(
                            review_run.session_id
                        )
                    log.info(
                        "Task [%s] reviewer(%s) passed=%s",
                        task.id,
                        reviewer.model,
                        passed,
                    )
                    if not passed:
                        all_passed = False
                        rejection_outputs.append(
                            f"=== Reviewer: {reviewer.model} | REQUEST_CHANGES ===\n"
                            + review_text
                        )
                        # Short-circuit: don't run remaining reviewers
                        log.info(
                            "Task [%s] short-circuiting after first rejection", task.id
                        )
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
                            task.id,
                            attempt + 1,
                            task.max_retries,
                            coder_session_id,
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
                            task.id,
                            task.max_retries + 1,
                        )

        except Exception as e:
            log.error(
                "Task execution failed [%s]: %s\n%s", task_id, e, traceback.format_exc()
            )
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
            self._flush_pending_dispatches()

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
        log.info(
            "Parent task [%s] updated to %s based on sub-task results",
            parent.id,
            parent.status.value,
        )

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
        from agents.prompts import DEFAULT_EXPLORE_CATEGORIES

        return self.config.get("explore", {}).get(
            "categories", DEFAULT_EXPLORE_CATEGORIES
        )

    def _get_explorer_model(self) -> str:
        return self.config.get("explore", {}).get(
            "explorer_model",
            self.config["opencode"].get("planner_model", ""),
        )

    def _get_explore_variant(self) -> str:
        return str(self.config.get("explore", {}).get("variant", "")).strip()

    def _get_explore_parallel_limit(self) -> int:
        raw = self.config.get("explore", {}).get(
            "max_parallel_runs",
            self.config.get("orchestrator", {}).get("max_parallel_tasks", 1),
        )
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = 1
        return max(1, limit)

    def _repo_name(self) -> str:
        repo_path = self.config["repo"]["path"]
        base = os.path.basename(os.path.abspath(repo_path.rstrip("/")))
        return base or repo_path

    @staticmethod
    def _trim_stream_output(output: str, max_chars: int = 240000) -> str:
        if len(output) <= max_chars:
            return output
        return output[-max_chars:]

    def _default_explore_map_state(self) -> dict:
        now = time.time()
        return {
            "status": "idle",
            "started_at": 0.0,
            "finished_at": 0.0,
            "updated_at": now,
            "session_id": "",
            "model": "",
            "output": "",
            "error": "",
            "cancel_requested": False,
            "modules_created": 0,
            "map_review_required": False,
            "map_review_reason": "",
            "map_review_module_id": "",
            "map_review_category": "",
            "repo_name": self._repo_name(),
            "repo_path": self.config["repo"]["path"],
        }

    def _persist_explore_map_state(self):
        with self._lock:
            state = dict(self._explore_map_state)
        self.db.save_state(self._explore_map_state_key, state)

    def _load_explore_map_state(self):
        persisted = self.db.get_state(self._explore_map_state_key)
        default_state = self._default_explore_map_state()
        if persisted:
            default_state.update(persisted)
        elif self.db.get_all_explore_modules():
            default_state["status"] = "done"
            default_state["finished_at"] = time.time()

        if default_state.get("status") == "in_progress":
            default_state["status"] = "failed"
            default_state["error"] = "map init interrupted by daemon restart"
            default_state["finished_at"] = time.time()

        default_state["repo_name"] = self._repo_name()
        default_state["repo_path"] = self.config["repo"]["path"]
        default_state["cancel_requested"] = False
        default_state["map_review_required"] = bool(
            default_state.get("map_review_required", False)
        )
        default_state["map_review_reason"] = str(
            default_state.get("map_review_reason", "")
        )
        default_state["map_review_module_id"] = str(
            default_state.get("map_review_module_id", "")
        )
        default_state["map_review_category"] = str(
            default_state.get("map_review_category", "")
        )
        with self._lock:
            self._explore_map_state = default_state
        self._persist_explore_map_state()

    def reset_explore_state(self) -> dict:
        """Clear explore metadata while preserving already created tasks."""
        with self._lock:
            running_jobs = list(self._explore_running.values())
            self._explore_queue = []
            self._explore_running = {}
            self._explore_cancel_requested.clear()
            map_in_progress = self._explore_map_state.get("status") == "in_progress"
            self._explore_map_cancel_requested = False
            self._explore_map_state = self._default_explore_map_state()
            self._explore_map_future = None

        for job in running_jobs:
            task_id = str(job.get("task_id", ""))
            if task_id:
                self.client.kill_task(task_id)
        if map_in_progress:
            self.client.kill_task(self._explore_map_task_id)

        self.db.delete_all_explore_queue_jobs()
        self.db.delete_all_explore_runs()
        self.db.delete_all_explore_modules()
        self.db.delete_state(self._explore_map_state_key)
        self._persist_explore_map_state()
        return {
            "ok": True,
            "tasks_preserved": True,
            "map_init": self.get_explore_init_state(),
        }

    def _persist_explore_job(self, job: dict):
        payload = {k: v for k, v in job.items() if not str(k).startswith("_")}
        self.db.save_explore_queue_job(payload)

    def _recover_explore_queue_jobs(self):
        persisted_jobs = self.db.get_explore_queue_jobs()
        if not persisted_jobs:
            self._recover_stuck_exploration()
            return

        valid_jobs: List[dict] = []
        active_keys: Set[str] = set()
        for job in persisted_jobs:
            module_id = str(job.get("module_id", ""))
            category = str(job.get("category", ""))
            if not module_id or not category:
                job_id = str(job.get("job_id", ""))
                if job_id:
                    self.db.delete_explore_queue_job(job_id)
                continue

            module = self.db.get_explore_module(module_id)
            if not module or category not in module.category_status:
                job_id = str(job.get("job_id", ""))
                if job_id:
                    self.db.delete_explore_queue_job(job_id)
                continue

            key = self._explore_job_key(module_id, category)
            if key in active_keys:
                job_id = str(job.get("job_id", ""))
                if job_id:
                    self.db.delete_explore_queue_job(job_id)
                continue

            active_keys.add(key)
            valid_jobs.append(job)

        self._recover_stuck_exploration(active_keys=active_keys)

        if not valid_jobs:
            return

        now = time.time()
        with self._lock:
            for job in valid_jobs:
                prev_state = str(job.get("state", "queued"))
                qid = int(job.get("queue_id", 0) or 0)
                if qid <= 0:
                    qid = self._next_explore_seq_locked()
                    job["queue_id"] = qid
                else:
                    self._explore_seq = max(self._explore_seq, qid)

                job.setdefault("job_id", uuid.uuid4().hex)
                job.setdefault(
                    "personality_key",
                    self._pick_personality_for_category(job["category"]),
                )
                job.setdefault("queued_at", now)
                job.setdefault("started_at", 0.0)
                job.setdefault("session_id", "")
                job.setdefault("focus_point", "")
                job.setdefault(
                    "task_id", f"__explore__:{job['module_id']}:{job['category']}"
                )
                job["state"] = "queued"
                job["resume_with_continue"] = (
                    bool(job.get("session_id")) and prev_state == "running"
                )
                if not job.get("queued_at"):
                    job["queued_at"] = now
                job["started_at"] = 0.0

                module = self.db.get_explore_module(job["module_id"])
                if (
                    module
                    and module.category_status.get(job["category"])
                    != ExploreStatus.IN_PROGRESS.value
                ):
                    module.category_status[job["category"]] = (
                        ExploreStatus.IN_PROGRESS.value
                    )
                    module.updated_at = now
                    self.db.save_explore_module(module)

                self._explore_queue.append(job)
                self._persist_explore_job(job)

            to_submit = self._dispatch_explore_queue_locked()

        self._submit_explore_jobs(to_submit)
        log.warning("Recovered %d exploration queue job(s) from DB", len(valid_jobs))

    def is_explore_map_ready(self) -> bool:
        with self._lock:
            init_status = self._explore_map_state.get("status", "idle")
        if init_status == "in_progress":
            return False
        return bool(self.db.get_all_explore_modules())

    def get_explore_init_state(self) -> dict:
        with self._lock:
            state = dict(self._explore_map_state)
        output = state.get("output", "")
        readable = self.client.format_readable_text(output) if output else ""
        if not isinstance(readable, str):
            readable = str(readable)
        state["readable_output"] = readable
        state["map_ready"] = self.is_explore_map_ready()
        return state

    def get_explore_status(self) -> dict:
        return {
            "repo_name": self._repo_name(),
            "repo_path": self.config["repo"]["path"],
            "categories": self._get_explore_categories(),
            "map_ready": self.is_explore_map_ready(),
            "map_init": self.get_explore_init_state(),
        }

    @staticmethod
    def _explore_job_key(module_id: str, category: str) -> str:
        return f"{module_id}:{category}"

    def _list_target_modules_for_explore(
        self,
        module_ids: Optional[List[str]],
        leaf_only_when_empty: bool = True,
    ) -> List[ExploreModule]:
        all_modules = self.db.get_all_explore_modules()
        if module_ids:
            selected = set(module_ids)
            return [m for m in all_modules if m.id in selected]
        if not leaf_only_when_empty:
            return all_modules
        child_parent_ids = {m.parent_id for m in all_modules if m.parent_id}
        return [m for m in all_modules if m.id not in child_parent_ids]

    def _validate_explore_categories(
        self, categories: Optional[List[str]]
    ) -> tuple[List[str], List[str]]:
        configured = self._get_explore_categories()
        configured_set = set(configured)
        if not categories:
            return configured, []
        requested = [c for c in categories if isinstance(c, str) and c.strip()]
        valid = [c for c in requested if c in configured_set]
        invalid = [c for c in requested if c not in configured_set]
        return valid, invalid

    def _next_explore_seq_locked(self) -> int:
        self._explore_seq += 1
        return self._explore_seq

    def _set_module_category_status(
        self, module_id: str, category: str, status: str, note: Optional[str] = None
    ):
        module = self.db.get_explore_module(module_id)
        if not module:
            return
        module.category_status[category] = status
        if note is not None:
            module.category_notes[category] = note
        module.updated_at = time.time()
        self.db.save_explore_module(module)

    @staticmethod
    def _append_explore_note(
        existing: str, new_note: str, max_chars: int = 8000
    ) -> str:
        if not existing.strip():
            merged = new_note.strip()
        elif not new_note.strip():
            merged = existing.strip()
        else:
            merged = f"{existing.strip()}\n\n{new_note.strip()}"
        if len(merged) <= max_chars:
            return merged
        return merged[-max_chars:]

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
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        parts = [f"[{ts}]"]
        if focus_point:
            parts.append(f"focus: {focus_point}")
        if actionability_score >= 0:
            parts.append(f"actionability: {actionability_score:.1f}/10")
        if reliability_score >= 0:
            parts.append(f"reliability: {reliability_score:.1f}/10")
        if explored_scope:
            parts.append(f"explored: {explored_scope}")
        if completion_status:
            parts.append(f"completion: {completion_status}")
        if summary:
            parts.append(f"summary: {summary}")
        if supplemental_note:
            parts.append(f"note: {supplemental_note}")
        return " | ".join(parts)

    @staticmethod
    def _build_map_review_prompt(review_reason: str) -> str:
        return (
            "Please review and update the repository module map based on the new "
            f"exploration signal: {review_reason}\n"
            "Re-check module boundaries, split/merge opportunities, and naming. "
            "Return the full latest module map JSON in the same schema as map initialization."
        )

    def _request_explore_map_review(
        self, module: ExploreModule, category: str, reason: str
    ):
        review_reason = reason.strip() or (
            f"Explorer requested module structure review for {module.name} ({module.path}) in {category}."
        )
        now = time.time()
        with self._lock:
            self._explore_map_state.update(
                {
                    "status": "review_required",
                    "updated_at": now,
                    "map_review_required": True,
                    "map_review_reason": review_reason,
                    "map_review_module_id": module.id,
                    "map_review_category": category,
                }
            )
        self._persist_explore_map_state()

        review_result = self.start_init_explore_map(review_reason=review_reason)
        if not review_result.get("accepted", False):
            log.warning(
                "Map review was requested but init-map could not start now: %s",
                review_result.get("error", "unknown"),
            )

    def _is_explore_cancel_requested(self, key: str) -> bool:
        with self._lock:
            return key in self._explore_cancel_requested

    def _clear_explore_cancel_flag(self, key: str):
        with self._lock:
            self._explore_cancel_requested.discard(key)

    def _dispatch_explore_queue_locked(self) -> List[dict]:
        to_submit: List[dict] = []
        while (
            self._explore_queue
            and len(self._explore_running) < self._explore_parallel_limit
        ):
            job = self._explore_queue.pop(0)
            key = self._explore_job_key(job["module_id"], job["category"])
            job["state"] = "running"
            job["started_at"] = time.time()
            self._explore_running[key] = job
            self._persist_explore_job(job)
            to_submit.append(job)
        return to_submit

    def _submit_explore_jobs(self, jobs: List[dict]):
        for job in jobs:
            self._pool.submit(self._run_exploration_job, job)

    def _run_exploration_job(self, job: dict):
        key = self._explore_job_key(job["module_id"], job["category"])
        next_jobs: List[dict] = []
        try:
            self._run_exploration(
                job["module_id"],
                job["category"],
                job["personality_key"],
                job=job,
            )
        finally:
            self.db.delete_explore_queue_job(job["job_id"])
            with self._lock:
                self._explore_running.pop(key, None)
                next_jobs = self._dispatch_explore_queue_locked()
            self._submit_explore_jobs(next_jobs)

    def get_exploration_queue_state(self) -> dict:
        with self._lock:
            queued_jobs = [dict(j) for j in self._explore_queue]
            running_jobs = [dict(j) for j in self._explore_running.values()]

        def _decorate(job: dict) -> dict:
            module = self.db.get_explore_module(job["module_id"])
            status = "unknown"
            if module:
                status = module.category_status.get(job["category"], "unknown")
            return {
                "queue_id": job.get("queue_id", 0),
                "module_id": job["module_id"],
                "module_name": module.name if module else "(deleted)",
                "module_path": module.path if module else "",
                "category": job["category"],
                "personality_key": job["personality_key"],
                "state": job.get("state", "queued"),
                "queued_at": job.get("queued_at", 0.0),
                "started_at": job.get("started_at", 0.0),
                "session_id": job.get("session_id", ""),
                "focus_point": job.get("focus_point", ""),
                "category_status": status,
            }

        queued = [_decorate(j) for j in queued_jobs]
        running = [_decorate(j) for j in running_jobs]
        queued.sort(key=lambda x: x["queue_id"])
        running.sort(key=lambda x: x["started_at"])
        return {
            "max_parallel_runs": self._explore_parallel_limit,
            "running": running,
            "queued": queued,
            "counts": {
                "running": len(running),
                "queued": len(queued),
                "total": len(running) + len(queued),
            },
        }

    def cancel_exploration(
        self,
        module_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        include_running: bool = True,
    ) -> dict:
        modules = self._list_target_modules_for_explore(
            module_ids,
            leaf_only_when_empty=False,
        )
        cats, invalid_categories = self._validate_explore_categories(categories)
        if not cats:
            return {
                "cancelled": 0,
                "cancelled_running": 0,
                "cancelled_queued": 0,
                "reset_stale": 0,
                "invalid_categories": invalid_categories,
            }

        module_map = {m.id: m for m in modules}
        target_keys = {
            self._explore_job_key(m.id, cat)
            for m in modules
            for cat in cats
            if cat in m.category_status
        }

        cancelled_queued = 0
        cancelled_running = 0
        next_jobs: List[dict] = []
        with self._lock:
            kept = []
            for job in self._explore_queue:
                key = self._explore_job_key(job["module_id"], job["category"])
                if key in target_keys:
                    cancelled_queued += 1
                    self.db.delete_explore_queue_job(job["job_id"])
                else:
                    kept.append(job)
            self._explore_queue = kept

            for key in target_keys:
                if key in self._explore_running and include_running:
                    self._explore_cancel_requested.add(key)
                    cancelled_running += 1
                    running_job = self._explore_running[key]
                    task_id = running_job.get("task_id", "")
                    if task_id:
                        self.client.kill_task(task_id)

            next_jobs = self._dispatch_explore_queue_locked()

        self._submit_explore_jobs(next_jobs)

        for key in target_keys:
            if key in self._explore_running:
                continue
            module_id, category = key.split(":", 1)
            module = module_map.get(module_id) or self.db.get_explore_module(module_id)
            if not module:
                continue
            if module.category_status.get(category) == ExploreStatus.IN_PROGRESS.value:
                self._set_module_category_status(
                    module_id, category, ExploreStatus.TODO.value, ""
                )

        reset_stale = 0
        for module in module_map.values():
            changed = False
            for cat in cats:
                if module.category_status.get(cat) == ExploreStatus.IN_PROGRESS.value:
                    key = self._explore_job_key(module.id, cat)
                    if key not in self._explore_running:
                        module.category_status[cat] = ExploreStatus.TODO.value
                        module.category_notes[cat] = ""
                        reset_stale += 1
                        changed = True
            if changed:
                module.updated_at = time.time()
                self.db.save_explore_module(module)

        cancelled = cancelled_queued + cancelled_running + reset_stale
        return {
            "cancelled": cancelled,
            "cancelled_running": cancelled_running,
            "cancelled_queued": cancelled_queued,
            "reset_stale": reset_stale,
            "invalid_categories": invalid_categories,
            "queue": self.get_exploration_queue_state(),
        }

    def _apply_explore_map(
        self, run: AgentRun, modules_data: List[dict], model: str
    ) -> int:
        agent_run = AgentRun(
            task_id=self._explore_map_task_id,
            agent_type="explorer_map_init",
            model=model,
            prompt=run.prompt,
            output=run.output,
            exit_code=run.exit_code,
            duration_sec=run.duration_sec,
            session_id=run.session_id,
        )
        self.db.save_agent_run(agent_run)

        self.db.delete_all_explore_modules()
        categories = self._get_explore_categories()

        def _create_modules(
            items: List[dict], parent_id: str = "", depth: int = 0
        ) -> int:
            created = 0
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
                created += 1
                children = item.get("children", [])
                if children:
                    created += _create_modules(children, mod.id, depth + 1)
            return created

        created_count = _create_modules(modules_data)
        return created_count

    def init_explore_map(self) -> dict:
        """Synchronous map-init entrypoint (used by tests)."""
        repo_path = self.config["repo"]["path"]
        model = self.config.get("explore", {}).get(
            "map_model", self._get_explorer_model()
        )
        variant = self._get_explore_variant()
        explorer = ExplorerAgent(model=model, client=self.client)
        log.info(
            "Starting explore map init: model=%s variant=%s", model, variant or "-"
        )

        try:
            run, modules_data = explorer.init_map(repo_path, agent_variant=variant)
            modules_created = self._apply_explore_map(run, modules_data, model)
            with self._lock:
                self._explore_map_state.update(
                    {
                        "status": "done",
                        "started_at": time.time() - run.duration_sec,
                        "finished_at": time.time(),
                        "updated_at": time.time(),
                        "session_id": run.session_id,
                        "model": model,
                        "output": self._trim_stream_output(run.output),
                        "error": "",
                        "cancel_requested": False,
                        "modules_created": modules_created,
                        "map_review_required": False,
                        "map_review_reason": "",
                        "map_review_module_id": "",
                        "map_review_category": "",
                    }
                )
            self._persist_explore_map_state()
            log.info("Explore map initialized: %d modules created", modules_created)
            return {"modules_created": modules_created}
        except Exception as e:
            with self._lock:
                self._explore_map_state.update(
                    {
                        "status": "failed",
                        "finished_at": time.time(),
                        "updated_at": time.time(),
                        "error": str(e),
                        "cancel_requested": False,
                    }
                )
            self._persist_explore_map_state()
            log.error("Map init failed: %s", e)
            return {"error": str(e)}

    def _start_init_explore_map(self, review_reason: str = "") -> dict:
        with self._lock:
            if self._explore_map_state.get("status") == "in_progress":
                return {
                    "accepted": False,
                    "error": "Map initialization already in progress",
                    "state": dict(self._explore_map_state),
                }

            model = self.config.get("explore", {}).get(
                "map_model", self._get_explorer_model()
            )
            variant = self._get_explore_variant()
            now = time.time()
            review_reason = review_reason.strip()
            review_message = (
                self._build_map_review_prompt(review_reason) if review_reason else ""
            )
            review_session_id = (
                str(self._explore_map_state.get("session_id", ""))
                if review_message
                else ""
            )
            self._explore_map_cancel_requested = False
            self._explore_map_state.update(
                {
                    "status": "in_progress",
                    "started_at": now,
                    "finished_at": 0.0,
                    "updated_at": now,
                    "session_id": "",
                    "model": model,
                    "variant": variant,
                    "output": "",
                    "error": "",
                    "cancel_requested": False,
                    "modules_created": 0,
                    "map_review_required": bool(review_message),
                    "map_review_reason": review_reason,
                    "map_review_module_id": "",
                    "map_review_category": "",
                }
            )
            self._explore_map_future = self._pool.submit(
                self._run_init_explore_map_job,
                model,
                variant,
                review_message,
                review_session_id,
            )

        self._persist_explore_map_state()
        return {"accepted": True, "state": self.get_explore_init_state()}

    def start_init_explore_map(self, review_reason: str = "") -> dict:
        return self._start_init_explore_map(review_reason=review_reason)

    def reinitialize_explore_map(self, review_reason: str = "") -> dict:
        reset = self.reset_explore_state()
        result = self._start_init_explore_map(review_reason=review_reason)
        result["reset"] = reset
        return result

    def cancel_init_explore_map(self) -> dict:
        with self._lock:
            in_progress = self._explore_map_state.get("status") == "in_progress"
            self._explore_map_cancel_requested = in_progress
            if in_progress:
                self._explore_map_state["cancel_requested"] = True
                self._explore_map_state["updated_at"] = time.time()

        if in_progress:
            self.client.kill_task(self._explore_map_task_id)
            self._persist_explore_map_state()
        return {
            "cancel_requested": bool(in_progress),
            "state": self.get_explore_init_state(),
        }

    def _run_init_explore_map_job(
        self,
        model: str,
        variant: str = "",
        review_message: str = "",
        review_session_id: str = "",
    ):
        repo_path = self.config["repo"]["path"]
        explorer = ExplorerAgent(model=model, client=self.client)
        last_persist_at = 0.0

        def _on_output(chunk: str, sid: str):
            nonlocal last_persist_at
            now = time.time()
            with self._lock:
                output = self._explore_map_state.get("output", "") + chunk
                self._explore_map_state["output"] = self._trim_stream_output(output)
                if sid:
                    self._explore_map_state["session_id"] = sid
                self._explore_map_state["updated_at"] = now
            if now - last_persist_at >= 0.5:
                self._persist_explore_map_state()
                last_persist_at = now

        try:
            run, modules_data = explorer.init_map_streaming(
                repo_path=repo_path,
                task_id=self._explore_map_task_id,
                session_id=review_session_id,
                message_override=review_message or None,
                on_output=_on_output,
                should_cancel=lambda: self._explore_map_cancel_requested,
                agent_variant=variant,
            )
            modules_created = self._apply_explore_map(run, modules_data, model)
            now = time.time()
            with self._lock:
                self._explore_map_cancel_requested = False
                self._explore_map_state.update(
                    {
                        "status": "done",
                        "finished_at": now,
                        "updated_at": now,
                        "session_id": run.session_id,
                        "output": self._trim_stream_output(run.output),
                        "error": "",
                        "cancel_requested": False,
                        "modules_created": modules_created,
                        "map_review_required": False,
                        "map_review_reason": "",
                        "map_review_module_id": "",
                        "map_review_category": "",
                    }
                )
            self._persist_explore_map_state()
            log.info("Explore map initialized: %d modules created", modules_created)
        except Exception as e:
            now = time.time()
            cancelled = self._explore_map_cancel_requested
            with self._lock:
                self._explore_map_cancel_requested = False
                self._explore_map_state.update(
                    {
                        "status": "cancelled" if cancelled else "failed",
                        "finished_at": now,
                        "updated_at": now,
                        "error": "" if cancelled else str(e),
                        "cancel_requested": False,
                    }
                )
            self._persist_explore_map_state()
            if cancelled:
                log.info("Map init cancelled")
            else:
                log.error("Map init failed: %s", e)

    def start_exploration(
        self,
        module_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        focus_point: str = "",
    ) -> dict:
        """Start exploration on selected modules x categories.

        Picks leaf modules with TODO cells if *module_ids* is empty.
        Returns ``{"started": N}``.
        """
        modules = self._list_target_modules_for_explore(module_ids)
        cats, invalid_categories = self._validate_explore_categories(categories)
        if not self.is_explore_map_ready():
            return {
                "started": 0,
                "queued": 0,
                "running": len(self._explore_running),
                "rejected_in_progress": 0,
                "skipped_non_todo": 0,
                "invalid_categories": invalid_categories,
                "error": "Explore map is not ready. Initialize map first.",
                "map_ready": False,
                "queue": self.get_exploration_queue_state(),
            }
        if not cats:
            return {
                "started": 0,
                "queued": 0,
                "running": len(self._explore_running),
                "rejected_in_progress": 0,
                "skipped_non_todo": 0,
                "invalid_categories": invalid_categories,
                "queue": self.get_exploration_queue_state(),
            }

        started = 0
        queued_now = 0
        rejected_in_progress = 0
        skipped_non_todo = 0
        focus_point = str(focus_point or "").strip()

        next_jobs: List[dict] = []
        with self._lock:
            for mod in modules:
                for cat in cats:
                    fresh = self.db.get_explore_module(mod.id)
                    if not fresh:
                        continue
                    status = fresh.category_status.get(cat)
                    if status == ExploreStatus.IN_PROGRESS.value:
                        rejected_in_progress += 1
                        continue
                    if status not in {
                        ExploreStatus.TODO.value,
                        ExploreStatus.DONE.value,
                        ExploreStatus.STALE.value,
                    }:
                        skipped_non_todo += 1
                        continue

                    fresh.category_status[cat] = ExploreStatus.IN_PROGRESS.value
                    fresh.updated_at = time.time()
                    self.db.save_explore_module(fresh)

                    personality_key = self._pick_personality_for_category(cat)
                    job = {
                        "job_id": uuid.uuid4().hex,
                        "queue_id": self._next_explore_seq_locked(),
                        "module_id": mod.id,
                        "category": cat,
                        "personality_key": personality_key,
                        "task_id": f"__explore__:{mod.id}:{cat}",
                        "state": "queued",
                        "queued_at": time.time(),
                        "started_at": 0.0,
                        "session_id": "",
                        "focus_point": focus_point,
                        "resume_with_continue": False,
                    }
                    self._explore_queue.append(job)
                    self._persist_explore_job(job)
                    started += 1
                    queued_now += 1

            next_jobs = self._dispatch_explore_queue_locked()
            running_now = len(self._explore_running)

        self._submit_explore_jobs(next_jobs)

        log.info(
            "Exploration scheduling: started=%d queued_now=%d running_now=%d "
            "rejected_in_progress=%d skipped_non_todo=%d",
            started,
            queued_now,
            running_now,
            rejected_in_progress,
            skipped_non_todo,
        )
        return {
            "started": started,
            "queued": queued_now,
            "running": running_now,
            "rejected_in_progress": rejected_in_progress,
            "skipped_non_todo": skipped_non_todo,
            "invalid_categories": invalid_categories,
            "focus_point": focus_point,
            "queue": self.get_exploration_queue_state(),
        }

    def _pick_personality_for_category(self, category: str) -> str:
        """Select a personality whose ``category`` matches the given one."""
        from agents.prompts import EXPLORER_PERSONALITIES

        candidates = [
            key
            for key, info in EXPLORER_PERSONALITIES.items()
            if info.get("category") == category
        ]
        if candidates:
            return random.choice(candidates)
        # Fallback: pick any
        return random.choice(list(EXPLORER_PERSONALITIES.keys()))

    def _run_exploration(
        self,
        module_id: str,
        category: str,
        personality_key: str,
        job: Optional[dict] = None,
    ):
        """Execute a single exploration run (called in thread pool)."""
        from agents.prompts import EXPLORER_PERSONALITIES

        key = self._explore_job_key(module_id, category)
        if self._is_explore_cancel_requested(key):
            self._set_module_category_status(
                module_id,
                category,
                ExploreStatus.TODO.value,
                "",
            )
            self._clear_explore_cancel_flag(key)
            return

        try:
            module = self.db.get_explore_module(module_id)
            assert module is not None, f"module {module_id} vanished from DB"
            personality = EXPLORER_PERSONALITIES[personality_key]
            repo_path = self.config["repo"]["path"]
            model = self._get_explorer_model()
            variant = self._get_explore_variant()
            task_id = f"__explore__:{module_id}:{category}"
            session_id = ""
            focus_point = ""
            prior_note = str(module.category_notes.get(category, ""))
            resume_with_continue = False
            if job is not None:
                task_id = str(job.get("task_id", task_id))
                session_id = str(job.get("session_id", ""))
                focus_point = str(job.get("focus_point", "")).strip()
                resume_with_continue = bool(job.get("resume_with_continue", False))

            explorer = ExplorerAgent(model=model, client=self.client)
            log.info(
                "Starting exploration run: module=%s category=%s model=%s variant=%s personality=%s",
                module.path,
                category,
                model,
                variant or "-",
                personality_key,
            )
            stream_mode = job is not None and isinstance(self.client, OpenCodeClient)
            if not stream_mode:
                run, findings, summary = explorer.explore_module(
                    module=module,
                    category=category,
                    personality_focus=personality["focus"],
                    personality_name=personality["name"],
                    repo_path=repo_path,
                    focus_point=focus_point,
                    prior_note=prior_note,
                    agent_variant=variant,
                )
            else:
                persist_box = {"last": 0.0}

                def _on_output(_chunk: str, sid: str):
                    now = time.time()
                    if sid and sid != job.get("session_id"):
                        job["session_id"] = sid
                    if now - persist_box["last"] >= 0.5:
                        self._persist_explore_job(job)
                        persist_box["last"] = now

                run, findings, summary = explorer.explore_module_streaming(
                    module=module,
                    category=category,
                    personality_focus=personality["focus"],
                    personality_name=personality["name"],
                    repo_path=repo_path,
                    focus_point=focus_point,
                    prior_note=prior_note,
                    task_id=task_id,
                    session_id=session_id,
                    message_override="Continue"
                    if (resume_with_continue and session_id)
                    else None,
                    on_output=_on_output,
                    agent_variant=variant,
                    should_cancel=lambda: self._is_explore_cancel_requested(key),
                )

            if job is not None and run.session_id:
                job["session_id"] = run.session_id
                self._persist_explore_job(job)

            if run.exit_code == -2:
                module = self.db.get_explore_module(module_id)
                if module:
                    module.category_status[category] = ExploreStatus.TODO.value
                    module.category_notes[category] = ""
                    module.updated_at = time.time()
                    self.db.save_explore_module(module)
                self._clear_explore_cancel_flag(key)
                log.info(
                    "Exploration cancelled: module=%s category=%s", module_id, category
                )
                return

            run_text = run.output if isinstance(run.output, str) else ""
            metadata = {
                "summary": summary,
                "focus_point": focus_point,
                "actionability_score": -1.0,
                "reliability_score": -1.0,
                "explored_scope": "",
                "completion_status": "complete",
                "supplemental_note": "",
                "map_review_required": False,
                "map_review_reason": "",
            }
            try:
                parsed_meta = ExplorerAgent.parse_output_metadata(run_text)
                metadata.update(parsed_meta)
            except ModelOutputError as meta_err:
                log.warning(
                    "Explore metadata parse failed: module=%s category=%s err=%s",
                    module_id,
                    category,
                    meta_err,
                )

            summary = metadata["summary"] or summary

            # Save ExploreRun
            explore_run = ExploreRun(
                module_id=module_id,
                category=category,
                personality=personality_key,
                model=model,
                prompt=run.prompt,
                output=run.output,
                session_id=run.session_id,
                focus_point=metadata["focus_point"] or focus_point,
                actionability_score=metadata["actionability_score"],
                reliability_score=metadata["reliability_score"],
                explored_scope=metadata["explored_scope"],
                completion_status=metadata["completion_status"],
                supplemental_note=metadata["supplemental_note"],
                map_review_required=metadata["map_review_required"],
                map_review_reason=metadata["map_review_reason"],
                findings=findings,
                summary=summary,
                issue_count=len(findings),
                exit_code=run.exit_code,
                duration_sec=run.duration_sec,
            )
            self.db.save_explore_run(explore_run)

            if self._is_explore_cancel_requested(key):
                module = self.db.get_explore_module(module_id)
                if module:
                    module.category_status[category] = ExploreStatus.TODO.value
                    module.category_notes[category] = ""
                    module.updated_at = time.time()
                    self.db.save_explore_module(module)
                self._clear_explore_cancel_flag(key)
                log.info(
                    "Exploration cancelled after run completion: module=%s category=%s",
                    module_id,
                    category,
                )
                return

            # Update module status
            module = self.db.get_explore_module(module_id)
            if module is None:
                log.warning(
                    "Explore result could not be applied because module vanished: module=%s category=%s",
                    module_id,
                    category,
                )
                return
            module.category_status[category] = (
                ExploreStatus.DONE.value
                if metadata["completion_status"] == "complete"
                else ExploreStatus.STALE.value
            )
            note_entry = self._build_explore_note_entry(
                summary=summary,
                focus_point=metadata["focus_point"] or focus_point,
                actionability_score=metadata["actionability_score"],
                reliability_score=metadata["reliability_score"],
                explored_scope=metadata["explored_scope"],
                completion_status=metadata["completion_status"],
                supplemental_note=metadata["supplemental_note"],
            )
            module.category_notes[category] = self._append_explore_note(
                module.category_notes.get(category, ""),
                note_entry,
            )
            module.updated_at = time.time()
            self.db.save_explore_module(module)

            if metadata["map_review_required"]:
                self._request_explore_map_review(
                    module=module,
                    category=category,
                    reason=metadata["map_review_reason"],
                )

            # Auto-create tasks for severe findings
            auto_severity = self.config.get("explore", {}).get(
                "auto_task_severity", "major"
            )
            severity_levels = ["critical", "major", "minor", "info"]
            threshold_idx = (
                severity_levels.index(auto_severity)
                if auto_severity in severity_levels
                else 1
            )
            for finding in findings:
                sev = finding.get("severity", "info")
                if sev in severity_levels[: threshold_idx + 1]:
                    self._create_explore_task(module, category, finding)

            log.info(
                "Exploration complete: module=%s category=%s findings=%d",
                module.name,
                category,
                len(findings),
            )

        except Exception as e:
            log.error(
                "Exploration failed: module=%s category=%s: %s\n%s",
                module_id,
                category,
                e,
                traceback.format_exc(),
            )
            # Reset status so it can be retried
            module = self.db.get_explore_module(module_id)
            if module:
                module.category_status[category] = ExploreStatus.TODO.value
                module.updated_at = time.time()
                self.db.save_explore_module(module)
        finally:
            self._clear_explore_cancel_flag(key)

    @staticmethod
    def _build_explore_task(
        module_name: str, module_path: str, category: str, finding: dict
    ) -> "Task":
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
                TaskPriority.HIGH
                if finding["severity"] == "critical"
                else TaskPriority.MEDIUM
            ),
            source=TaskSource.EXPLORE,
            file_path=finding.get("file_path", ""),
            line_number=finding.get("line_number", 0),
        )

    def _create_explore_task(self, module: ExploreModule, category: str, finding: dict):
        """Create and persist a Task from an exploration finding."""
        task = self._build_explore_task(module.name, module.path, category, finding)
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

    def add_explore_module(
        self, name: str, path: str, parent_id: str = "", description: str = ""
    ) -> dict:
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
        log.info(
            "Created task [%s] from explore run [%s] finding #%d",
            task.id,
            run_id,
            finding_index,
        )
        return task.to_dict()
