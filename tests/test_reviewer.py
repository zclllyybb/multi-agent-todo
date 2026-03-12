"""Tests for agents/reviewer.py: _evaluate_review pure logic and prompts."""

from unittest.mock import MagicMock

import pytest

from agents.prompts import reviewer_review
from agents.reviewer import ReviewerAgent
from core.models import AgentRun, Task


@pytest.fixture
def evaluator():
    """Return a ReviewerAgent with a dummy client — we only test _evaluate_review."""
    class _DummyClient:
        pass
    return ReviewerAgent(model="test-model", client=_DummyClient())


class TestEvaluateReview:

    # ── Explicit verdict keywords ──

    def test_approve_keyword(self, evaluator):
        assert evaluator._evaluate_review("APPROVE\nAll looks good.") is True

    def test_request_changes_keyword(self, evaluator):
        assert evaluator._evaluate_review("REQUEST_CHANGES\nPlease fix the bug.") is False

    def test_request_changes_overrides_approve(self, evaluator):
        """If both keywords appear, REQUEST_CHANGES wins."""
        text = "I considered APPROVE but ultimately REQUEST_CHANGES are needed."
        assert evaluator._evaluate_review(text) is False

    # ── Heuristic fallback (no explicit keywords) ──

    def test_heuristic_positive(self, evaluator):
        text = "LGTM, looks good to me."
        assert evaluator._evaluate_review(text) is True

    def test_heuristic_negative(self, evaluator):
        text = "There is a bug in the implementation and the result is wrong."
        assert evaluator._evaluate_review(text) is False

    def test_heuristic_tie_rejects(self, evaluator):
        """Equal positive and negative signals → not passed (pos_count > neg_count is False)."""
        text = "Looks good but there is a bug."
        assert evaluator._evaluate_review(text) is False

    def test_empty_output_passes(self, evaluator):
        """No signals at all → 0 > 0 is False → rejected."""
        assert evaluator._evaluate_review("") is False

    # ── Case insensitivity ──

    def test_approve_case_insensitive(self, evaluator):
        assert evaluator._evaluate_review("approve") is True

    def test_request_changes_case_insensitive(self, evaluator):
        assert evaluator._evaluate_review("request_changes") is False


class TestReviewerReviewPrompt:
    """Tests for the reviewer_review() prompt builder."""

    def test_no_prior_rejections(self):
        prompt = reviewer_review(title="T", description="D")
        assert "Previous Review Rejections" not in prompt
        assert "T" in prompt
        assert "D" in prompt

    def test_prior_rejections_included(self):
        prompt = reviewer_review(
            title="T", description="D",
            prior_rejections="=== Reviewer: m1 | REQUEST_CHANGES ===\nFix the null check.",
        )
        assert "Previous Review Rejections (for reference only)" in prompt
        assert "Fix the null check." in prompt
        assert "may already be resolved" in prompt
        assert "reach your own" in prompt

    def test_revision_context_included(self):
        prompt = reviewer_review(title="T", description="D", revision_context="Also fix X")
        assert "Revision Context" in prompt
        assert "Also fix X" in prompt

    def test_both_blocks_present(self):
        prompt = reviewer_review(
            title="T", description="D",
            revision_context="Fix X",
            prior_rejections="Old rejection",
        )
        assert "Revision Context" in prompt
        assert "Previous Review Rejections" in prompt

    def test_empty_prior_rejections_omitted(self):
        prompt = reviewer_review(title="T", description="D", prior_rejections="")
        assert "Previous Review Rejections" not in prompt


def _make_reviewer(review_text: str) -> ReviewerAgent:
    """Create a ReviewerAgent whose run() returns a fixed text response."""
    mock_client = MagicMock()
    mock_run = AgentRun(agent_type="reviewer", model="test-model", output=review_text)
    mock_client.run.return_value = mock_run
    mock_client.extract_text_response.return_value = review_text
    return ReviewerAgent(model="test-model", client=mock_client)


class TestReviewChangesInterface:
    """Tests for ReviewerAgent.review_changes with prior_rejections."""

    def _make_task(self) -> Task:
        return Task(title="Add feature", description="desc")

    def test_approve_no_prior(self):
        reviewer = _make_reviewer("APPROVE\nLooks good.")
        run, passed, text = reviewer.review_changes(self._make_task(), "/repo")
        assert passed is True

    def test_request_changes_no_prior(self):
        reviewer = _make_reviewer("REQUEST_CHANGES\nMissing null check.")
        run, passed, text = reviewer.review_changes(self._make_task(), "/repo")
        assert passed is False

    def test_prior_rejections_forwarded_to_prompt(self):
        """Verify prior_rejections ends up in the prompt sent to the client."""
        reviewer = _make_reviewer("APPROVE\nFixed.")
        task = self._make_task()
        prior = "=== Reviewer: m1 | REQUEST_CHANGES ===\nFix the null check."
        reviewer.review_changes(task, "/repo", prior_rejections=prior)
        prompt_sent = reviewer.client.run.call_args.kwargs["message"]
        assert "Fix the null check." in prompt_sent
        assert "Previous Review Rejections" in prompt_sent

    def test_no_prior_rejections_not_in_prompt(self):
        reviewer = _make_reviewer("APPROVE\nOK.")
        task = self._make_task()
        reviewer.review_changes(task, "/repo", prior_rejections="")
        prompt_sent = reviewer.client.run.call_args.kwargs["message"]
        assert "Previous Review Rejections" not in prompt_sent
