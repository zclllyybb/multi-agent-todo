import json
import os
import subprocess
import sys
from pathlib import Path


def test_regression_forced_dry_run_emits_synthetic_key_self_and_payload(tmp_path):
    skill_dir = Path(__file__).resolve().parents[1] / "skills" / "jira-issue"
    desc = tmp_path / "desc.md"
    desc.write_text("Regression body", encoding="utf-8")

    env = os.environ.copy()
    env["MULTI_AGENT_TODO_JIRA_DRY_RUN"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/jira_create_issue.py",
            "--project-key",
            "QA",
            "--issue-type",
            "Task",
            "--summary",
            "Regression summary",
            "--description-file",
            str(desc),
            "--label",
            "DorisExplorer",
            "--epic",
            "QA-1",
        ],
        cwd=skill_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "key=DRYRUN-QA-1" in result.stdout
    assert "self=https://dry-run.invalid/rest/api/2/issue/DRYRUN-QA-1" in result.stdout
    payload_line = next(
        line for line in result.stdout.splitlines() if line.startswith("payload=")
    )
    payload = json.loads(payload_line.split("=", 1)[1])
    assert payload["fields"]["project"]["key"] == "QA"
    assert payload["fields"]["summary"] == "Regression summary"
    assert payload["fields"]["labels"] == ["DorisExplorer"]
