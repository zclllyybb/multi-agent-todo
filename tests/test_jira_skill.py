"""Tests for executing the vendored Jira create script in dry-run mode."""

import json
import subprocess
import sys
from pathlib import Path


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
        "DorisExplorer",
        "--label",
        "planner",
        "--component",
        "query execution",
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
    assert fields["labels"] == ["DorisExplorer", "planner"]
    assert fields["components"] == [{"name": "query execution"}]
    assert fields["assignee"] == {"name": "alice"}
    assert fields["priority"] == {"name": "High"}


def test_jira_create_issue_dry_run_allows_empty_extra_labels_and_empty_component():
    payload = _run_jira_create_issue_dry_run(
        "--label",
        "DorisExplorer",
        description_text="Only fixed label",
    )

    fields = payload["fields"]
    assert fields["labels"] == ["DorisExplorer"]
    assert "components" not in fields
