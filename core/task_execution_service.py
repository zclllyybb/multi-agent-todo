"""Task execution domain logic extracted from Orchestrator."""

import logging
import re
import time
import traceback
from dataclasses import dataclass
from typing import List

from agents.reviewer import ReviewerAgent
from core.models import (
    AgentRun,
    ModelOutputError,
    Task,
    TaskPriority,
    TaskSource,
    TaskStatus,
)

log = logging.getLogger(__name__)


@dataclass
class ReviseContext:
    manual_feedback: str = ""
    prior_reviewer_feedback: str = ""

    @property
    def reviewer_revision_context(self) -> str:
        return self.manual_feedback

    @property
    def coder_retry_feedback(self) -> str:
        parts = []
        if self.manual_feedback:
            parts.append(self.manual_feedback)
        if self.prior_reviewer_feedback:
            parts.append(self.prior_reviewer_feedback)
        return "\n\n".join(parts)


class TaskExecutionService:
    """Own develop/revise/review-only task execution flows."""

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

    @property
    def planner(self):
        return self.orchestrator.planner

    @property
    def reviewers(self):
        return self.orchestrator.reviewers

    @property
    def worktree_mgr(self):
        return self.orchestrator.worktree_mgr

    @property
    def dep_tracker(self):
        return self.orchestrator.dep_tracker

    @property
    def _coder_by_complexity(self):
        return self.orchestrator._coder_by_complexity

    @property
    def _default_coder(self):
        return self.orchestrator._default_coder

    def latest_coder_session_id(self, task: Task) -> str:
        coder_sessions = task.session_ids.get("coder", [])
        if coder_sessions:
            return coder_sessions[-1]

        runs = self.db.get_runs_for_task(task.id)
        for run in sorted(runs, key=lambda r: r.created_at, reverse=True):
            if run.agent_type == "coder" and run.session_id:
                return run.session_id
        return ""

    def latest_revise_context(self, task_id: str) -> ReviseContext:
        """Return the latest revise round's manual feedback plus preceding review.

        Semantics:
        - Keep only the latest manual-review input for the current revise round.
        - If the immediately preceding relevant reviewer round raised feedback,
          keep that reviewer feedback too.
        - Do not re-send older manual-review notes, because those should already
          have been addressed or superseded.
        """
        runs = sorted(self.db.get_runs_for_task(task_id), key=lambda r: r.created_at)
        latest_manual_idx = -1
        latest_manual_feedback = ""
        for idx in range(len(runs) - 1, -1, -1):
            run = runs[idx]
            if run.agent_type != "manual_review":
                continue
            text = (run.output or "").strip()
            if text:
                latest_manual_idx = idx
                latest_manual_feedback = text
                break

        if latest_manual_idx == -1:
            return ReviseContext()

        prior_reviewer_feedback = ""
        for idx in range(latest_manual_idx - 1, -1, -1):
            run = runs[idx]
            if run.agent_type != "reviewer":
                continue
            review_text = self.client.extract_last_text_block_or_raw(run.output).strip()
            verdict = (
                ReviewerAgent._evaluate_review(None, review_text)
                if review_text
                else None
            )
            if verdict is False:
                prior_reviewer_feedback = review_text
            break

        return ReviseContext(
            manual_feedback=latest_manual_feedback,
            prior_reviewer_feedback=prior_reviewer_feedback,
        )

    def extract_coder_response(self, code_run: AgentRun) -> str:
        return self.client.extract_last_text_block(code_run.output)

    def latest_reviewer_feedback(self, task_id: str, fallback: str = "") -> str:
        """Return the most recent reviewer run's final text block."""
        runs = self.db.get_runs_for_task(task_id)
        for run in sorted(runs, key=lambda r: r.created_at, reverse=True):
            if run.agent_type != "reviewer":
                continue
            feedback = self.client.extract_last_text_block_or_raw(run.output).strip()
            if feedback:
                return feedback
        return fallback

    def ensure_coder_run_success(self, code_run: AgentRun, attempt: int):
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

    def plan_with_retry(self, task: Task, repo_path: str):
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

    def dispatch_review_only(self, task_id: str) -> bool:
        with self.orchestrator._lock:
            if task_id in self.orchestrator._futures:
                log.warning("Task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self.orchestrator._futures) >= max_p:
                log.warning("Max parallel tasks reached (%d)", max_p)
                return False
            future = self.orchestrator._pool.submit(
                self.orchestrator._review_only_pipeline, task_id
            )
            self.orchestrator._futures[task_id] = future
            log.info("Dispatched review-only task: %s", task_id)
            return True

    def dispatch_revise(self, task_id: str) -> bool:
        with self.orchestrator._lock:
            if task_id in self.orchestrator._futures:
                log.warning("Task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self.orchestrator._futures) >= max_p:
                log.warning("Max parallel tasks reached (%d)", max_p)
                return False
            future = self.orchestrator._pool.submit(
                self.orchestrator._revise_task_pipeline, task_id
            )
            self.orchestrator._futures[task_id] = future
            log.info("Dispatched revise for task: %s", task_id)
            return True

    def dispatch_resume(self, task_id: str, first_message: str) -> bool:
        with self.orchestrator._lock:
            if task_id in self.orchestrator._futures:
                log.warning("Task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self.orchestrator._futures) >= max_p:
                log.warning("Max parallel tasks reached (%d)", max_p)
                return False
            future = self.orchestrator._pool.submit(
                self.orchestrator._revise_task_pipeline,
                task_id,
                first_message,
                True,
            )
            self.orchestrator._futures[task_id] = future
            log.info("Dispatched resume for task: %s", task_id)
            return True

    def revise_task(self, task_id: str, feedback: str) -> dict:
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        if not TaskStatus.is_revisable(task.status):
            return {"error": f"Cannot revise task in {task.status.value} state"}
        if task.task_mode == "jira":
            return {"error": "Revise is not supported for jira-mode tasks"}
        if not task.worktree_path:
            return {"error": "Task has no worktree (was it split into sub-tasks?)"}

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

        task.user_feedback = feedback
        task.review_pass = False
        task.retry_count = 0
        task.status = TaskStatus.PENDING
        task.error = ""
        task.completed_at = 0.0
        task.updated_at = time.time()
        self.db.save_task(task)

        if task.task_mode == "review":
            self.orchestrator._dispatch_review_only(task_id)
        else:
            self.orchestrator._dispatch_revise(task_id)
        log.info(
            "Revise task [%s] (mode=%s) with manual feedback (%d chars)",
            task_id,
            task.task_mode,
            len(feedback),
        )
        log.debug("Task [%s] revised with feedback: %s", task_id, feedback)
        return {"ok": True, "task_id": task_id}

    def resume_task(self, task_id: str, message: str = "Continue") -> dict:
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        if not TaskStatus.is_resumable(task.status):
            return {"error": f"Cannot resume task in {task.status.value} state"}
        if task.task_mode != "develop":
            return {"error": "Resume is only supported for develop-mode tasks"}
        if not task.worktree_path:
            return {"error": "Task has no worktree; cannot resume coder session"}

        coder_session_id = self.orchestrator._latest_coder_session_id(task)
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

        if not self.orchestrator._dispatch_resume(task_id, resume_message):
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

    def revise_task_pipeline(
        self,
        task_id: str,
        first_coder_message: str = "",
        first_message_raw: bool = False,
    ):
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

            coder_session_id = self.orchestrator._latest_coder_session_id(task)
            revise_context = self.latest_revise_context(task_id)
            review_revision_context = (
                revise_context.reviewer_revision_context or task.user_feedback
            )
            if first_message_raw:
                initial_coder_feedback = first_coder_message or task.user_feedback
            else:
                initial_coder_feedback = revise_context.coder_retry_feedback or (
                    review_revision_context
                )

            for attempt in range(task.max_retries + 1):
                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Revise [%s] was cancelled, aborting", task_id)
                    return

                task.retry_count = attempt
                task.status = TaskStatus.CODING
                task.updated_at = time.time()
                self.db.save_task(task)

                coder_feedback = initial_coder_feedback
                if attempt > 0:
                    coder_feedback = self.latest_reviewer_feedback(
                        task.id, fallback=task.review_output
                    )
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
                        manual_feedback=(
                            revise_context.manual_feedback if attempt == 0 else ""
                        ),
                        prior_reviewer_feedback=(
                            revise_context.prior_reviewer_feedback
                            if attempt == 0
                            else ""
                        ),
                    )
                self.db.save_agent_run(code_run)
                task.code_output = code_text
                if code_run.session_id:
                    coder_session_id = code_run.session_id
                    task.session_ids.setdefault("coder", []).append(code_run.session_id)
                task.updated_at = time.time()
                self.db.save_task(task)

                self.orchestrator._ensure_coder_run_success(code_run, attempt + 1)

                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Revise [%s] cancelled before review", task_id)
                    return

                task.status = TaskStatus.REVIEWING
                task.updated_at = time.time()
                self.db.save_task(task)

                coder_response = self.orchestrator._extract_coder_response(code_run)

                reviewer_results = []
                rejection_outputs = []
                all_passed = True
                for reviewer in self.reviewers:
                    review_run, passed, review_text = reviewer.review_changes(
                        task,
                        worktree_path,
                        revision_context=review_revision_context,
                        coder_response=coder_response,
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
                    self.orchestrator._update_parent_status(task.id)
                    break
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
                self.orchestrator._update_parent_status(task_id)
        finally:
            with self.orchestrator._lock:
                self.orchestrator._futures.pop(task_id, None)

    def cleanup_review_worktree(self, task):
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

    def review_only_pipeline(self, task_id: str):
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

                if task.copy_files:
                    self.worktree_mgr.copy_files_into(worktree_path, task.copy_files)

            worktree_path = task.worktree_path
            revise_context = self.latest_revise_context(task_id)
            revision_context = (
                revise_context.reviewer_revision_context or task.user_feedback
            )
            prior_rejections = revise_context.prior_reviewer_feedback

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
                    prior_rejections=prior_rejections,
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

            self.orchestrator._cleanup_review_worktree(task)

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
                self.orchestrator._cleanup_review_worktree(task)
        finally:
            with self.orchestrator._lock:
                self.orchestrator._futures.pop(task_id, None)

    def execute_task(self, task_id: str):
        task = self.db.get_task(task_id)
        if not task:
            log.error("Task not found: %s", task_id)
            return
        if task.task_mode == "jira":
            self.orchestrator._jira_task_pipeline(task_id)
            return

        repo_path = self.config["repo"]["path"]
        try:
            task.status = TaskStatus.PLANNING
            task.started_at = time.time()
            task.updated_at = time.time()
            self.db.save_task(task)

            plan_run, is_split, plan_text, sub_tasks, complexity = (
                self.orchestrator._plan_with_retry(task, repo_path)
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
                log.info("Task [%s] split into %d sub-tasks", task.id, len(sub_tasks))
                task.plan_output = (
                    f"Split into {len(sub_tasks)} sub-tasks:\n"
                    + "\n".join(f"- {st.get('title', '')}" for st in sub_tasks)
                )
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
                for child in children:
                    if not self.dep_tracker.is_blocked(child.id):
                        self.orchestrator.dispatch_task(child.id)
                    else:
                        log.info(
                            "Sub-task [%s] '%s' blocked by deps=%s, waiting",
                            child.id,
                            child.title,
                            child.depends_on,
                        )
                task.status = TaskStatus.PLANNING
                task.updated_at = time.time()
                self.db.save_task(task)
                log.info(
                    "Task [%s] split into sub-tasks, waiting for children", task.id
                )
                return

            task.plan_output = plan_text
            task.updated_at = time.time()
            self.db.save_task(task)

            branch_name = self.orchestrator._generate_branch_slug(task.title, task.id)
            hooks = self.config.get("repo", {}).get("worktree_hooks", [])
            worktree_path = self.worktree_mgr.create_worktree(branch_name, hooks=hooks)
            task.branch_name = branch_name
            task.worktree_path = worktree_path
            task.updated_at = time.time()
            self.db.save_task(task)

            if task.copy_files:
                self.worktree_mgr.copy_files_into(worktree_path, task.copy_files)

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

            coder = self._coder_by_complexity.get(task.complexity, self._default_coder)
            log.info(
                "Task [%s] using coder model=%s (complexity=%s)",
                task.id,
                coder.model,
                task.complexity,
            )

            coder_session_id = ""
            all_prior_rejections: list[str] = []

            for attempt in range(task.max_retries + 1):
                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Task [%s] was cancelled, aborting loop", task_id)
                    return

                task.retry_count = attempt
                task.status = TaskStatus.CODING
                task.updated_at = time.time()
                self.db.save_task(task)

                if attempt == 0 or not coder_session_id:
                    code_run, code_text = coder.implement_task(
                        task,
                        worktree_path,
                        session_id=coder_session_id,
                        dep_context=dep_context,
                    )
                else:
                    code_run, code_text = coder.retry_with_feedback(
                        task,
                        worktree_path,
                        review_feedback=self.latest_reviewer_feedback(
                            task.id, fallback=task.review_output
                        ),
                        session_id=coder_session_id,
                    )
                self.db.save_agent_run(code_run)
                task.code_output = code_text
                if code_run.session_id:
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

                self.orchestrator._ensure_coder_run_success(code_run, attempt + 1)

                task = self.db.get_task(task_id)
                if task.status == TaskStatus.CANCELLED:
                    log.info("Task [%s] was cancelled before review, aborting", task_id)
                    return

                coder_response = self.orchestrator._extract_coder_response(code_run)

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
                        prior_rejections="\n\n".join(all_prior_rejections),
                        coder_response=coder_response,
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
                        log.info(
                            "Task [%s] short-circuiting after first rejection",
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
                    self.orchestrator._update_parent_status(task.id)
                    break
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
                self.orchestrator._update_parent_status(task_id)
        finally:
            with self.orchestrator._lock:
                self.orchestrator._futures.pop(task_id, None)
            self.orchestrator._flush_pending_dispatches()
