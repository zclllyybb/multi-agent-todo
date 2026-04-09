"""Tests for Fix C: NEEDS_ARBITRATION status and resolve_arbitration logic."""

from unittest.mock import MagicMock, patch

import pytest

from core.models import AgentRun, Task, TaskStatus, TaskPriority, TaskSource


def _make_orchestrator(tmp_db):
    """Return a minimal Orchestrator with mocked dependencies."""
    from core.orchestrator import Orchestrator

    cfg = {
        "repo": {
            "path": "/fake/repo",
            "base_branch": "master",
            "worktree_dir": "/fake/wt",
            "worktree_hooks": [],
        },
        "opencode": {
            "planner_model": "m",
            "coder_model_default": "m",
            "reviewer_models": [],
        },
        "orchestrator": {"max_retries": 1, "max_workers": 1, "max_parallel_tasks": 2},
        "database": {"path": ":memory:"},
        "publish": {"remote": "origin"},
    }
    with (
        patch("core.orchestrator.WorktreeManager"),
        patch("core.orchestrator.OpenCodeClient"),
        patch("core.orchestrator.PlannerAgent"),
        patch("core.orchestrator.Database", return_value=tmp_db),
    ):
        orch = Orchestrator.__new__(Orchestrator)
        orch.config = cfg
        orch.db = tmp_db
        orch.worktree_mgr = MagicMock()
        orch.client = MagicMock()
        orch.dep_tracker = MagicMock()
        orch._coder_by_complexity = {}
        orch._default_coder = MagicMock()
        orch.reviewers = []
        orch._executor = MagicMock()
        orch._pool = MagicMock()
        orch._futures = {}
        orch._lock = __import__("threading").Lock()
    return orch


class TestResolveArbitrationApprove:
    """Test the 'approve' action of resolve_arbitration."""

    def test_approve_sets_completed(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            worktree_path="/wt/task",
            branch_name="agent/task-a",
            error="Review failed after 5 attempts — needs human arbitration",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "approve")

        assert result.get("ok") is True
        assert result["action"] == "approve"
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.COMPLETED
        assert saved.review_pass is True
        assert saved.error == ""
        assert saved.completed_at > 0


class TestResolveArbitrationReject:
    """Test the 'reject' action of resolve_arbitration."""

    def test_reject_sets_failed(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            worktree_path="/wt/task",
            branch_name="agent/task-b",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "reject", feedback="Not needed")

        assert result.get("ok") is True
        assert result["action"] == "reject"
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.FAILED
        assert saved.error == "Not needed"

    def test_reject_default_error_message(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.NEEDS_ARBITRATION, worktree_path="/wt")
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "reject")

        saved = tmp_db.get_task(task.id)
        assert saved.error == "Rejected by human arbitration"


class TestResolveArbitrationRevise:
    """Test the 'revise' action of resolve_arbitration."""

    def test_revise_delegates_to_revise_task(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            worktree_path="/wt/task",
            branch_name="agent/task-c",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "revise", feedback="Please fix X")

        # revise_task should have been called, which resets status to PENDING
        assert result.get("ok") is True
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.PENDING
        assert saved.user_feedback == "Please fix X"

    def test_revise_requires_feedback(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.NEEDS_ARBITRATION, worktree_path="/wt")
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "revise", feedback="")

        assert "error" in result
        assert "feedback" in result["error"].lower()


class TestResolveArbitrationErrors:
    """Test error handling in resolve_arbitration."""

    def test_task_not_found(self, tmp_db):
        orch = _make_orchestrator(tmp_db)
        result = orch.resolve_arbitration("nonexistent", "approve")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_wrong_status(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.COMPLETED)
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "approve")

        assert "error" in result
        assert "not awaiting arbitration" in result["error"].lower()

    def test_unknown_action(self, tmp_db, make_task):
        task = make_task(status=TaskStatus.NEEDS_ARBITRATION, worktree_path="/wt")
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resolve_arbitration(task.id, "maybe")

        assert "error" in result
        assert "maybe" in result["error"]


class TestNeedsArbitrationInCleanAndRevise:
    """Verify that NEEDS_ARBITRATION tasks can be cleaned and revised."""

    def test_clean_accepts_needs_arbitration(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            branch_name="agent/task-x",
            worktree_path="/wt/agent/task-x",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)
        orch.worktree_mgr.remove_worktree.return_value = None

        result = orch.clean_task(task.id)

        assert result == {"cleaned": True, "branch": "agent/task-x"}

    def test_revise_accepts_needs_arbitration(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.NEEDS_ARBITRATION,
            worktree_path="/wt/task",
            branch_name="agent/task-y",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.revise_task(task.id, "Fix the issue please")

        assert result.get("ok") is True
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.PENDING
        assert saved.user_feedback == "Fix the issue please"


class TestResumeTask:
    """Tests for manual resume of failed coder runs."""

    def test_resume_failed_task_dispatches_resume_pipeline(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.FAILED,
            worktree_path="/wt/task",
            branch_name="agent/task-r1",
            task_mode="develop",
            session_ids={"coder": ["ses_resume_1"]},
            error="Coder run timed out",
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)
        orch._dispatch_resume = MagicMock(return_value=True)

        result = orch.resume_task(task.id, "Continue")

        assert result.get("ok") is True
        assert result["session_id"] == "ses_resume_1"
        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.PENDING
        assert saved.error == ""
        assert saved.user_feedback == "Continue"

        runs = tmp_db.get_runs_for_task(task.id)
        manual_runs = [r for r in runs if r.agent_type == "manual_review"]
        assert len(manual_runs) == 1
        assert manual_runs[0].output == "Continue"
        orch._dispatch_resume.assert_called_once_with(task.id, "Continue")

    def test_resume_requires_failed_status(self, tmp_db, make_task):
        task = make_task(
            status=TaskStatus.COMPLETED,
            worktree_path="/wt/task",
            task_mode="develop",
            session_ids={"coder": ["ses_resume_2"]},
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        result = orch.resume_task(task.id, "Continue")

        assert "error" in result
        assert "Cannot resume" in result["error"]

    def test_resume_pipeline_raw_continue_completes_task(self, tmp_db, make_task):
        from core.models import AgentRun

        task = make_task(
            status=TaskStatus.PENDING,
            worktree_path="/wt/task",
            branch_name="agent/task-r2",
            task_mode="develop",
            max_retries=0,
            session_ids={"coder": ["ses_resume_raw"]},
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        code_output = (
            '{"type":"step_start"}\n'
            '{"type":"text","part":{"text":"done"}}\n'
            '{"type":"step_finish","part":{"reason":"stop"}}\n'
        )
        code_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="Continue",
            output=code_output,
            exit_code=0,
            session_id="ses_resume_raw",
        )
        orch._default_coder.continue_session.return_value = (code_run, "done")
        orch.client.is_output_complete.return_value = True

        reviewer = MagicMock()
        reviewer.model = "rev-m"
        review_run = AgentRun(
            task_id=task.id,
            agent_type="reviewer",
            model="rev-m",
            prompt="",
            output="APPROVE",
            exit_code=0,
            session_id="ses_review",
        )
        reviewer.review_changes.return_value = (review_run, True, "APPROVE")
        orch.reviewers = [reviewer]

        orch._revise_task_pipeline(task.id, "Continue", True)

        saved = tmp_db.get_task(task.id)
        assert saved.status == TaskStatus.COMPLETED
        assert saved.review_pass is True
        assert saved.code_output == "done"
        orch._default_coder.continue_session.assert_called_once()
        call_args, call_kwargs = orch._default_coder.continue_session.call_args
        assert call_args[1] == "/wt/task"
        assert call_kwargs["user_message"] == "Continue"
        assert call_kwargs["session_id"] == "ses_resume_raw"

    def test_resume_pipeline_passes_only_last_coder_text_block_to_reviewer(
        self, tmp_db, make_task
    ):
        from core.models import AgentRun

        task = make_task(
            status=TaskStatus.PENDING,
            worktree_path="/wt/task",
            branch_name="agent/task-r3",
            task_mode="develop",
            max_retries=0,
            session_ids={"coder": ["ses_resume_last_block"]},
        )
        tmp_db.save_task(task)
        orch = _make_orchestrator(tmp_db)

        code_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="Continue",
            output="full resumed transcript",
            exit_code=0,
            session_id="ses_resume_last_block",
        )
        orch._default_coder.continue_session.return_value = (
            code_run,
            "full resumed transcript",
        )
        orch.client.is_output_complete.return_value = True
        orch.client.extract_last_text_block.return_value = "final resumed summary"

        reviewer = MagicMock()
        reviewer.model = "rev-m"
        review_run = AgentRun(
            task_id=task.id,
            agent_type="reviewer",
            model="rev-m",
            prompt="",
            output="APPROVE",
            exit_code=0,
            session_id="ses_review",
        )
        reviewer.review_changes.return_value = (review_run, True, "APPROVE")
        orch.reviewers = [reviewer]

        orch._revise_task_pipeline(task.id, "Continue", True)

        orch.client.extract_last_text_block.assert_called_once_with(code_run.output)
        assert reviewer.review_changes.call_args.kwargs["coder_response"] == (
            "final resumed summary"
        )
        assert (
            reviewer.review_changes.call_args.kwargs["coder_response"]
            != "full resumed transcript"
        )


class TestManualReviewContextAccumulation:
    def test_revise_pipeline_uses_only_latest_manual_feedback_for_revision_context(
        self, tmp_db, make_task
    ):
        task = make_task(
            status=TaskStatus.PENDING,
            worktree_path="/wt/task",
            branch_name="agent/task-manual-history",
            task_mode="develop",
            max_retries=0,
            session_ids={"coder": ["ses_manual_history"]},
        )
        tmp_db.save_task(task)

        tmp_db.save_agent_run(
            AgentRun(
                task_id=task.id,
                agent_type="manual_review",
                model="user",
                output="First manual review note",
                created_at=1.0,
            )
        )
        tmp_db.save_agent_run(
            AgentRun(
                task_id=task.id,
                agent_type="manual_review",
                model="user",
                output="Second manual review note",
                created_at=2.0,
            )
        )

        orch = _make_orchestrator(tmp_db)

        code_output = (
            '{"type":"step_start"}\n'
            '{"type":"text","part":{"text":"done"}}\n'
            '{"type":"step_finish","part":{"reason":"stop"}}\n'
        )
        code_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="Continue",
            output=code_output,
            exit_code=0,
            session_id="ses_manual_history",
        )
        orch._default_coder.continue_session.return_value = (code_run, "done")
        orch.client.is_output_complete.return_value = True
        orch.client.extract_last_text_block.return_value = "done"

        reviewer = MagicMock()
        reviewer.model = "rev-m"
        review_run = AgentRun(
            task_id=task.id,
            agent_type="reviewer",
            model="rev-m",
            prompt="",
            output="APPROVE",
            exit_code=0,
            session_id="ses_review",
        )
        reviewer.review_changes.return_value = (review_run, True, "APPROVE")
        orch.reviewers = [reviewer]

        orch._revise_task_pipeline(task.id, "Continue", True)

        revision_context = reviewer.review_changes.call_args.kwargs["revision_context"]
        assert "First manual review note" not in revision_context
        assert "Second manual review note" in revision_context

    def test_revise_pipeline_first_coder_retry_prompt_keeps_latest_manual_feedback_and_immediate_prior_reviewer_feedback(
        self, tmp_db, make_task
    ):
        task = make_task(
            status=TaskStatus.PENDING,
            worktree_path="/wt/task",
            branch_name="agent/task-manual-boundary",
            task_mode="develop",
            max_retries=0,
            session_ids={"coder": ["ses_manual_boundary"]},
        )
        tmp_db.save_task(task)

        tmp_db.save_agent_run(
            AgentRun(
                task_id=task.id,
                agent_type="manual_review",
                model="user",
                output="Old manual feedback that should not be replayed",
                created_at=1.0,
            )
        )
        tmp_db.save_agent_run(
            AgentRun(
                task_id=task.id,
                agent_type="reviewer",
                model="rev-old",
                output="REQUEST_CHANGES\nPrevious reviewer issue to keep",
                created_at=2.0,
            )
        )
        tmp_db.save_agent_run(
            AgentRun(
                task_id=task.id,
                agent_type="manual_review",
                model="user",
                output="Latest manual feedback to keep",
                created_at=3.0,
            )
        )

        orch = _make_orchestrator(tmp_db)

        code_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="revise",
            output="coder output",
            exit_code=0,
            session_id="ses_manual_boundary",
            created_at=4.0,
        )
        orch._default_coder.retry_with_feedback.return_value = (
            code_run,
            "coder output",
        )
        orch.client.is_output_complete.return_value = True
        orch.client.extract_last_text_block.return_value = "coder summary"
        orch.client.extract_last_text_block_or_raw.return_value = (
            "REQUEST_CHANGES\nPrevious reviewer issue to keep"
        )

        reviewer = MagicMock()
        reviewer.model = "rev-m"
        review_run = AgentRun(
            task_id=task.id,
            agent_type="reviewer",
            model="rev-m",
            prompt="",
            output="APPROVE",
            exit_code=0,
            session_id="ses_review",
            created_at=5.0,
        )
        reviewer.review_changes.return_value = (review_run, True, "APPROVE")
        orch.reviewers = [reviewer]

        orch._revise_task_pipeline(task.id)

        retry_kwargs = orch._default_coder.retry_with_feedback.call_args.kwargs
        assert retry_kwargs["manual_feedback"] == "Latest manual feedback to keep"
        assert retry_kwargs["prior_reviewer_feedback"] == (
            "REQUEST_CHANGES\nPrevious reviewer issue to keep"
        )
        assert (
            retry_kwargs["review_feedback"]
            == "Latest manual feedback to keep\n\nREQUEST_CHANGES\nPrevious reviewer issue to keep"
        )
        assert (
            "Old manual feedback that should not be replayed"
            not in retry_kwargs["review_feedback"]
        )
        revision_context = reviewer.review_changes.call_args.kwargs["revision_context"]
        assert revision_context == "Latest manual feedback to keep"

    def test_revise_pipeline_omits_prior_reviewer_feedback_when_previous_verdict_was_approve(
        self, tmp_db, make_task
    ):
        task = make_task(
            status=TaskStatus.PENDING,
            worktree_path="/wt/task",
            branch_name="agent/task-manual-approve",
            task_mode="develop",
            max_retries=0,
            session_ids={"coder": ["ses_manual_approve"]},
        )
        tmp_db.save_task(task)

        tmp_db.save_agent_run(
            AgentRun(
                task_id=task.id,
                agent_type="reviewer",
                model="rev-ok",
                output="APPROVE\nLooks good already",
                created_at=1.0,
            )
        )
        tmp_db.save_agent_run(
            AgentRun(
                task_id=task.id,
                agent_type="manual_review",
                model="user",
                output="Latest manual feedback only",
                created_at=2.0,
            )
        )

        orch = _make_orchestrator(tmp_db)

        code_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="revise",
            output="coder output",
            exit_code=0,
            session_id="ses_manual_approve",
            created_at=3.0,
        )
        orch._default_coder.retry_with_feedback.return_value = (
            code_run,
            "coder output",
        )
        orch.client.is_output_complete.return_value = True
        orch.client.extract_last_text_block.return_value = "coder summary"
        orch.client.extract_last_text_block_or_raw.return_value = (
            "APPROVE\nLooks good already"
        )

        reviewer = MagicMock()
        reviewer.model = "rev-m"
        review_run = AgentRun(
            task_id=task.id,
            agent_type="reviewer",
            model="rev-m",
            prompt="",
            output="APPROVE",
            exit_code=0,
            session_id="ses_review",
            created_at=4.0,
        )
        reviewer.review_changes.return_value = (review_run, True, "APPROVE")
        orch.reviewers = [reviewer]

        orch._revise_task_pipeline(task.id)

        retry_kwargs = orch._default_coder.retry_with_feedback.call_args.kwargs
        assert retry_kwargs["manual_feedback"] == "Latest manual feedback only"
        assert retry_kwargs["prior_reviewer_feedback"] == ""
        assert retry_kwargs["review_feedback"] == "Latest manual feedback only"

    def test_revise_pipeline_retry_passes_only_last_reviewer_text_block_to_coder(
        self, tmp_db, make_task
    ):
        task = make_task(
            status=TaskStatus.PENDING,
            worktree_path="/wt/task",
            branch_name="agent/task-review-feedback",
            task_mode="develop",
            max_retries=1,
            session_ids={"coder": ["ses_manual_retry"]},
        )
        tmp_db.save_task(task)

        orch = _make_orchestrator(tmp_db)

        first_code_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="revise",
            output="first coder output",
            exit_code=0,
            session_id="ses_manual_retry",
            created_at=1.0,
        )
        second_code_run = AgentRun(
            task_id=task.id,
            agent_type="coder",
            model="m",
            prompt="revise-again",
            output="second coder output",
            exit_code=0,
            session_id="ses_manual_retry",
            created_at=3.0,
        )
        orch._default_coder.retry_with_feedback.side_effect = [
            (first_code_run, "first coder output"),
            (second_code_run, "second coder output"),
        ]
        orch.client.is_output_complete.return_value = True
        orch.client.extract_last_text_block.side_effect = [
            "first coder summary",
            "second coder summary",
        ]
        orch.client.extract_last_text_block_or_raw.return_value = (
            "last reviewer text block"
        )

        reviewer = MagicMock()
        reviewer.model = "rev-m"
        reject_run = AgentRun(
            task_id=task.id,
            agent_type="reviewer",
            model="rev-m",
            prompt="",
            output="review transcript with wrapper",
            exit_code=0,
            session_id="ses_review_1",
            created_at=2.0,
        )
        approve_run = AgentRun(
            task_id=task.id,
            agent_type="reviewer",
            model="rev-m",
            prompt="",
            output="APPROVE",
            exit_code=0,
            session_id="ses_review_2",
            created_at=4.0,
        )
        reviewer.review_changes.side_effect = [
            (reject_run, False, "Please fix the follow-up issue"),
            (approve_run, True, "APPROVE"),
        ]
        orch.reviewers = [reviewer]

        orch._revise_task_pipeline(task.id)

        retry_kwargs = orch._default_coder.retry_with_feedback.call_args.kwargs
        assert retry_kwargs["review_feedback"] == "last reviewer text block"
        assert retry_kwargs["review_feedback"] != (
            "=== Reviewer: rev-m | REQUEST_CHANGES ===\nPlease fix the follow-up issue"
        )
        orch.client.extract_last_text_block_or_raw.assert_called_with(reject_run.output)
