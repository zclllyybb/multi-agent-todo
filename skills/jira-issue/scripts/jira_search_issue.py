#!/usr/bin/env python3
import json
from pathlib import Path
from typing import Iterable, Optional

import click
import requests
from requests.auth import HTTPBasicAuth

from jira_common import load_config, resolve_value


JQL_SEARCH_URL = "/rest/api/2/search"

# Default fields returned by search (compact output)
DEFAULT_FIELDS = [
    "key",
    "summary",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "labels",
    "created",
    "comment",
    "attachment",
]


def _build_auth_headers(
    jira_url: str,
    jira_token: str,
    jira_user: str,
    auth: str,
    dry: bool = False,
) -> tuple[dict, Optional[HTTPBasicAuth]]:
    if dry:
        return {}, None
    headers = {"Content-Type": "application/json"}
    request_auth = None
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {jira_token}"
    else:
        if not jira_user:
            raise click.UsageError("Basic auth requires --jira-user or JIRA_USER.")
        request_auth = HTTPBasicAuth(jira_user, jira_token)
    return headers, request_auth


def _build_jql(
    project_key: str,
    keyword: Optional[str],
    keyword_field: Optional[str],
    labels: Iterable[str],
    assignee: Optional[str],
    status: Optional[str],
    issue_type: Optional[str],
    priority: Optional[str],
    component: Optional[str],
    affects_version: Optional[str],
    fix_version: Optional[str],
    epic: Optional[str],
    reporter: Optional[str],
    raw_jql: Optional[str],
) -> str:
    if raw_jql:
        return raw_jql.strip()

    parts = []
    if project_key:
        parts.append(f'project = "{project_key}"')

    if keyword:
        field = keyword_field or "text"
        # Escape double quotes inside keyword
        escaped = keyword.replace('"', '\\"')
        parts.append(f'{field} ~ "{escaped}"')

    label_list = [str(lbl).strip() for lbl in labels if str(lbl).strip()]
    if len(label_list) == 1:
        parts.append(f'labels = "{label_list[0]}"')
    elif len(label_list) > 1:
        quoted = ", ".join(f'"{l}"' for l in label_list)
        parts.append(f"labels in ({quoted})")

    if assignee:
        parts.append(f'assignee = "{assignee}"')

    if status:
        parts.append(f'status = "{status}"')

    if issue_type:
        parts.append(f'issuetype = "{issue_type}"')

    if priority:
        parts.append(f'priority = "{priority}"')

    if component:
        parts.append(f'component = "{component}"')

    if affects_version:
        parts.append(f'affectedVersion = "{affects_version}"')

    if fix_version:
        parts.append(f'fixVersion = "{fix_version}"')

    if epic:
        parts.append(f'"Epic Link" = "{epic}"')

    if reporter:
        parts.append(f'reporter = "{reporter}"')

    if not parts:
        return ""
    return " AND ".join(parts) + " ORDER BY created DESC"


@click.command(help="Search Jira issues using JQL (REST API v2)")
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
    "--keyword",
    help="Text to search for in issue fields (use --keyword-field to specify field)",
)
@click.option(
    "--keyword-field",
    type=click.Choice(["summary", "description", "text", "comment", "environment"]),
    default="text",
    show_default=True,
    help="Field to search --keyword in (text = summary + description + comments)",
)
@click.option(
    "--label", "labels", multiple=True, help="Filter by label (repeatable)"
)
@click.option("--assignee", help="Filter by assignee username")
@click.option("--status", help="Filter by status (e.g. 'In Progress')")
@click.option("--issue-type", help="Filter by issue type (e.g. Bug, Task)")
@click.option("--priority", help="Filter by priority (e.g. High)")
@click.option("--component", help="Filter by component name")
@click.option("--affects-version", help="Filter by Affects Version")
@click.option("--fix-version", help="Filter by Fix Version")
@click.option("--epic", help="Filter by Epic issue key")
@click.option("--reporter", help="Filter by reporter username")
@click.option(
    "--jql",
    "raw_jql",
    help="Raw JQL query (overrides all other search options)",
)
@click.option(
    "--max-results",
    type=int,
    default=50,
    show_default=True,
    help="Maximum number of results to return (1-100)",
)
@click.option(
    "--fields",
    help="Comma-separated list of fields to return (defaults to key,summary,status,issuetype,priority,assignee,labels,created). Use * for all fields.",
)
@click.option("--output-json", is_flag=True, help="Output full JSON instead of compact format")
@click.option("--show-comments", is_flag=True, help="Show full comment bodies in compact output")
@click.option("--show-attachments", is_flag=True, help="Show attachment filenames and download URLs")
@click.option("--show-description", is_flag=True, help="Show issue description in compact output")
@click.option("--print-payload", is_flag=True, help="Print request payload")
@click.option("--dry-run", is_flag=True, help="Only print payload, do not search")
def main(
    jira_url: Optional[str],
    jira_user: Optional[str],
    jira_token: Optional[str],
    auth: str,
    project_key: Optional[str],
    keyword: Optional[str],
    keyword_field: Optional[str],
    labels: Iterable[str],
    assignee: Optional[str],
    status: Optional[str],
    issue_type: Optional[str],
    priority: Optional[str],
    component: Optional[str],
    affects_version: Optional[str],
    fix_version: Optional[str],
    epic: Optional[str],
    reporter: Optional[str],
    raw_jql: Optional[str],
    max_results: int,
    fields: Optional[str],
    output_json: bool,
    show_comments: bool,
    show_attachments: bool,
    show_description: bool,
    print_payload: bool,
    dry_run: bool,
) -> None:
    config = load_config()
    project_key = resolve_value(project_key, config, "JIRA_PROJECT", required=False)
    is_dry = bool(print_payload or dry_run)

    jql = _build_jql(
        project_key=project_key or "",
        keyword=keyword,
        keyword_field=keyword_field,
        labels=labels,
        assignee=assignee,
        status=status,
        issue_type=issue_type,
        priority=priority,
        component=component,
        affects_version=affects_version,
        fix_version=fix_version,
        epic=epic,
        reporter=reporter,
        raw_jql=raw_jql,
    )

    if fields is None:
        field_list = DEFAULT_FIELDS
    elif fields.strip() == "*":
        field_list = ["*all"]
    else:
        field_list = [f.strip() for f in fields.split(",") if f.strip()]

    payload: dict = {
        "jql": jql,
        "maxResults": max(max_results, 1),
    }
    if "summary" not in field_list and "*all" not in field_list:
        field_list.insert(0, "summary")
    if "key" not in field_list and "*all" not in field_list:
        field_list.insert(0, "key")

    if field_list != ["*all"]:
        payload["fields"] = field_list

    if print_payload or dry_run:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        if dry_run:
            return

    base_url = resolve_value(jira_url, config, "JIRA_URL", required=True).rstrip("/")
    jira_token_val = resolve_value(jira_token, config, "JIRA_TOKEN", required=True)
    jira_user_val = resolve_value(jira_user, config, "JIRA_USER") if auth == "basic" else ""
    headers, request_auth = _build_auth_headers(base_url, jira_token_val, jira_user_val, auth)

    url = f"{base_url}{JQL_SEARCH_URL}"
    resp = requests.post(
        url, headers=headers, auth=request_auth, json=payload, timeout=30
    )
    if resp.status_code >= 400:
        click.echo(f"Request failed: {resp.status_code}", err=True)
        click.echo(resp.text, err=True)
        raise SystemExit(1)

    data = resp.json()
    issues = data.get("issues", [])
    total = data.get("total", len(issues))
    is_last = data.get("isLast")
    next_token = data.get("nextPageToken")

    if not issues:
        click.echo("total=0")
        return

    if output_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return

    click.echo(f"total={total}")
    for i, issue in enumerate(issues):
        fields_data = issue.get("fields", {})

        # --- basic fields (single-line per issue) ---
        parts = [f"key={issue.get('key', '')}"]
        for k in field_list:
            if k in ("key", "*all", "comment", "attachment", "description"):
                continue
            raw = fields_data.get(k)
            if raw is None:
                parts.append(f"{k}=")
            elif isinstance(raw, dict):
                parts.append(f"{k}={raw.get('name', raw.get('displayName', str(raw)))}")
            elif isinstance(raw, list):
                elems = [e.get("name", str(e)) if isinstance(e, dict) else str(e) for e in raw]
                parts.append(f"{k}={','.join(elems)}" if elems else f"{k}=")
            else:
                parts.append(f"{k}={raw}")
        click.echo(" | ".join(parts))

        # --- description ---
        if show_description:
            desc = fields_data.get("description")
            if desc:
                desc_text = desc if isinstance(desc, str) else str(desc)
                click.echo(f"  description: {desc_text[:500]}{'...(truncated)' if len(desc_text) > 500 else ''}")

        # --- comments ---
        comment_data = fields_data.get("comment")
        if isinstance(comment_data, dict):
            cmt_total = comment_data.get("total", 0)
            cmt_list = comment_data.get("comments") or []
            parts.append(f"comments={cmt_total}")
            if show_comments and cmt_list:
                for cmt in cmt_list:
                    author = cmt.get("author", {}).get("displayName", "?")
                    created = cmt.get("created", "?")
                    body = cmt.get("body", "")
                    click.echo(f"  [comment] {author} @ {created}")
                    click.echo(f"    {body}")
        elif isinstance(comment_data, list):
            parts.append(f"comments={len(comment_data)}")
        if "comment" in field_list:
            cmt_total = 0
            if isinstance(comment_data, dict):
                cmt_total = comment_data.get("total", len(comment_data.get("comments") or []))
            elif isinstance(comment_data, list):
                cmt_total = len(comment_data)
            if not show_comments:
                click.echo(f"  comments={cmt_total}")

        # --- attachments ---
        attach_data = fields_data.get("attachment")
        if isinstance(attach_data, list):
            click.echo(f"  attachments={len(attach_data)}")
            if show_attachments and attach_data:
                for a in attach_data:
                    click.echo(f"    {a.get('filename','?')} ({a.get('size',0)} bytes, {a.get('mimeType','?')}) -> {a.get('content','?')}")

        if i < len(issues) - 1:
            click.echo("")

    if next_token:
        click.echo(f"nextPageToken={next_token}")
    if is_last is not None:
        click.echo(f"isLast={is_last}")


if __name__ == "__main__":
    main()
