"""Tests for agents/reviewer.py: _evaluate_review pure logic and prompts."""

from unittest.mock import MagicMock, call

import pytest

from agents.prompts import coder_retry_feedback, reviewer_review, reviewer_review_patch
from agents.reviewer import ReviewerAgent, _MAX_REVIEWER_RETRIES
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
        assert (
            evaluator._evaluate_review("REQUEST_CHANGES\nPlease fix the bug.") is False
        )

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

    def test_empty_output_inconclusive(self, evaluator):
        """No signals at all → inconclusive (None), not a definite rejection."""
        assert evaluator._evaluate_review("") is None

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
            title="T",
            description="D",
            prior_rejections="=== Reviewer: m1 | REQUEST_CHANGES ===\nFix the null check.",
        )
        assert "Previous Review Rejections (for reference only)" in prompt
        assert "Fix the null check." in prompt
        assert "may already be resolved" in prompt
        assert "reach your own" in prompt

    def test_revision_context_included(self):
        prompt = reviewer_review(
            title="T", description="D", revision_context="Also fix X"
        )
        assert "Revision Context" in prompt
        assert "Also fix X" in prompt

    def test_both_blocks_present(self):
        prompt = reviewer_review(
            title="T",
            description="D",
            revision_context="Fix X",
            prior_rejections="Old rejection",
        )
        assert "Revision Context" in prompt
        assert "Previous Review Rejections" in prompt

    def test_revision_context_describes_latest_manual_feedback_only(self):
        prompt = reviewer_review(
            title="T", description="D", revision_context="Latest manual feedback"
        )
        assert "latest manual feedback" in prompt
        assert "Older manual-review notes are intentionally omitted" in prompt

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


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3: Verdict parsing — standalone lines & preamble disambiguation
# ─────────────────────────────────────────────────────────────────────────────


class TestVerdictParsing:
    """Regression tests for _evaluate_review verdict disambiguation."""

    def test_standalone_approve_line(self, evaluator):
        assert evaluator._evaluate_review("APPROVE\nLooks good.") is True

    def test_standalone_request_changes_line(self, evaluator):
        assert evaluator._evaluate_review("REQUEST_CHANGES\nFix the bug.") is False

    def test_preamble_both_keywords_approve_verdict(self, evaluator):
        """Real-world case: preamble mentions both keywords, verdict is APPROVE at end."""
        text = (
            "I detect evaluation intent — this is a code review of already-committed "
            "documentation, so my approach is committed-history inspection plus source "
            "cross-checks, then a strict APPROVE/REQUEST_CHANGES verdict based on "
            "factual accuracy only.APPROVE\n\nNo blocking issues found."
        )
        assert evaluator._evaluate_review(text) is True

    def test_deliberation_then_request_changes(self, evaluator):
        """Both keywords in deliberation, last one is REQUEST_CHANGES → reject."""
        text = "I considered APPROVE but ultimately REQUEST_CHANGES are needed."
        assert evaluator._evaluate_review(text) is False

    def test_standalone_approve_with_request_changes_in_body(self, evaluator):
        """Standalone APPROVE verdict line, REQUEST_CHANGES mentioned in feedback body."""
        text = (
            "APPROVE\nPreviously I would have said REQUEST_CHANGES but it's fixed now."
        )
        assert evaluator._evaluate_review(text) is True

    def test_standalone_request_changes_with_approve_in_body(self, evaluator):
        """Standalone REQUEST_CHANGES line, approve mentioned later."""
        text = "REQUEST_CHANGES\nI cannot approve this until the null check is added."
        assert evaluator._evaluate_review(text) is False

    def test_approve_slash_request_changes_preamble_then_standalone_approve(
        self, evaluator
    ):
        """APPROVE/REQUEST_CHANGES in preamble, then standalone APPROVE on next line."""
        text = (
            "My task is to give an APPROVE/REQUEST_CHANGES verdict.\n"
            "APPROVE\n"
            "The code looks correct."
        )
        assert evaluator._evaluate_review(text) is True


# ─────────────────────────────────────────────────────────────────────────────
# Fix A: Tri-state _evaluate_review + auto-retry on inconclusive output
# ─────────────────────────────────────────────────────────────────────────────


class TestEvaluateReviewTriState:
    """Verify _evaluate_review returns None for inconclusive output."""

    def test_no_signals_returns_none(self, evaluator):
        """Random text with no verdict keywords and no heuristic signals → None."""
        assert evaluator._evaluate_review("I will now begin my review process.") is None

    def test_only_whitespace_returns_none(self, evaluator):
        assert evaluator._evaluate_review("   \n\t  ") is None

    def test_heuristic_with_signals_still_returns_bool(self, evaluator):
        """If heuristic keywords are present (even without explicit verdict), return bool."""
        assert evaluator._evaluate_review("This looks good to me, LGTM") is True
        assert evaluator._evaluate_review("There is a bug here") is False


class TestReviewChangesAutoRetry:
    """Verify that review_changes auto-retries when output is inconclusive."""

    def _make_task(self) -> Task:
        return Task(title="Test", description="desc")

    def test_auto_retry_succeeds_on_second_attempt(self):
        """First call returns inconclusive, second returns APPROVE → passed."""
        mock_client = MagicMock()
        inconclusive_run = AgentRun(
            agent_type="reviewer", model="m", output="Planning..."
        )
        approve_run = AgentRun(agent_type="reviewer", model="m", output="APPROVE\nOK")
        mock_client.run.side_effect = [inconclusive_run, approve_run]
        mock_client.extract_text_response.side_effect = ["Planning...", "APPROVE\nOK"]

        reviewer = ReviewerAgent(model="m", client=mock_client)
        run, passed, text = reviewer.review_changes(self._make_task(), "/repo")

        assert passed is True
        assert text == "APPROVE\nOK"
        assert mock_client.run.call_count == 2

    def test_auto_retry_exhausted_defaults_to_reject(self):
        """All attempts return inconclusive → defaults to False."""
        mock_client = MagicMock()
        inconclusive_run = AgentRun(
            agent_type="reviewer", model="m", output="Thinking..."
        )
        mock_client.run.return_value = inconclusive_run
        mock_client.extract_text_response.return_value = "Thinking..."

        reviewer = ReviewerAgent(model="m", client=mock_client)
        run, passed, text = reviewer.review_changes(self._make_task(), "/repo")

        assert passed is False
        # 1 initial + _MAX_REVIEWER_RETRIES retries
        assert mock_client.run.call_count == 1 + _MAX_REVIEWER_RETRIES

    def test_definite_verdict_no_retry(self):
        """If first attempt has a clear verdict, no retry happens."""
        mock_client = MagicMock()
        run_obj = AgentRun(
            agent_type="reviewer", model="m", output="REQUEST_CHANGES\nBug"
        )
        mock_client.run.return_value = run_obj
        mock_client.extract_text_response.return_value = "REQUEST_CHANGES\nBug"

        reviewer = ReviewerAgent(model="m", client=mock_client)
        _, passed, _ = reviewer.review_changes(self._make_task(), "/repo")

        assert passed is False
        assert mock_client.run.call_count == 1  # no retry


class TestReviewPatchAutoRetry:
    """Same auto-retry logic for review_patch."""

    def _make_task(self) -> Task:
        return Task(
            title="Review PR",
            description="d",
            task_mode="review",
            review_input="https://github.com/org/repo/pull/1",
        )

    def test_patch_auto_retry_succeeds(self):
        mock_client = MagicMock()
        inconclusive_run = AgentRun(
            agent_type="reviewer", model="m", output="Let me check..."
        )
        approve_run = AgentRun(agent_type="reviewer", model="m", output="APPROVE\nLGTM")
        mock_client.run.side_effect = [inconclusive_run, approve_run]
        mock_client.extract_text_response.side_effect = [
            "Let me check...",
            "APPROVE\nLGTM",
        ]

        reviewer = ReviewerAgent(model="m", client=mock_client)
        _, passed, _ = reviewer.review_patch(self._make_task(), "/repo")
        assert passed is True
        assert mock_client.run.call_count == 2

    def test_patch_exhausted_defaults_to_reject(self):
        mock_client = MagicMock()
        run_obj = AgentRun(agent_type="reviewer", model="m", output="Hmm...")
        mock_client.run.return_value = run_obj
        mock_client.extract_text_response.return_value = "Hmm..."

        reviewer = ReviewerAgent(model="m", client=mock_client)
        _, passed, _ = reviewer.review_patch(self._make_task(), "/repo")
        assert passed is False
        assert mock_client.run.call_count == 1 + _MAX_REVIEWER_RETRIES


# ─────────────────────────────────────────────────────────────────────────────
# Fix B: Coder response channel (coder_response in reviewer prompt)
# ─────────────────────────────────────────────────────────────────────────────


class TestCoderResponseChannel:
    """Verify coder_response is included in the reviewer prompt."""

    def _make_task(self) -> Task:
        return Task(title="Feat", description="desc")

    def test_coder_response_in_prompt_builder(self):
        prompt = reviewer_review(
            title="T",
            description="D",
            coder_response="I chose approach X because Y.",
        )
        assert "Coder's Response" in prompt
        assert "I chose approach X because Y." in prompt
        assert "Consider these arguments on their merits" in prompt

    def test_coder_response_omitted_when_empty(self):
        prompt = reviewer_review(title="T", description="D", coder_response="")
        assert "Coder's Response" not in prompt

    def test_coder_response_forwarded_via_review_changes(self):
        """Verify coder_response reaches the prompt sent to the client."""
        reviewer = _make_reviewer("APPROVE\nOK")
        task = self._make_task()
        reviewer.review_changes(
            task,
            "/repo",
            coder_response="I disagree with the reviewer's suggestion because...",
        )
        prompt_sent = reviewer.client.run.call_args.kwargs["message"]
        assert "I disagree with the reviewer's suggestion because..." in prompt_sent
        assert "Coder's Response" in prompt_sent

    def test_all_sections_coexist(self):
        """coder_response, prior_rejections, and revision_context all appear together."""
        prompt = reviewer_review(
            title="T",
            description="D",
            revision_context="User says fix X",
            prior_rejections="Old rejection text",
            coder_response="Coder explanation",
        )
        assert "Revision Context" in prompt
        assert "Previous Review Rejections" in prompt
        assert "Coder's Response" in prompt


class TestCoderRetryFeedbackPrompt:
    def test_standard_retry_uses_review_feedback_section(self):
        prompt = coder_retry_feedback(
            review_feedback="Reviewer says fix null check",
            attempt=2,
        )
        assert "## Review Feedback (attempt 2)" in prompt
        assert "Reviewer says fix null check" in prompt
        assert "## Revise Context" not in prompt

    def test_revise_retry_separates_manual_and_prior_reviewer_feedback(self):
        prompt = coder_retry_feedback(
            review_feedback="combined fallback",
            attempt=0,
            manual_feedback="Latest manual instruction",
            prior_reviewer_feedback="REQUEST_CHANGES\nPrevious reviewer issue",
        )
        assert "## Revise Context (attempt 0)" in prompt
        assert "### Current Manual Feedback" in prompt
        assert "Latest manual instruction" in prompt
        assert "### Reviewer Feedback Immediately Before This Manual Feedback" in prompt
        assert "Previous reviewer issue" in prompt
        assert "Older manual-review inputs are intentionally omitted" in prompt
        assert "## Review Feedback" not in prompt


class TestReviewerReviewPatchPrompt:
    def test_review_patch_can_include_prior_rejections(self):
        prompt = reviewer_review_patch(
            title="Review T",
            review_input="patch",
            revision_context="Latest manual note",
            prior_rejections="REQUEST_CHANGES\nOld reviewer note",
        )
        assert "Additional Review Instructions" in prompt
        assert "Latest manual note" in prompt
        assert "Previous Review Rejections" in prompt
        assert "Old reviewer note" in prompt

    def test_review_patch_omits_prior_rejections_when_empty(self):
        prompt = reviewer_review_patch(
            title="Review T",
            review_input="patch",
            revision_context="Latest manual note",
            prior_rejections="",
        )
        assert "Previous Review Rejections" not in prompt
