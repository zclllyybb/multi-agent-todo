"""Jira-specific orchestration logic extracted from Orchestrator."""

import logging
import os
import time
import traceback

from agents.prompts import coder_assign_jira_issue
from core.models import AgentRun, Task, TaskPriority, TaskSource, TaskStatus

log = logging.getLogger(__name__)


class JiraService:
    """Own Jira-related config normalization and execution flows."""

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
    def _coder_by_complexity(self):
        return self.orchestrator._coder_by_complexity

    @property
    def _default_coder(self):
        return self.orchestrator._default_coder

    def get_jira_config(self) -> dict:
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

    def run_jira_agent(self, task: Task) -> tuple[AgentRun, str]:
        jira = self.get_jira_config()
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
            variant=simple_agent.variant,
            agent=simple_agent.agent,
            agent_type="jira_assign",
            task_id=task.id,
            max_continues=8,
            env=env,
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

    def parse_jira_agent_result(self, task: Task, text: str) -> dict:
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
    def build_jira_browse_url(jira_base_url: str, issue_key: str) -> str:
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
        jira = self.get_jira_config()
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
        dispatched = self.orchestrator._dispatch_jira_task(task.id)
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
        dispatched = self.orchestrator._dispatch_jira_task(source_task.id)
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

    def dispatch_jira_task(self, task_id: str) -> bool:
        with self.orchestrator._lock:
            if task_id in self.orchestrator._futures:
                log.warning("Jira task already running: %s", task_id)
                return False
            max_p = self.config["orchestrator"]["max_parallel_tasks"]
            if len(self.orchestrator._futures) >= max_p:
                log.warning("Max parallel tasks reached for jira dispatch (%d)", max_p)
                return False
            future = self.orchestrator._pool.submit(self.jira_task_pipeline, task_id)
            self.orchestrator._futures[task_id] = future
            log.info("Dispatched jira task: jira_task=%s", task_id)
            return True

    def jira_task_pipeline(self, task_id: str):
        task = self.db.get_task(task_id)
        if not task:
            log.error("Jira task not found: %s", task_id)
            return

        original_status = task.status
        original_completed_at = task.completed_at
        original_review_pass = task.review_pass
        original_reviewer_results = list(task.reviewer_results)
        original_review_output = task.review_output
        original_error = task.error

        try:
            jira = self.get_jira_config()
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

            agent_run, agent_text = self.orchestrator._run_jira_agent(task)
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

            result = self.orchestrator._parse_jira_agent_result(task, agent_text)
            key = str(result.get("key", "")).strip()
            issue_url = self.orchestrator._build_jira_browse_url(jira["url"], key)
            task.jira_payload_preview = str(result.get("payload", "")).strip()

            task.jira_issue_key = key
            task.jira_issue_url = issue_url
            task.jira_status = "created"
            if task.task_mode == "jira":
                task.review_pass = True
                task.reviewer_results = []
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
            else:
                task.status = TaskStatus.PENDING
                task.completed_at = 0.0
                task.review_pass = original_review_pass
                task.reviewer_results = original_reviewer_results
                task.review_output = original_review_output
                task.error = original_error
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

            self.orchestrator._update_parent_status(task.id)
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
                if task.task_mode != "jira":
                    task.review_pass = original_review_pass
                    task.reviewer_results = original_reviewer_results
                    task.review_output = original_review_output
                    task.completed_at = original_completed_at
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

                self.orchestrator._update_parent_status(task_id)
        finally:
            with self.orchestrator._lock:
                self.orchestrator._futures.pop(task_id, None)
