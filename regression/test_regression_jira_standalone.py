"""Black-box regression coverage for standalone Jira-mode tasks."""

from __future__ import annotations

import json

import pytest

from core.models import TaskStatus


pytestmark = pytest.mark.regression


def test_real_standalone_jira_task_runs_end_to_end_in_dry_run_mode(
    regression_harness_factory,
):
    harness = regression_harness_factory("tiny_python_app")

    create_result = harness.submit_jira_task(
        title="File standalone regression Jira task",
        description=(
            "Create a Jira issue describing a regression-only validation problem in the "
            "calculator fixture. Use the configured dry-run Jira path and stop after the "
            "issue payload is produced."
        ),
    )
    task_id = create_result["id"]

    completed = harness.wait_for_task_terminal(task_id, timeout_sec=1800)
    assert completed.status == TaskStatus.COMPLETED, harness.describe_task(task_id)
    assert completed.task_mode == "jira", harness.describe_task(task_id)
    assert completed.jira_status == "created", harness.describe_task(task_id)
    assert completed.jira_issue_key.startswith("DRYRUN-QA-"), harness.describe_task(
        task_id
    )
    assert completed.jira_issue_url.endswith(completed.jira_issue_key), (
        harness.describe_task(task_id)
    )
    assert completed.jira_payload_preview, harness.describe_task(task_id)

    detail = harness.get_task_detail(task_id)
    run_types = {run["agent_type"] for run in detail["runs"]}
    assert "jira_assign" in run_types, detail

    payload = json.loads(completed.jira_payload_preview)
    fields = payload["fields"]
    assert fields["project"]["key"] == "QA"
    assert fields["summary"].startswith(f"[Doris Agent {task_id}]")
    assert "DorisExplorer" in fields.get("labels", [])
    assert fields["issuetype"]["name"] in {"Task", "Improvement"}
    assert fields["priority"]["name"] in {"Medium", "Low"}
