#!/usr/bin/env python3
import json
from pathlib import Path
from typing import Optional

import click
import requests
from requests.auth import HTTPBasicAuth

from jira_common import load_config, read_text_input, resolve_value


def read_comment(file_path: Path) -> str:
    return read_text_input(file_path, "--comment-file")


@click.command(help="Add a comment to an existing Jira issue (REST API v2)")
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
@click.option("--issue-key", required=True, help="Target issue key, e.g. DORIS-12345")
@click.option(
    "--comment-file",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Read comment text from file, or use - for stdin",
)
@click.option("--print-payload", is_flag=True, help="Print request payload")
@click.option("--dry-run", is_flag=True, help="Only print payload, do not send")
def main(
    jira_url: Optional[str],
    jira_user: Optional[str],
    jira_token: Optional[str],
    auth: str,
    issue_key: str,
    comment_file: Path,
    print_payload: bool,
    dry_run: bool,
) -> None:
    config = load_config()
    comment_text = read_comment(comment_file)

    payload = {"body": comment_text}

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

    url = f"{jira_url}/rest/api/2/issue/{issue_key}/comment"
    resp = requests.post(url, headers=headers, auth=request_auth, json=payload, timeout=20)
    if resp.status_code >= 400:
        click.echo(f"Request failed: {resp.status_code}", err=True)
        click.echo(resp.text, err=True)
        raise SystemExit(1)
    data = resp.json()
    comment_id = data.get("id")
    self_url = data.get("self")
    if comment_id:
        click.echo(f"comment_id={comment_id}")
    if self_url:
        click.echo(f"self={self_url}")


if __name__ == "__main__":
    main()
