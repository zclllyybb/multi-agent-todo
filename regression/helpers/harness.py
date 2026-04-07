"""Black-box daemon and HTTP harness for real regression executions."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

from core.config import load_config
from core.models import AgentRun, ExploreRun, Task, TaskStatus
from regression.helpers.configuration import RegressionConfigFactory
from regression.helpers.models import (
    RegressionModelProfile,
    RegressionSettings,
    RegressionWorkspace,
)
from regression.helpers.waiting import wait_until


_TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.NEEDS_ARBITRATION,
}


class RegressionHarness:
    """Runs the real daemon and drives it strictly through public APIs."""

    def __init__(
        self,
        *,
        workspace: RegressionWorkspace,
        settings: RegressionSettings,
        config: dict,
        model_profile: RegressionModelProfile,
        config_factory: RegressionConfigFactory,
    ):
        self.workspace = workspace
        self.settings = settings
        self.config = config
        self.model_profile = model_profile
        self._config_factory = config_factory
        self._daemon_proc: subprocess.Popen[str] | None = None
        self._cli = Path(__file__).resolve().parents[2] / "cli.py"
        self._config_path = Path(self.workspace.paths.config_file)
        self._start_daemon()

    @classmethod
    def create(
        cls,
        *,
        workspace: RegressionWorkspace,
        settings: RegressionSettings,
        config_factory: RegressionConfigFactory,
        config_overrides: dict[str, Any] | None = None,
    ) -> "RegressionHarness":
        config, profile = config_factory.create_runtime_config(
            workspace,
            profile_name=settings.profile_name,
            config_overrides=config_overrides,
        )
        return cls(
            workspace=workspace,
            settings=settings,
            config=config,
            model_profile=profile,
            config_factory=config_factory,
        )

    def close(self) -> None:
        try:
            if self.workspace.paths.repo.exists():
                self._run_cli("stop", check=False, timeout=30)
        finally:
            proc = self._daemon_proc
            if proc is not None:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                self._daemon_proc = None

    def restart(self, *, config_overrides: dict[str, Any] | None = None) -> None:
        self.close()
        config, profile = self._config_factory.create_runtime_config(
            self.workspace,
            profile_name=self.settings.profile_name,
            config_overrides=config_overrides,
        )
        self.config = config
        self.model_profile = profile
        self._start_daemon()

    def restart_preserving_runtime_config(self) -> None:
        self.close()
        self.config = load_config(str(self._config_path))
        self._start_daemon()

    def submit_develop_task(
        self,
        *,
        title: str,
        description: str,
        priority: str = "medium",
        file_path: str = "",
        line_number: int = 0,
    ) -> dict[str, Any]:
        return self.post_json(
            "/api/tasks",
            {
                "title": title,
                "description": description,
                "priority": priority,
                "file_path": file_path,
                "line_number": line_number,
            },
        )

    def submit_review_task(
        self,
        *,
        title: str,
        review_input: str,
        priority: str = "medium",
        copy_files: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.post_json(
            "/api/tasks/review",
            {
                "title": title,
                "review_input": review_input,
                "priority": priority,
                "copy_files": "\n".join(copy_files or []),
            },
        )

    def submit_jira_task(
        self,
        *,
        title: str,
        description: str,
        priority: str = "medium",
        source_task_id: str = "",
    ) -> dict[str, Any]:
        return self.post_json(
            "/api/tasks/jira",
            {
                "title": title,
                "description": description,
                "priority": priority,
                "source_task_id": source_task_id,
            },
        )

    def assign_jira_for_task(self, task_id: str) -> dict[str, Any]:
        return self.post_json(f"/api/tasks/{task_id}/jira", {})

    def revise_task(self, task_id: str, *, feedback: str) -> dict[str, Any]:
        return self.post_json(
            f"/api/tasks/{task_id}/revise",
            {"feedback": feedback},
        )

    def resume_task(self, task_id: str, *, message: str = "Continue") -> dict[str, Any]:
        return self.post_json(
            f"/api/tasks/{task_id}/resume",
            {"message": message},
        )

    def arbitrate_task(
        self, task_id: str, *, action: str, feedback: str = ""
    ) -> dict[str, Any]:
        return self.post_json(
            f"/api/tasks/{task_id}/arbitrate",
            {"action": action, "feedback": feedback},
        )

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return self.post_json(f"/api/tasks/{task_id}/cancel", {})

    def init_explore_map(self) -> dict[str, Any]:
        return self.post_json("/api/explore/init-map", {})

    def start_exploration(
        self,
        *,
        module_ids: list[str] | None = None,
        categories: list[str] | None = None,
        focus_point: str = "",
    ) -> dict[str, Any]:
        return self.post_json(
            "/api/explore/start",
            {
                "module_ids": module_ids,
                "categories": categories,
                "focus_point": focus_point,
            },
        )

    def add_explore_module(
        self,
        *,
        name: str,
        path: str,
        parent_id: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        return self.post_json(
            "/api/explore/modules",
            {
                "name": name,
                "path": path,
                "parent_id": parent_id,
                "description": description,
            },
        )

    def update_explore_module(
        self, module_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        return self.post_json(f"/api/explore/modules/{module_id}/update", updates)

    def get_task_record(self, task_id: str) -> Task | None:
        try:
            detail = self.get_task_detail(task_id)
        except AssertionError:
            return None
        task_payload = detail.get("task") if isinstance(detail, dict) else None
        if not isinstance(task_payload, dict):
            return None
        return Task.from_dict(task_payload)

    def get_task_runs(self, task_id: str) -> list[AgentRun]:
        try:
            detail = self.get_task_detail(task_id)
        except AssertionError:
            return []
        runs = detail.get("runs", []) if isinstance(detail, dict) else []
        return [AgentRun.from_dict(run) for run in runs if isinstance(run, dict)]

    def get_explore_runs(self) -> list[ExploreRun]:
        runs = self.get_explore_runs_api()
        return [ExploreRun.from_dict(run) for run in runs if isinstance(run, dict)]

    def get_task_detail(self, task_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/tasks/{task_id}")

    def list_tasks(self) -> list[dict[str, Any]]:
        return self.get_json("/api/tasks")

    def get_explore_status(self) -> dict[str, Any]:
        return self.get_json("/api/explore/status")

    def get_config(self) -> dict[str, Any]:
        return self.get_json("/api/config")

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        return self.post_json("/api/config", updates)

    def list_explore_modules(self) -> list[dict[str, Any]]:
        return self.get_json("/api/explore/modules")

    def get_explore_module_detail(self, module_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/explore/modules/{module_id}")

    def get_explore_queue(self) -> dict[str, Any]:
        return self.get_json("/api/explore/queue")

    def exec_in_task_worktree(self, task_id: str, *, command: str) -> dict[str, Any]:
        return self.post_json(f"/api/tasks/{task_id}/exec", {"command": command})

    def get_explore_runs_api(self) -> list[dict[str, Any]]:
        return self.get_json("/api/explore/runs")

    def get_explore_run_detail(self, run_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/explore/runs/{run_id}")

    def create_task_from_finding(
        self, run_id: str, *, finding_index: int
    ) -> dict[str, Any]:
        return self.post_json(
            f"/api/explore/runs/{run_id}/create-task",
            {"finding_index": finding_index},
        )

    def wait_for_task_terminal(
        self,
        task_id: str,
        *,
        timeout_sec: float | None = None,
    ) -> Task:
        timeout_sec = timeout_sec or self.settings.task_timeout_sec

        def _task_if_terminal() -> Task | None:
            task = self.get_task_record(task_id)
            if task and task.status in _TERMINAL_TASK_STATUSES:
                return task
            return None

        try:
            task = wait_until(
                _task_if_terminal,
                timeout_sec=timeout_sec,
                poll_interval_sec=self.settings.poll_interval_sec,
                description=f"task {task_id} to reach a terminal state",
            )
        except TimeoutError as exc:
            raise TimeoutError(f"{exc}\n\n{self.describe_task(task_id)}") from exc
        assert task is not None
        return task

    def wait_for_task_status(
        self,
        task_id: str,
        expected_status: TaskStatus,
        *,
        timeout_sec: float | None = None,
    ) -> Task:
        timeout_sec = timeout_sec or self.settings.task_timeout_sec

        def _task_if_matching() -> Task | None:
            task = self.get_task_record(task_id)
            if task and task.status == expected_status:
                return task
            return None

        try:
            task = wait_until(
                _task_if_matching,
                timeout_sec=timeout_sec,
                poll_interval_sec=self.settings.poll_interval_sec,
                description=f"task {task_id} to reach status {expected_status.value}",
            )
        except TimeoutError as exc:
            raise TimeoutError(f"{exc}\n\n{self.describe_task(task_id)}") from exc
        assert task is not None
        return task

    def wait_for_jira_result(
        self,
        task_id: str,
        *,
        timeout_sec: float | None = None,
    ) -> Task:
        timeout_sec = timeout_sec or self.settings.task_timeout_sec

        def _task_if_jira_finished() -> Task | None:
            task = self.get_task_record(task_id)
            if task and task.jira_status in {"created", "failed"}:
                return task
            return None

        try:
            task = wait_until(
                _task_if_jira_finished,
                timeout_sec=timeout_sec,
                poll_interval_sec=self.settings.poll_interval_sec,
                description=f"task {task_id} to finish jira assignment",
            )
        except TimeoutError as exc:
            raise TimeoutError(f"{exc}\n\n{self.describe_task(task_id)}") from exc
        assert task is not None
        return task

    def wait_for_explore_map_terminal(
        self, *, timeout_sec: float | None = None
    ) -> dict[str, Any]:
        timeout_sec = timeout_sec or self.settings.explore_timeout_sec

        state = wait_until(
            lambda: self._explore_map_terminal_state(),
            timeout_sec=timeout_sec,
            poll_interval_sec=self.settings.poll_interval_sec,
            description="explore map initialization to finish",
        )
        assert state is not None
        return state

    def wait_for_exploration_idle(
        self, *, timeout_sec: float | None = None
    ) -> dict[str, Any]:
        timeout_sec = timeout_sec or self.settings.explore_timeout_sec

        state = wait_until(
            lambda: self._explore_queue_if_idle(),
            timeout_sec=timeout_sec,
            poll_interval_sec=self.settings.poll_interval_sec,
            description="exploration queue to become idle",
        )
        assert state is not None
        return state

    def wait_for_exploration_activity(
        self, *, timeout_sec: float | None = None
    ) -> dict[str, Any]:
        timeout_sec = timeout_sec or self.settings.explore_timeout_sec

        state = wait_until(
            lambda: self._explore_queue_if_active(),
            timeout_sec=timeout_sec,
            poll_interval_sec=self.settings.poll_interval_sec,
            description="exploration queue to become active",
        )
        assert state is not None
        return state

    def wait_for_exploration_session(
        self, *, timeout_sec: float | None = None
    ) -> dict[str, Any]:
        timeout_sec = timeout_sec or self.settings.explore_timeout_sec

        state = wait_until(
            lambda: self._explore_queue_if_session_active(),
            timeout_sec=timeout_sec,
            poll_interval_sec=self.settings.poll_interval_sec,
            description="exploration run to acquire a session",
        )
        assert state is not None
        return state

    def wait_for_exploration_running_age(
        self,
        *,
        min_age_sec: float,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        timeout_sec = timeout_sec or self.settings.explore_timeout_sec

        state = wait_until(
            lambda: self._explore_queue_if_running_for(min_age_sec),
            timeout_sec=timeout_sec,
            poll_interval_sec=self.settings.poll_interval_sec,
            description=f"exploration run to stay active for {min_age_sec:.1f}s",
        )
        assert state is not None
        return state

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self._url(path),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._request_json(req)

    def crash_daemon(self) -> None:
        proc = self._daemon_proc
        if proc is None:
            raise AssertionError("Regression daemon is not running")
        proc.kill()
        proc.wait(timeout=10)
        self._daemon_proc = None

    def get_json(self, path: str) -> Any:
        req = request.Request(self._url(path), method="GET")
        return self._request_json(req)

    def describe_task(self, task_id: str) -> str:
        task = self.get_task_record(task_id)
        runs = sorted(self.get_task_runs(task_id), key=lambda run: run.created_at)
        lines = [f"Regression task diagnostics for {task_id}:"]
        if task is None:
            lines.append("- task: <missing>")
        else:
            lines.append(f"- status: {task.status.value}")
            lines.append(f"- error: {task.error or '-'}")
            lines.append(f"- jira_status: {task.jira_status or '-'}")
            lines.append(f"- jira_key: {task.jira_issue_key or '-'}")
            lines.append(f"- jira_url: {task.jira_issue_url or '-'}")
            lines.append(f"- branch: {task.branch_name or '-'}")
            lines.append(f"- worktree: {task.worktree_path or '-'}")
        lines.append(
            f"- daemon stdout: {self.workspace.paths.logs_dir / 'daemon.stdout.log'}"
        )
        lines.append(
            f"- daemon stderr: {self.workspace.paths.logs_dir / 'daemon.stderr.log'}"
        )
        lines.append(f"- runtime config: {self.workspace.paths.config_file}")
        lines.append(f"- runtime repo: {self.workspace.paths.repo}")
        if runs:
            lines.append("- agent runs:")
            for run in runs:
                summary = self._summarize_agent_output(run.output)
                lines.append(
                    f"  * {run.agent_type} model={run.model} exit={run.exit_code} "
                    f"session={run.session_id or '-'} summary={summary}"
                )
        lines.append(f"- product log: {self._product_log_path()}")
        return "\n".join(lines)

    def describe_explore(self) -> str:
        status = self.get_explore_status()
        queue = self.get_explore_queue()
        runs = self.get_explore_runs_api()
        lines = ["Regression explore diagnostics:"]
        lines.append(f"- map_status: {status.get('map_init', {}).get('status', '-')}")
        lines.append(f"- map_ready: {status.get('map_ready')}")
        lines.append(f"- queued: {queue.get('counts', {}).get('queued', '-')}")
        lines.append(f"- running: {queue.get('counts', {}).get('running', '-')}")
        lines.append(f"- total: {queue.get('counts', {}).get('total', '-')}")
        lines.append(f"- explore_runs: {len(runs)}")
        lines.append(
            f"- daemon stdout: {self.workspace.paths.logs_dir / 'daemon.stdout.log'}"
        )
        lines.append(
            f"- daemon stderr: {self.workspace.paths.logs_dir / 'daemon.stderr.log'}"
        )
        lines.append(f"- runtime config: {self.workspace.paths.config_file}")
        lines.append(f"- runtime repo: {self.workspace.paths.repo}")
        lines.append(f"- product log: {self._product_log_path()}")
        return "\n".join(lines)

    @property
    def runtime_repo(self) -> Path:
        return Path(self.workspace.paths.repo)

    @property
    def api_base_url(self) -> str:
        host = self.config["web"]["host"]
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return f"http://{host}:{self.config['web']['port']}"

    def _start_daemon(self) -> None:
        stdout_path = self.workspace.paths.logs_dir / "daemon.stdout.log"
        stderr_path = self.workspace.paths.logs_dir / "daemon.stderr.log"
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [
                sys.executable,
                str(self._cli),
                "--config",
                str(self._config_path),
                "start",
                "--foreground",
            ],
            cwd=str(self.workspace.paths.repo),
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        self._daemon_proc = proc
        try:
            wait_until(
                lambda: self._daemon_ready(),
                timeout_sec=self.settings.daemon_start_timeout_sec,
                poll_interval_sec=0.25,
                description="regression daemon to become ready",
            )
        except Exception:
            stdout_handle.close()
            stderr_handle.close()
            raise
        stdout_handle.close()
        stderr_handle.close()

    def _product_log_path(self) -> Path:
        configured = str(self.config.get("logging", {}).get("file", "")).strip()
        if configured:
            return Path(configured)
        return self.workspace.paths.logs_dir / "regression.log"

    def _daemon_ready(self) -> dict[str, Any] | None:
        proc = self._daemon_proc
        if proc is None:
            return None
        if proc.poll() is not None:
            raise RuntimeError(
                "Regression daemon exited early. "
                f"stdout={self.workspace.paths.logs_dir / 'daemon.stdout.log'} "
                f"stderr={self.workspace.paths.logs_dir / 'daemon.stderr.log'}"
            )
        try:
            return self.get_json("/api/status")
        except Exception:
            return None

    def _explore_map_terminal_state(self) -> dict[str, Any] | None:
        state = self.get_explore_status()
        map_init = state.get("map_init", {})
        if map_init.get("status") in {"done", "failed", "cancelled"}:
            return state
        return None

    def _explore_queue_if_idle(self) -> dict[str, Any] | None:
        state = self.get_explore_queue()
        if state.get("counts", {}).get("total") == 0:
            return state
        return None

    def _explore_queue_if_active(self) -> dict[str, Any] | None:
        state = self.get_explore_queue()
        if state.get("counts", {}).get("total", 0) > 0:
            return state
        return None

    def _explore_queue_if_session_active(self) -> dict[str, Any] | None:
        state = self.get_explore_queue()
        running = state.get("running", []) if isinstance(state, dict) else []
        if any(str(job.get("session_id", "")).strip() for job in running):
            return state
        return None

    def _explore_queue_if_running_for(
        self, min_age_sec: float
    ) -> dict[str, Any] | None:
        state = self.get_explore_queue()
        running = state.get("running", []) if isinstance(state, dict) else []
        now = __import__("time").time()
        if any(
            now - float(job.get("started_at", 0.0) or 0.0) >= min_age_sec
            for job in running
        ):
            return state
        return None

    def _request_json(self, req: request.Request) -> Any:
        try:
            with request.urlopen(req, timeout=30) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise AssertionError(
                f"HTTP request failed: {req.method} {req.full_url} status={exc.code}\n{body}"
            ) from exc
        except error.URLError as exc:
            raise AssertionError(
                f"HTTP request could not reach regression daemon: {req.method} {req.full_url} error={exc}"
            ) from exc
        payload = json.loads(body) if body else None
        return payload

    def _url(self, path: str) -> str:
        return f"{self.api_base_url}{path}"

    def _run_cli(
        self,
        *args: str,
        check: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            str(self._cli),
            "--config",
            str(self._config_path),
            *args,
        ]
        result = subprocess.run(
            cmd,
            cwd=str(self.workspace.paths.repo),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"CLI command failed: {cmd!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    @staticmethod
    def _summarize_agent_output(output: str) -> str:
        text = " ".join(str(output or "").split())
        return text[:220] or "-"
