"""Explore-domain orchestration logic extracted from Orchestrator."""

import logging
import os
import random
import time
import traceback
import uuid
from typing import List, Optional, Set

from agents.explorer import ExplorerAgent
from core.model_config import ModelSpec
from core.models import (
    AgentRun,
    ExploreModule,
    ExploreRun,
    ExploreStatus,
    ModelOutputError,
    Task,
    TaskPriority,
    TaskSource,
)
from core.opencode_client import OpenCodeClient

log = logging.getLogger(__name__)


class ExploreService:
    """Own explore queueing, map init, and finding-to-task flows."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    @property
    def config(self):
        return self.orchestrator.config

    @property
    def db(self):
        return self.orchestrator.db

    @property
    def client(self):
        return self.orchestrator.client

    def get_explore_categories(self) -> List[str]:
        from agents.prompts import DEFAULT_EXPLORE_CATEGORIES

        return list(DEFAULT_EXPLORE_CATEGORIES)

    def normalize_module_categories(
        self, module: ExploreModule, persist: bool = False
    ) -> ExploreModule:
        configured = self.get_explore_categories()
        normalized_status = {
            cat: module.category_status.get(cat, ExploreStatus.TODO.value)
            for cat in configured
        }
        normalized_notes = {
            cat: module.category_notes.get(cat, "")
            for cat in configured
        }
        changed = (
            normalized_status != module.category_status
            or normalized_notes != module.category_notes
        )
        if changed:
            module.category_status = normalized_status
            module.category_notes = normalized_notes
            module.updated_at = time.time()
            if persist:
                self.db.save_explore_module(module)
        return module

    def get_explore_module(self, module_id: str, persist: bool = True) -> Optional[ExploreModule]:
        module = self.db.get_explore_module(module_id)
        if not module:
            return None
        return self.normalize_module_categories(module, persist=persist)

    def get_all_explore_modules(self, persist: bool = True) -> List[ExploreModule]:
        modules = self.db.get_all_explore_modules()
        return [self.normalize_module_categories(m, persist=persist) for m in modules]

    def get_explorer_model(self) -> str:
        return self.get_explorer_spec().model

    def get_explorer_spec(self) -> ModelSpec:
        return self.orchestrator._explorer_spec_from_config(self.config)

    def get_map_spec(self) -> ModelSpec:
        return self.orchestrator._map_spec_from_config(self.config)

    def get_explore_parallel_limit(self) -> int:
        raw = self.config.get("explore", {}).get(
            "max_parallel_runs",
            self.config.get("orchestrator", {}).get("max_parallel_tasks", 1),
        )
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = 1
        return max(1, limit)

    def repo_name(self) -> str:
        repo_path = self.config["repo"]["path"]
        base = os.path.basename(os.path.abspath(repo_path.rstrip("/")))
        return base or repo_path

    @staticmethod
    def trim_stream_output(output: str, max_chars: int = 240000) -> str:
        if len(output) <= max_chars:
            return output
        return output[-max_chars:]

    def default_explore_map_state(self) -> dict:
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
            "repo_name": self.repo_name(),
            "repo_path": self.config["repo"]["path"],
        }

    def persist_explore_map_state(self):
        with self.orchestrator._lock:
            state = dict(self.orchestrator._explore_map_state)
        self.db.save_state(self.orchestrator._explore_map_state_key, state)

    def load_explore_map_state(self):
        persisted = self.db.get_state(self.orchestrator._explore_map_state_key)
        default_state = self.orchestrator._default_explore_map_state()
        if persisted:
            default_state.update(persisted)
        elif self.get_all_explore_modules(persist=False):
            default_state["status"] = "done"
            default_state["finished_at"] = time.time()

        if default_state.get("status") == "in_progress":
            default_state["status"] = "failed"
            default_state["error"] = "map init interrupted by daemon restart"
            default_state["finished_at"] = time.time()

        default_state["repo_name"] = self.orchestrator._repo_name()
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
        with self.orchestrator._lock:
            self.orchestrator._explore_map_state = default_state
        self.orchestrator._persist_explore_map_state()

    def reset_explore_state(self) -> dict:
        """Clear explore metadata while preserving already created tasks."""
        with self.orchestrator._lock:
            running_jobs = list(self.orchestrator._explore_running.values())
            self.orchestrator._explore_queue = []
            self.orchestrator._explore_running = {}
            self.orchestrator._explore_cancel_requested.clear()
            map_in_progress = (
                self.orchestrator._explore_map_state.get("status") == "in_progress"
            )
            self.orchestrator._explore_map_cancel_requested = False
            self.orchestrator._explore_map_state = (
                self.orchestrator._default_explore_map_state()
            )
            self.orchestrator._explore_map_future = None

        for job in running_jobs:
            task_id = str(job.get("task_id", ""))
            if task_id:
                self.client.kill_task(task_id)
        if map_in_progress:
            self.client.kill_task(self.orchestrator._explore_map_task_id)

        self.db.delete_all_explore_queue_jobs()
        self.db.delete_all_explore_runs()
        self.db.delete_all_explore_modules()
        self.db.delete_state(self.orchestrator._explore_map_state_key)
        self.orchestrator._persist_explore_map_state()
        return {
            "ok": True,
            "tasks_preserved": True,
            "map_init": self.orchestrator.get_explore_init_state(),
        }

    def persist_explore_job(self, job: dict):
        payload = {k: v for k, v in job.items() if not str(k).startswith("_")}
        self.db.save_explore_queue_job(payload)

    def recover_explore_queue_jobs(self):
        persisted_jobs = self.db.get_explore_queue_jobs()
        if not persisted_jobs:
            self.orchestrator._recover_stuck_exploration()
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

            module = self.get_explore_module(module_id)
            if not module or category not in module.category_status:
                job_id = str(job.get("job_id", ""))
                if job_id:
                    self.db.delete_explore_queue_job(job_id)
                continue

            key = self.orchestrator._explore_job_key(module_id, category)
            if key in active_keys:
                job_id = str(job.get("job_id", ""))
                if job_id:
                    self.db.delete_explore_queue_job(job_id)
                continue

            active_keys.add(key)
            valid_jobs.append(job)

        self.orchestrator._recover_stuck_exploration(active_keys=active_keys)

        if not valid_jobs:
            return

        now = time.time()
        with self.orchestrator._lock:
            for job in valid_jobs:
                prev_state = str(job.get("state", "queued"))
                qid = int(job.get("queue_id", 0) or 0)
                if qid <= 0:
                    qid = self.orchestrator._next_explore_seq_locked()
                    job["queue_id"] = qid
                else:
                    self.orchestrator._explore_seq = max(
                        self.orchestrator._explore_seq, qid
                    )

                job.setdefault("job_id", uuid.uuid4().hex)
                job.setdefault(
                    "personality_key",
                    self.orchestrator._pick_personality_for_category(job["category"]),
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

                module = self.get_explore_module(job["module_id"])
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

                self.orchestrator._explore_queue.append(job)
                self.orchestrator._persist_explore_job(job)

            to_submit = self.orchestrator._dispatch_explore_queue_locked()

        self.orchestrator._submit_explore_jobs(to_submit)
        log.warning("Recovered %d exploration queue job(s) from DB", len(valid_jobs))

    def is_explore_map_ready(self) -> bool:
        with self.orchestrator._lock:
            init_status = self.orchestrator._explore_map_state.get("status", "idle")
        if init_status == "in_progress":
            return False
        return bool(self.get_all_explore_modules(persist=False))

    def get_explore_init_state(self) -> dict:
        with self.orchestrator._lock:
            state = dict(self.orchestrator._explore_map_state)
        output = state.get("output", "")
        readable = self.client.format_readable_text(output) if output else ""
        if not isinstance(readable, str):
            readable = str(readable)
        state["readable_output"] = readable
        state["map_ready"] = self.orchestrator.is_explore_map_ready()
        return state

    def get_explore_status(self) -> dict:
        return {
            "repo_name": self.orchestrator._repo_name(),
            "repo_path": self.config["repo"]["path"],
            "categories": self.orchestrator._get_explore_categories(),
            "map_ready": self.orchestrator.is_explore_map_ready(),
            "map_init": self.orchestrator.get_explore_init_state(),
        }

    @staticmethod
    def explore_job_key(module_id: str, category: str) -> str:
        return f"{module_id}:{category}"

    def list_target_modules_for_explore(
        self,
        module_ids: Optional[List[str]],
        leaf_only_when_empty: bool = True,
    ) -> List[ExploreModule]:
        all_modules = self.get_all_explore_modules()
        if module_ids:
            selected = set(module_ids)
            return [m for m in all_modules if m.id in selected]
        if not leaf_only_when_empty:
            return all_modules
        child_parent_ids = {m.parent_id for m in all_modules if m.parent_id}
        return [m for m in all_modules if m.id not in child_parent_ids]

    def validate_explore_categories(
        self, categories: Optional[List[str]]
    ) -> tuple[List[str], List[str]]:
        configured = self.orchestrator._get_explore_categories()
        configured_set = set(configured)
        if not categories:
            return configured, []
        requested = [c for c in categories if isinstance(c, str) and c.strip()]
        valid = [c for c in requested if c in configured_set]
        invalid = [c for c in requested if c not in configured_set]
        return valid, invalid

    def next_explore_seq_locked(self) -> int:
        self.orchestrator._explore_seq += 1
        return self.orchestrator._explore_seq

    def set_module_category_status(
        self, module_id: str, category: str, status: str, note: Optional[str] = None
    ):
        module = self.get_explore_module(module_id)
        if not module:
            return
        module.category_status[category] = status
        if note is not None:
            module.category_notes[category] = note
        module.updated_at = time.time()
        self.db.save_explore_module(module)

    @staticmethod
    def append_explore_note(existing: str, new_note: str, max_chars: int = 8000) -> str:
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
    def build_explore_note_entry(
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
    def build_map_review_prompt(review_reason: str) -> str:
        return (
            "Please review and update the repository module map based on the new "
            f"exploration signal: {review_reason}\n"
            "Re-check module boundaries, split/merge opportunities, and naming. "
            "Return the full latest module map JSON in the same schema as map initialization."
        )

    def request_explore_map_review(
        self, module: ExploreModule, category: str, reason: str
    ):
        review_reason = reason.strip() or (
            f"Explorer requested module structure review for {module.name} ({module.path}) in {category}."
        )
        now = time.time()
        with self.orchestrator._lock:
            self.orchestrator._explore_map_state.update(
                {
                    "status": "review_required",
                    "updated_at": now,
                    "map_review_required": True,
                    "map_review_reason": review_reason,
                    "map_review_module_id": module.id,
                    "map_review_category": category,
                }
            )
        self.orchestrator._persist_explore_map_state()

        review_result = self.orchestrator.start_init_explore_map(
            review_reason=review_reason
        )
        if not review_result.get("accepted", False):
            log.warning(
                "Map review was requested but init-map could not start now: %s",
                review_result.get("error", "unknown"),
            )

    def is_explore_cancel_requested(self, key: str) -> bool:
        with self.orchestrator._lock:
            return key in self.orchestrator._explore_cancel_requested

    def clear_explore_cancel_flag(self, key: str):
        with self.orchestrator._lock:
            self.orchestrator._explore_cancel_requested.discard(key)

    def dispatch_explore_queue_locked(self) -> List[dict]:
        to_submit: List[dict] = []
        while (
            self.orchestrator._explore_queue
            and len(self.orchestrator._explore_running)
            < self.orchestrator._explore_parallel_limit
        ):
            job = self.orchestrator._explore_queue.pop(0)
            key = self.orchestrator._explore_job_key(job["module_id"], job["category"])
            job["state"] = "running"
            job["started_at"] = time.time()
            self.orchestrator._explore_running[key] = job
            self.orchestrator._persist_explore_job(job)
            to_submit.append(job)
        return to_submit

    def submit_explore_jobs(self, jobs: List[dict]):
        for job in jobs:
            self.orchestrator._pool.submit(self.orchestrator._run_exploration_job, job)

    def run_exploration_job(self, job: dict):
        key = self.orchestrator._explore_job_key(job["module_id"], job["category"])
        next_jobs: List[dict] = []
        try:
            self.orchestrator._run_exploration(
                job["module_id"],
                job["category"],
                job["personality_key"],
                job=job,
            )
        finally:
            self.db.delete_explore_queue_job(job["job_id"])
            with self.orchestrator._lock:
                self.orchestrator._explore_running.pop(key, None)
                next_jobs = self.orchestrator._dispatch_explore_queue_locked()
            self.orchestrator._submit_explore_jobs(next_jobs)

    def get_exploration_queue_state(self) -> dict:
        with self.orchestrator._lock:
            queued_jobs = [dict(j) for j in self.orchestrator._explore_queue]
            running_jobs = [
                dict(j) for j in self.orchestrator._explore_running.values()
            ]

        def _decorate(job: dict) -> dict:
            module = self.get_explore_module(job["module_id"])
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
            "max_parallel_runs": self.orchestrator._explore_parallel_limit,
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
        modules = self.orchestrator._list_target_modules_for_explore(
            module_ids,
            leaf_only_when_empty=False,
        )
        cats, invalid_categories = self.orchestrator._validate_explore_categories(
            categories
        )
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
            self.orchestrator._explore_job_key(m.id, cat)
            for m in modules
            for cat in cats
            if cat in m.category_status
        }

        cancelled_queued = 0
        cancelled_running = 0
        next_jobs: List[dict] = []
        with self.orchestrator._lock:
            kept = []
            for job in self.orchestrator._explore_queue:
                key = self.orchestrator._explore_job_key(
                    job["module_id"], job["category"]
                )
                if key in target_keys:
                    cancelled_queued += 1
                    self.db.delete_explore_queue_job(job["job_id"])
                else:
                    kept.append(job)
            self.orchestrator._explore_queue = kept

            for key in target_keys:
                if key in self.orchestrator._explore_running and include_running:
                    self.orchestrator._explore_cancel_requested.add(key)
                    cancelled_running += 1
                    running_job = self.orchestrator._explore_running[key]
                    task_id = running_job.get("task_id", "")
                    if task_id:
                        self.client.kill_task(task_id)

            next_jobs = self.orchestrator._dispatch_explore_queue_locked()

        self.orchestrator._submit_explore_jobs(next_jobs)

        for key in target_keys:
            if key in self.orchestrator._explore_running:
                continue
            module_id, category = key.split(":", 1)
            module = module_map.get(module_id) or self.get_explore_module(module_id)
            if not module:
                continue
            if module.category_status.get(category) == ExploreStatus.IN_PROGRESS.value:
                self.orchestrator._set_module_category_status(
                    module_id,
                    category,
                    ExploreStatus.TODO.value,
                    "",
                )

        reset_stale = 0
        for module in module_map.values():
            changed = False
            for cat in cats:
                if module.category_status.get(cat) == ExploreStatus.IN_PROGRESS.value:
                    key = self.orchestrator._explore_job_key(module.id, cat)
                    if key not in self.orchestrator._explore_running:
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
            "queue": self.orchestrator.get_exploration_queue_state(),
        }

    def apply_explore_map(
        self, run: AgentRun, modules_data: List[dict], model: str
    ) -> int:
        agent_run = AgentRun(
            task_id=self.orchestrator._explore_map_task_id,
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
        categories = self.orchestrator._get_explore_categories()

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

        return _create_modules(modules_data)

    def init_explore_map(self) -> dict:
        """Synchronous map-init entrypoint (used by tests)."""
        repo_path = self.config["repo"]["path"]
        spec = self.get_map_spec()
        model = spec.model
        explorer = ExplorerAgent(
            model=spec.model, variant=spec.variant, agent=spec.agent, client=self.client
        )
        log.info(
            "Starting explore map init: model=%s variant=%s", model, spec.variant or "-"
        )

        try:
            run, modules_data = explorer.init_map(repo_path)
            modules_created = self.orchestrator._apply_explore_map(
                run, modules_data, model
            )
            with self.orchestrator._lock:
                self.orchestrator._explore_map_state.update(
                    {
                        "status": "done",
                        "started_at": time.time() - run.duration_sec,
                        "finished_at": time.time(),
                        "updated_at": time.time(),
                        "session_id": run.session_id,
                        "model": model,
                        "output": self.orchestrator._trim_stream_output(run.output),
                        "error": "",
                        "cancel_requested": False,
                        "modules_created": modules_created,
                        "map_review_required": False,
                        "map_review_reason": "",
                        "map_review_module_id": "",
                        "map_review_category": "",
                    }
                )
            self.orchestrator._persist_explore_map_state()
            log.info("Explore map initialized: %d modules created", modules_created)
            return {"modules_created": modules_created}
        except Exception as e:
            with self.orchestrator._lock:
                self.orchestrator._explore_map_state.update(
                    {
                        "status": "failed",
                        "finished_at": time.time(),
                        "updated_at": time.time(),
                        "error": str(e),
                        "cancel_requested": False,
                    }
                )
            self.orchestrator._persist_explore_map_state()
            log.error("Map init failed: %s", e)
            return {"error": str(e)}

    def start_init_explore_map(self, review_reason: str = "") -> dict:
        with self.orchestrator._lock:
            if self.orchestrator._explore_map_state.get("status") == "in_progress":
                return {
                    "accepted": False,
                    "error": "Map initialization already in progress",
                    "state": dict(self.orchestrator._explore_map_state),
                }

            spec = self.get_map_spec()
            model = spec.model
            variant = spec.variant
            now = time.time()
            review_reason = review_reason.strip()
            review_message = (
                self.orchestrator._build_map_review_prompt(review_reason)
                if review_reason
                else ""
            )
            review_session_id = (
                str(self.orchestrator._explore_map_state.get("session_id", ""))
                if review_message
                else ""
            )
            self.orchestrator._explore_map_cancel_requested = False
            self.orchestrator._explore_map_state.update(
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
            self.orchestrator._explore_map_future = self.orchestrator._pool.submit(
                self.orchestrator._run_init_explore_map_job,
                model,
                variant,
                review_message,
                review_session_id,
            )

        self.orchestrator._persist_explore_map_state()
        return {"accepted": True, "state": self.orchestrator.get_explore_init_state()}

    def reinitialize_explore_map(self, review_reason: str = "") -> dict:
        reset = self.orchestrator.reset_explore_state()
        result = self.orchestrator._start_init_explore_map(review_reason=review_reason)
        result["reset"] = reset
        return result

    def cancel_init_explore_map(self) -> dict:
        with self.orchestrator._lock:
            in_progress = (
                self.orchestrator._explore_map_state.get("status") == "in_progress"
            )
            self.orchestrator._explore_map_cancel_requested = in_progress
            if in_progress:
                self.orchestrator._explore_map_state["cancel_requested"] = True
                self.orchestrator._explore_map_state["updated_at"] = time.time()

        if in_progress:
            self.client.kill_task(self.orchestrator._explore_map_task_id)
            self.orchestrator._persist_explore_map_state()
        return {
            "cancel_requested": bool(in_progress),
            "state": self.orchestrator.get_explore_init_state(),
        }

    def run_init_explore_map_job(
        self,
        model: str,
        variant: str = "",
        review_message: str = "",
        review_session_id: str = "",
    ):
        repo_path = self.config["repo"]["path"]
        explorer = ExplorerAgent(model=model, variant=variant, client=self.client)
        last_persist_at = 0.0

        def _on_output(chunk: str, sid: str):
            nonlocal last_persist_at
            now = time.time()
            with self.orchestrator._lock:
                output = self.orchestrator._explore_map_state.get("output", "") + chunk
                self.orchestrator._explore_map_state["output"] = (
                    self.orchestrator._trim_stream_output(output)
                )
                if sid:
                    self.orchestrator._explore_map_state["session_id"] = sid
                self.orchestrator._explore_map_state["updated_at"] = now
            if now - last_persist_at >= 0.5:
                self.orchestrator._persist_explore_map_state()
                last_persist_at = now

        try:
            run, modules_data = explorer.init_map_streaming(
                repo_path=repo_path,
                task_id=self.orchestrator._explore_map_task_id,
                session_id=review_session_id,
                message_override=review_message or None,
                on_output=_on_output,
                should_cancel=lambda: self.orchestrator._explore_map_cancel_requested,
            )
            modules_created = self.orchestrator._apply_explore_map(
                run, modules_data, model
            )
            now = time.time()
            with self.orchestrator._lock:
                self.orchestrator._explore_map_cancel_requested = False
                self.orchestrator._explore_map_state.update(
                    {
                        "status": "done",
                        "finished_at": now,
                        "updated_at": now,
                        "session_id": run.session_id,
                        "output": self.orchestrator._trim_stream_output(run.output),
                        "error": "",
                        "cancel_requested": False,
                        "modules_created": modules_created,
                        "map_review_required": False,
                        "map_review_reason": "",
                        "map_review_module_id": "",
                        "map_review_category": "",
                    }
                )
            self.orchestrator._persist_explore_map_state()
            log.info("Explore map initialized: %d modules created", modules_created)
        except Exception as e:
            now = time.time()
            cancelled = self.orchestrator._explore_map_cancel_requested
            with self.orchestrator._lock:
                self.orchestrator._explore_map_cancel_requested = False
                self.orchestrator._explore_map_state.update(
                    {
                        "status": "cancelled" if cancelled else "failed",
                        "finished_at": now,
                        "updated_at": now,
                        "error": "" if cancelled else str(e),
                        "cancel_requested": False,
                    }
                )
            self.orchestrator._persist_explore_map_state()
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
        """Start exploration on selected modules x categories."""
        modules = self.orchestrator._list_target_modules_for_explore(module_ids)
        cats, invalid_categories = self.orchestrator._validate_explore_categories(
            categories
        )
        if not self.orchestrator.is_explore_map_ready():
            return {
                "started": 0,
                "queued": 0,
                "running": len(self.orchestrator._explore_running),
                "rejected_in_progress": 0,
                "skipped_non_todo": 0,
                "invalid_categories": invalid_categories,
                "error": "Explore map is not ready. Initialize map first.",
                "map_ready": False,
                "queue": self.orchestrator.get_exploration_queue_state(),
            }
        if not cats:
            return {
                "started": 0,
                "queued": 0,
                "running": len(self.orchestrator._explore_running),
                "rejected_in_progress": 0,
                "skipped_non_todo": 0,
                "invalid_categories": invalid_categories,
                "queue": self.orchestrator.get_exploration_queue_state(),
            }

        started = 0
        queued_now = 0
        rejected_in_progress = 0
        skipped_non_todo = 0
        focus_point = str(focus_point or "").strip()

        next_jobs: List[dict] = []
        with self.orchestrator._lock:
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

                    personality_key = self.orchestrator._pick_personality_for_category(
                        cat
                    )
                    job = {
                        "job_id": uuid.uuid4().hex,
                        "queue_id": self.orchestrator._next_explore_seq_locked(),
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
                    self.orchestrator._explore_queue.append(job)
                    self.orchestrator._persist_explore_job(job)
                    started += 1
                    queued_now += 1

            next_jobs = self.orchestrator._dispatch_explore_queue_locked()
            running_now = len(self.orchestrator._explore_running)

        self.orchestrator._submit_explore_jobs(next_jobs)

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
            "queue": self.orchestrator.get_exploration_queue_state(),
        }

    def pick_personality_for_category(self, category: str) -> str:
        """Select a personality whose `category` matches the given one."""
        from agents.prompts import EXPLORER_PERSONALITIES

        candidates = [
            key
            for key, info in EXPLORER_PERSONALITIES.items()
            if info.get("category") == category
        ]
        if candidates:
            return random.choice(candidates)
        return random.choice(list(EXPLORER_PERSONALITIES.keys()))

    def run_exploration(
        self,
        module_id: str,
        category: str,
        personality_key: str,
        job: Optional[dict] = None,
    ):
        """Execute a single exploration run (called in thread pool)."""
        from agents.prompts import EXPLORER_PERSONALITIES

        key = self.orchestrator._explore_job_key(module_id, category)
        if self.orchestrator._is_explore_cancel_requested(key):
            self.orchestrator._set_module_category_status(
                module_id,
                category,
                ExploreStatus.TODO.value,
                "",
            )
            self.orchestrator._clear_explore_cancel_flag(key)
            return

        try:
            module = self.get_explore_module(module_id)
            assert module is not None, f"module {module_id} vanished from DB"
            personality = EXPLORER_PERSONALITIES[personality_key]
            repo_path = self.config["repo"]["path"]
            spec = self.get_explorer_spec()
            model = spec.model
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

            explorer = ExplorerAgent(
                model=spec.model, variant=spec.variant, agent=spec.agent, client=self.client
            )
            log.info(
                "Starting exploration run: module=%s category=%s model=%s variant=%s personality=%s",
                module.path,
                category,
                model,
                spec.variant or "-",
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
                )
            else:
                persist_box = {"last": 0.0}

                def _on_output(_chunk: str, sid: str):
                    now = time.time()
                    if sid and sid != job.get("session_id"):
                        job["session_id"] = sid
                    if now - persist_box["last"] >= 0.5:
                        self.orchestrator._persist_explore_job(job)
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
                    should_cancel=lambda: (
                        self.orchestrator._is_explore_cancel_requested(key)
                    ),
                )

            if job is not None and run.session_id:
                job["session_id"] = run.session_id
                self.orchestrator._persist_explore_job(job)

            if run.exit_code == -2:
                module = self.get_explore_module(module_id)
                if module:
                    module.category_status[category] = ExploreStatus.TODO.value
                    module.category_notes[category] = ""
                    module.updated_at = time.time()
                    self.db.save_explore_module(module)
                self.orchestrator._clear_explore_cancel_flag(key)
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

            if self.orchestrator._is_explore_cancel_requested(key):
                module = self.get_explore_module(module_id)
                if module:
                    module.category_status[category] = ExploreStatus.TODO.value
                    module.category_notes[category] = ""
                    module.updated_at = time.time()
                    self.db.save_explore_module(module)
                self.orchestrator._clear_explore_cancel_flag(key)
                log.info(
                    "Exploration cancelled after run completion: module=%s category=%s",
                    module_id,
                    category,
                )
                return

            module = self.get_explore_module(module_id)
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
            note_entry = self.orchestrator._build_explore_note_entry(
                summary=summary,
                focus_point=metadata["focus_point"] or focus_point,
                actionability_score=metadata["actionability_score"],
                reliability_score=metadata["reliability_score"],
                explored_scope=metadata["explored_scope"],
                completion_status=metadata["completion_status"],
                supplemental_note=metadata["supplemental_note"],
            )
            module.category_notes[category] = self.orchestrator._append_explore_note(
                module.category_notes.get(category, ""),
                note_entry,
            )
            module.updated_at = time.time()
            self.db.save_explore_module(module)

            if metadata["map_review_required"]:
                self.orchestrator._request_explore_map_review(
                    module=module,
                    category=category,
                    reason=metadata["map_review_reason"],
                )

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
                    self.orchestrator._create_explore_task(module, category, finding)

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
            module = self.get_explore_module(module_id)
            if module:
                module.category_status[category] = ExploreStatus.TODO.value
                module.updated_at = time.time()
                self.db.save_explore_module(module)
        finally:
            self.orchestrator._clear_explore_cancel_flag(key)

    @staticmethod
    def build_explore_task(
        module_name: str, module_path: str, category: str, finding: dict
    ) -> Task:
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

    def create_explore_task(self, module: ExploreModule, category: str, finding: dict):
        """Create and persist a Task from an exploration finding."""
        task = self.orchestrator._build_explore_task(
            module.name,
            module.path,
            category,
            finding,
        )
        self.db.save_task(task)
        log.info("Created explore task [%s]: %s", task.id, task.title)

    def update_explore_module(self, module_id: str, updates: dict) -> dict:
        """Update an explore module's editable fields."""
        module = self.get_explore_module(module_id)
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
        depth = 0
        if parent_id:
            parent = self.get_explore_module(parent_id)
            if not parent:
                return {"error": "Parent module not found"}
            depth = parent.depth + 1

        categories = self.orchestrator._get_explore_categories()
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
        module = self.get_explore_module(module_id)
        if not module:
            return {"error": "Module not found"}
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
        module = self.get_explore_module(explore_run.module_id)
        module_name = module.name if module else "unknown"
        module_path = module.path if module else ""

        task = self.orchestrator._build_explore_task(
            module_name,
            module_path,
            explore_run.category,
            finding,
        )
        self.db.save_task(task)
        log.info(
            "Created task [%s] from explore run [%s] finding #%d",
            task.id,
            run_id,
            finding_index,
        )
        return task.to_dict()
