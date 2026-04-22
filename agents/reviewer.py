"""Review agent: reviews code changes made by the coding agent."""

import logging
from typing import Optional, Tuple

from agents.base import BaseAgent
from agents.prompts import reviewer_review, reviewer_review_patch
from core.models import AgentRun, ModelOutputError, Task
from core.opencode_client import OpenCodeClient

log = logging.getLogger(__name__)

# Maximum number of times to auto-retry a reviewer when its output is
# inconclusive (truncated / no verdict).  This does NOT count against the
# orchestrator's per-task max_retries.
_MAX_REVIEWER_RETRIES = 1


class ReviewerAgent(BaseAgent):
    agent_type = "reviewer"

    def __init__(
        self,
        model: str,
        client: OpenCodeClient,
        variant: str = "",
        agent: str = "",
    ):
        super().__init__(model, client, variant=variant, agent=agent)

    def review_changes(
        self,
        task: Task,
        worktree_path: str,
        revision_context: str = "",
        prior_rejections: str = "",
        coder_response: str = "",
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

        If *coder_response* is provided (the coder's textual response from the
        latest coding round), it is included so the reviewer can consider the
        coder's reasoning and stated intent alongside the code changes.

        Returns (agent_run, passed: bool, review_text).
        """
        prompt = reviewer_review(
            title=task.title,
            description=task.description,
            revision_context=revision_context,
            prior_rejections=prior_rejections,
            coder_response=coder_response,
        )
        return self._run_review_with_retry(prompt, worktree_path, task.id, mode="changes")

    def review_patch(
        self,
        task: Task,
        worktree_path: str,
        revision_context: str = "",
        prior_rejections: str = "",
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
            prior_rejections=prior_rejections,
        )
        return self._run_review_with_retry(prompt, worktree_path, task.id, mode="patch")

    def _run_review_with_retry(
        self,
        prompt: str,
        worktree_path: str,
        task_id: str,
        mode: str,
    ) -> Tuple[AgentRun, bool, str]:
        run = None
        review_text = ""
        verdict = None
        for attempt in range(_MAX_REVIEWER_RETRIES + 1):
            run = self.run(prompt, worktree_path, task_id=task_id)
            if not self.client.has_readable_steps(run.output):
                raise ModelOutputError(
                    f"Reviewer({self.model}) {mode} output is not a readable opencode "
                    f"transcript for task [{task_id}] (session={run.session_id or '-'})"
                )
            review_text = self.get_final_text(run)
            if not self.client.is_output_complete(run.output):
                if attempt < _MAX_REVIEWER_RETRIES:
                    log.warning(
                        "Reviewer(%s) %s output incomplete for task [%s], auto-retrying (%d/%d)",
                        self.model,
                        mode,
                        task_id,
                        attempt + 1,
                        _MAX_REVIEWER_RETRIES,
                    )
                    continue
                log.error(
                    "Reviewer(%s) %s output still incomplete after %d retries for task [%s], treating as REQUEST_CHANGES",
                    self.model,
                    mode,
                    _MAX_REVIEWER_RETRIES,
                    task_id,
                )
                return run, False, review_text

            verdict = self._evaluate_review(review_text)
            if verdict is not None:
                return run, verdict, review_text

            if attempt < _MAX_REVIEWER_RETRIES:
                log.warning(
                    "Reviewer(%s) %s output inconclusive for task [%s], auto-retrying (%d/%d)",
                    self.model,
                    mode,
                    task_id,
                    attempt + 1,
                    _MAX_REVIEWER_RETRIES,
                )
                continue

        log.error(
            "Reviewer(%s) %s still inconclusive after %d retries for task [%s], treating as REQUEST_CHANGES",
            self.model,
            mode,
            _MAX_REVIEWER_RETRIES,
            task_id,
        )
        return run, False, review_text

    def _evaluate_review(self, review_text: str) -> Optional[bool]:
        """Parse review output to determine if changes are approved.

        Returns:
            True   – explicitly approved
            False  – explicitly rejected (REQUEST_CHANGES or negative heuristic)
            None   – inconclusive (no verdict keywords AND no heuristic signals;
                     typically means the reviewer output was truncated)
        """
        text_upper = review_text.upper()

        # Step 1: Look for a standalone verdict line.  The reviewer is
        # instructed to put APPROVE or REQUEST_CHANGES on its own line.
        for line in text_upper.splitlines():
            stripped = line.strip()
            if stripped in ("APPROVE", "APPROVED"):
                return True
            if stripped == "REQUEST_CHANGES":
                return False

        # Step 2: No standalone verdict line.  Use the *last* occurrence of
        # each keyword — the final mention after preamble/deliberation text
        # is most likely to be the actual verdict.
        # Example: "...APPROVE/REQUEST_CHANGES verdict...APPROVE" → True
        # Example: "I considered APPROVE but ultimately REQUEST_CHANGES" → False
        last_approve = text_upper.rfind("APPROVE")
        last_reject = text_upper.rfind("REQUEST_CHANGES")

        if last_approve != -1 and last_reject != -1:
            return last_approve > last_reject
        if last_approve != -1:
            return True
        if last_reject != -1:
            return False

        # Heuristic: look for positive signals
        positive = ["LGTM", "LOOKS GOOD", "APPROVED", "NO ISSUES"]
        negative = ["BUG", "ERROR", "INCORRECT", "WRONG", "MISSING", "SHOULD BE"]
        pos_count = sum(1 for p in positive if p in text_upper)
        neg_count = sum(1 for n in negative if n in text_upper)
        if pos_count == 0 and neg_count == 0:
            return None  # inconclusive — no signals at all
        return pos_count > neg_count
