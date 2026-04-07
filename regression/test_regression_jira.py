"""Black-box real regression coverage for Jira assignment in dry-run mode."""

from __future__ import annotations

import json

import pytest

from core.models import TaskStatus


pytestmark = pytest.mark.regression


def test_real_jira_assignment_pipeline_runs_end_to_end_in_dry_run_mode(
    regression_harness_factory,
):
    harness = regression_harness_factory("tiny_python_app")

    create_result = harness.submit_develop_task(
        title="Prepare source task for Jira regression",
        description=(
            "Add a new multiply(a, b) helper to app/calculator.py, export it from the "
            "same module as the existing arithmetic helpers, and add pytest coverage in "
            "tests/test_calculator.py. Keep the code style and API shape consistent with "
            "the existing add and subtract helpers. After the task changes are committed "
            "and python -m pytest -q passes, stop immediately without extra repository "
            "exploration."
        ),
        file_path="app/calculator.py",
        line_number=1,
    )
    source_task_id = create_result["id"]
    source_task = harness.wait_for_task_terminal(source_task_id)
    assert source_task.status == TaskStatus.COMPLETED, harness.describe_task(
        source_task_id
    )

    assign_result = harness.assign_jira_for_task(source_task_id)
    assert assign_result["ok"] is True, assign_result

    updated_source = harness.wait_for_jira_result(source_task_id)
    assert updated_source.jira_status == "created", harness.describe_task(
        source_task_id
    )
    assert updated_source.jira_issue_key.startswith("DRYRUN-QA-"), (
        harness.describe_task(source_task_id)
    )
    assert updated_source.jira_issue_url.endswith(updated_source.jira_issue_key), (
        harness.describe_task(source_task_id)
    )
    assert updated_source.jira_payload_preview, harness.describe_task(source_task_id)

    payload = json.loads(updated_source.jira_payload_preview)
    fields = payload["fields"]
    assert fields["project"]["key"] == "QA"
    assert fields["summary"].startswith(f"[Doris Agent {source_task_id}]")
    assert "DorisExplorer" in fields.get("labels", [])
    assert fields["issuetype"]["name"] in {"Task", "Improvement"}
    assert fields["priority"]["name"] in {"Medium", "Low"}

    detail = harness.get_task_detail(source_task_id)
    run_types = {run["agent_type"] for run in detail["runs"]}
    assert "jira_assign" in run_types, detail
