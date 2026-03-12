"""Review agent: reviews code changes made by the coding agent."""

import logging
from typing import Tuple

from agents.base import BaseAgent
from agents.prompts import reviewer_review, reviewer_review_patch
from core.models import AgentRun, Task
from core.opencode_client import OpenCodeClient

log = logging.getLogger(__name__)


class ReviewerAgent(BaseAgent):
    agent_type = "reviewer"

    def __init__(self, model: str, client: OpenCodeClient):
        super().__init__(model, client)

    def review_changes(
        self, task: Task, worktree_path: str,
        revision_context: str = "",
        prior_rejections: str = "",
    ) -> Tuple[AgentRun, bool, str]:
        """Review the committed changes in the worktree.

        The coder's work has already been committed.  The reviewer runs as a
        full opencode agent inside the worktree and is free to use git log,
        git diff, read files, etc. to form its judgement.

        If *revision_context* is provided (manual user feedback for a revise),
        it is included in the prompt so the reviewer can verify it was addressed.

        If *prior_rejections* is provided (concatenated REQUEST_CHANGES feedback
        from all previous retry rounds), it is included so the reviewer knows
        what issues were already raised and can verify whether they were fixed.

        Returns (agent_run, passed: bool, review_text).
        """
        prompt = reviewer_review(
            title=task.title,
            description=task.description,
            revision_context=revision_context,
            prior_rejections=prior_rejections,
        )
        run = self.run(prompt, worktree_path, task_id=task.id)
        review_text = self.get_text(run)

        passed = self._evaluate_review(review_text)
        return run, passed, review_text

    def review_patch(
        self, task: Task, worktree_path: str,
        revision_context: str = "",
    ) -> Tuple[AgentRun, bool, str]:
        """Review a user-supplied patch / PR link / code snippet.

        Used for review-only tasks (task_mode='review').
        If *revision_context* is provided (manual user feedback for a revise),
        it is included in the prompt so the reviewer pays attention to it.
        Returns (agent_run, passed: bool, review_text).
        """
        prompt = reviewer_review_patch(
            title=task.title,
            review_input=task.review_input,
            revision_context=revision_context,
        )
        run = self.run(prompt, worktree_path, task_id=task.id)
        review_text = self.get_text(run)
        passed = self._evaluate_review(review_text)
        return run, passed, review_text

    def _evaluate_review(self, review_text: str) -> bool:
        """Parse review output to determine if changes are approved."""
        text_upper = review_text.upper()
        # Look for explicit approval/rejection
        if "APPROVE" in text_upper and "REQUEST_CHANGES" not in text_upper:
            return True
        if "REQUEST_CHANGES" in text_upper:
            return False
        # Heuristic: look for positive signals
        positive = ["LGTM", "LOOKS GOOD", "APPROVED", "NO ISSUES"]
        negative = ["BUG", "ERROR", "INCORRECT", "WRONG", "MISSING", "SHOULD BE"]
        pos_count = sum(1 for p in positive if p in text_upper)
        neg_count = sum(1 for n in negative if n in text_upper)
        return pos_count > neg_count
