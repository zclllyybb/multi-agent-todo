"""Coding agent: implements changes using opencode in a git worktree."""

import logging
import os
from typing import Optional, Tuple

from agents.base import BaseAgent
from agents.prompts import coder_implement, coder_retry_feedback
from core.models import AgentRun, Task
from core.opencode_client import OpenCodeClient

log = logging.getLogger(__name__)


class CoderAgent(BaseAgent):
    agent_type = "coder"

    def __init__(self, model: str, client: OpenCodeClient):
        super().__init__(model, client)

    @staticmethod
    def _resolve_file_path(file_path: str, worktree_path: str) -> Optional[str]:
        """Convert an absolute repo file path to a relative path inside the worktree.

        Returns the relative path if the file exists in the worktree, else None.
        """
        if not file_path:
            return None
        rel = file_path
        if os.path.isabs(rel):
            # Try os.path.relpath first (works when worktree IS the repo checkout)
            candidate = os.path.relpath(rel, worktree_path)
            if not candidate.startswith(".."):
                rel = candidate
            else:
                # file_path is under a different root (main repo vs worktree).
                # Walk the path components to find the suffix that exists.
                parts = rel.split("/")
                for i in range(1, len(parts)):
                    candidate = "/".join(parts[i:])
                    if os.path.exists(os.path.join(worktree_path, candidate)):
                        rel = candidate
                        break
        if os.path.exists(os.path.join(worktree_path, rel)):
            log.info("Coder file_path resolved to: %s (from %s)", rel, file_path)
            return rel
        log.warning("Coder file_path not found in worktree, ignoring: %s", file_path)
        return None

    def implement_task(
        self,
        task: Task,
        worktree_path: str,
        session_id: str = "",
        dep_context: str = "",
    ) -> Tuple[AgentRun, str]:
        """Use opencode to implement the task in the given worktree.

        If session_id is provided the existing session is continued so the
        model retains full prior context (used on review-feedback retries).
        Returns (agent_run, output_text).
        """
        rel_path = self._resolve_file_path(task.file_path, worktree_path)
        prompt = self._build_prompt(task, rel_path, dep_context=dep_context)

        run = self.run(
            prompt,
            worktree_path,
            task_id=task.id,
            session_id=session_id,
            max_continues=8,
            require_stop=True,
        )
        output_text = self.get_text(run)
        return run, output_text

    def continue_session(
        self,
        task: Task,
        worktree_path: str,
        user_message: str,
        session_id: str,
    ) -> Tuple[AgentRun, str]:
        """Continue an existing coder session with a raw user-provided message."""
        run = self.run(
            user_message,
            worktree_path,
            task_id=task.id,
            session_id=session_id,
            max_continues=8,
            require_stop=True,
        )
        output_text = self.get_text(run)
        return run, output_text

    def retry_with_feedback(
        self,
        task: Task,
        worktree_path: str,
        review_feedback: str,
        session_id: str,
        manual_feedback: str = "",
        prior_reviewer_feedback: str = "",
    ) -> Tuple[AgentRun, str]:
        """Continue an existing coder session with only the review feedback.

        Used on retry rounds where the session already has the full task
        context; re-sending the complete prompt would be redundant.
        """
        prompt = coder_retry_feedback(
            review_feedback=review_feedback,
            attempt=task.retry_count,
            manual_feedback=manual_feedback,
            prior_reviewer_feedback=prior_reviewer_feedback,
        )
        run = self.run(
            prompt,
            worktree_path,
            task_id=task.id,
            session_id=session_id,
            max_continues=8,
            require_stop=True,
        )
        output_text = self.get_text(run)
        return run, output_text

    def _build_prompt(
        self, task: Task, rel_file_path: Optional[str] = None, dep_context: str = ""
    ) -> str:
        return coder_implement(
            title=task.title,
            description=task.description,
            file_path=rel_file_path or task.file_path,
            line_number=task.line_number,
            plan_output=task.plan_output,
            dep_context=dep_context,
        )
