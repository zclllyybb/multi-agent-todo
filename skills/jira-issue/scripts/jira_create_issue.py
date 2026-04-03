#!/usr/bin/env python3
import json
from pathlib import Path
from typing import Iterable, Optional

import click
import requests
from requests.auth import HTTPBasicAuth

from jira_common import load_config, read_text_input, resolve_value


def read_description(file_path: Path) -> str:
    return read_text_input(file_path, "--description-file")


def build_payload(
    project_key: str,
    issue_type: str,
    summary: str,
    description: str,
    labels: Iterable[str],
    components: Iterable[str],
    affects_versions: Iterable[str],
    fix_versions: Iterable[str],
    assignee: Optional[str],
    priority: Optional[str],
) -> dict:
    fields = {
        "project": {"key": project_key},
        "summary": summary,
        "description": description,
        "issuetype": {"name": issue_type},
    }
    label_list = [str(label).strip() for label in labels if str(label).strip()]
    if label_list:
        fields["labels"] = label_list
    component_list = [component for component in components if component]
    if component_list:
        fields["components"] = [{"name": name} for name in component_list]
    affects_list = [version for version in affects_versions if version]
    if affects_list:
        fields["versions"] = [{"name": name} for name in affects_list]
    fix_list = [version for version in fix_versions if version]
    if fix_list:
        fields["fixVersions"] = [{"name": name} for name in fix_list]
    if assignee:
        fields["assignee"] = {"name": assignee}
    if priority:
        fields["priority"] = {"name": priority}
    return {"fields": fields}


@click.command(help="Create a Jira issue using REST API v2")
@click.option("--jira-url", help="Jira base URL, default from JIRA_URL")
@click.option("--jira-user", help="Jira username/email, default from JIRA_USER")
@click.option("--jira-token", help="Jira API token, default from JIRA_TOKEN")
@click.option(
    "--auth",
    type=click.Choice(["bearer", "basic"]),
    default="bearer",
    show_default=True,
    help="Authentication method to use with the token",
)
@click.option("--project-key", help="Project key, default from JIRA_PROJECT")
@click.option(
    "--issue-type",
    default=None,
    help="Issue type, default from JIRA_ISSUE_TYPE or Bug",
)
@click.option("--summary", required=True, help="Issue summary")
@click.option(
    "--description-file",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Read description from file, or use - for stdin",
)
@click.option("--label", "labels", multiple=True, help="Add a label (repeatable)")
@click.option(
    "--component", "components", multiple=True, help="Add a component (repeatable)"
)
@click.option(
    "--affects-version",
    "affects_versions",
    multiple=True,
    help="Add an affects version (repeatable)",
)
@click.option(
    "--fix-version",
    "fix_versions",
    multiple=True,
    help="Add a fix version (repeatable)",
)
@click.option("--assignee", help="Assignee username (optional)")
@click.option("--priority", help="Priority name (optional)")
@click.option("--epic", help="Epic issue key to link this issue to (e.g., DORIS-24979)")
@click.option("--print-payload", is_flag=True, help="Print request payload")
@click.option("--dry-run", is_flag=True, help="Only print payload, do not create issue")
def main(
    jira_url: Optional[str],
    jira_user: Optional[str],
    jira_token: Optional[str],
    auth: str,
    project_key: Optional[str],
    issue_type: Optional[str],
    summary: str,
    description_file: Path,
    labels: Iterable[str],
    components: Iterable[str],
    affects_versions: Iterable[str],
    fix_versions: Iterable[str],
    assignee: Optional[str],
    priority: Optional[str],
    epic: Optional[str],
    print_payload: bool,
    dry_run: bool,
) -> None:
    config = load_config()
    project_key = resolve_value(project_key, config, "JIRA_PROJECT", required=True)
    issue_type = issue_type or config.get("JIRA_ISSUE_TYPE", "Bug")
    description_text = read_description(description_file)
    if assignee and "@" in assignee:
        click.echo(
            "Warning: assignee expects Jira username, not email (e.g. laihui).",
            err=True,
        )

    payload = build_payload(
        project_key=project_key,
        issue_type=issue_type,
        summary=summary,
        description=description_text,
        labels=labels,
        components=components,
        affects_versions=affects_versions,
        fix_versions=fix_versions,
        assignee=assignee,
        priority=priority,
    )

    if print_payload or dry_run:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        if dry_run:
            return

    jira_url = resolve_value(jira_url, config, "JIRA_URL", required=True).rstrip("/")
    jira_token = resolve_value(jira_token, config, "JIRA_TOKEN", required=True)
    jira_user = resolve_value(jira_user, config, "JIRA_USER") if auth == "basic" else ""
    headers = {"Content-Type": "application/json"}
    request_auth = None
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {jira_token}"
    else:
        if not jira_user:
            raise click.UsageError("Basic auth requires --jira-user or JIRA_USER.")
        request_auth = HTTPBasicAuth(jira_user, jira_token)

    url = f"{jira_url}/rest/api/2/issue"
    resp = requests.post(
        url, headers=headers, auth=request_auth, json=payload, timeout=20
    )
    if resp.status_code >= 400:
        click.echo(f"Request failed: {resp.status_code}", err=True)
        click.echo(resp.text, err=True)
        raise SystemExit(1)
    data = resp.json()
    key = data.get("key")
    self_url = data.get("self")
    if key:
        click.echo(f"key={key}")
    if self_url:
        click.echo(f"self={self_url}")

    # Link issue to epic if --epic was provided
    if key and epic:
        epic_key = str(epic).strip()
        epic_url = f"{jira_url}/rest/agile/1.0/epic/{epic_key}/issue"
        epic_payload = {"issues": [key]}
        epic_resp = requests.post(
            epic_url,
            headers=headers,
            auth=request_auth,
            json=epic_payload,
            timeout=20,
        )
        if epic_resp.status_code >= 400:
            click.echo(
                f"Warning: Failed to link issue to epic: {epic_resp.status_code}",
                err=True,
            )
            click.echo(epic_resp.text, err=True)
        else:
            click.echo(f"epic_linked={epic_key}")


if __name__ == "__main__":
    main()
