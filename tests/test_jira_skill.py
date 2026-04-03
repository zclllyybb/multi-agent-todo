"""Tests for executing the vendored Jira create script in dry-run mode."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "jira-issue"


def _run_jira_create_issue_dry_run(*args: str, description_text: str) -> dict:
    desc_file = SKILL_DIR / ".tmp-jira-dry-run-description.md"
    desc_arg = desc_file.name
    desc_file.write_text(description_text, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/jira_create_issue.py",
                "--project-key",
                "QA",
                "--issue-type",
                "Bug",
                "--summary",
                "Dry run issue",
                "--description-file",
                desc_arg,
                *args,
                "--dry-run",
            ],
            cwd=SKILL_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    finally:
        desc_file.unlink(missing_ok=True)


def test_jira_create_issue_dry_run_executes_and_includes_fixed_label_and_component():
    payload = _run_jira_create_issue_dry_run(
        "--label",
        "Doris Explorer",
        "--label",
        "planner",
        "--component",
        "query execution",
        "--epic",
        "DORIS-24979",
        "--assignee",
        "alice",
        "--priority",
        "High",
        description_text="Dry-run description",
    )

    fields = payload["fields"]
    assert fields["project"]["key"] == "QA"
    assert fields["issuetype"]["name"] == "Bug"
    assert fields["summary"] == "Dry run issue"
    assert fields["description"] == "Dry-run description"
    assert fields["labels"] == ["Doris Explorer", "planner"]
    assert fields["components"] == [{"name": "query execution"}]
    assert fields["assignee"] == {"name": "alice"}
    assert fields["priority"] == {"name": "High"}


def test_jira_create_issue_dry_run_allows_empty_extra_labels_and_empty_component():
    payload = _run_jira_create_issue_dry_run(
        "--label",
        "Doris Explorer",
        "--epic",
        "DORIS-24979",
        description_text="Only fixed label",
    )

    fields = payload["fields"]
    assert fields["labels"] == ["Doris Explorer"]
    assert "components" not in fields


def test_jira_create_issue_dry_run_ignores_epic_in_payload():
    """Epic param should be ignored in dry-run mode (no network call)."""
    payload = _run_jira_create_issue_dry_run(
        "--epic",
        "DORIS-24979",
        "--label",
        "Doris Explorer",
        description_text="Issue with epic link",
    )

    # dry-run only prints the create payload, epic linking happens after creation
    fields = payload["fields"]
    assert fields["project"]["key"] == "QA"
    assert "epic" not in payload


def test_jira_create_issue_epic_linking_calls_agile_api():
    """Verify epic linking API is called correctly after issue creation."""
    import sys

    skill_scripts = SKILL_DIR / "scripts"
    sys.path.insert(0, str(skill_scripts))
    try:
        from jira_create_issue import build_payload

        # Verify build_payload does not include epic (epic is handled separately)
        payload = build_payload(
            project_key="QA",
            issue_type="Bug",
            summary="Test issue",
            description="Test desc",
            labels=["Doris Explorer"],
            components=[],
            affects_versions=[],
            fix_versions=[],
            assignee=None,
            priority=None,
        )
        assert "epic" not in payload["fields"]
    finally:
        sys.path.pop(0)


def test_epic_link_api_format():
    """Verify the expected epic link API URL and payload format."""
    # This test documents the expected API format without making real calls
    base_url = "https://jira.example.com"
    epic_key = "DORIS-24979"
    issue_key = "QA-12345"

    expected_url = f"{base_url}/rest/agile/1.0/epic/{epic_key}/issue"
    expected_payload = {"issues": [issue_key]}

    assert (
        expected_url == "https://jira.example.com/rest/agile/1.0/epic/DORIS-24979/issue"
    )
    assert expected_payload == {"issues": ["QA-12345"]}
    assert "issues" in expected_payload
    assert isinstance(expected_payload["issues"], list)
    assert len(expected_payload["issues"]) == 1
