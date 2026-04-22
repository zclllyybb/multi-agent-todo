"""Task markdown tracking and Claude skill bootstrap helpers."""

from __future__ import annotations

import logging
import os
import stat
import textwrap
from pathlib import Path

from core.models import Task

log = logging.getLogger(__name__)

SKILL_NAME = "opencode-session-ask"
SKILL_BASE_DIR = Path.home() / ".claude" / "skills" / SKILL_NAME
SKILL_FILE = SKILL_BASE_DIR / "SKILL.md"
SKILL_SCRIPT = SKILL_BASE_DIR / "ask_opencode_session.sh"


def task_notes_dir_for(db_path: str) -> Path:
    db_parent = Path(db_path).resolve().parent
    return db_parent / "task-notes"


def task_note_path(task: Task, db_path: str) -> Path:
    return task_notes_dir_for(db_path) / f"{task.id}.md"


def render_task_note(task: Task) -> str:
    planner_sessions = _list_sessions(task, "planner")
    coder_sessions = _list_sessions(task, "coder")
    reviewer_sessions = _list_sessions(task, "reviewer")
    lines = [
        f"# Task {task.id}",
        "",
        f"- Title: {task.title or '-'}",
        f"- Status: {task.status.value}",
        f"- Priority: {task.priority.value}",
        f"- Mode: {task.task_mode or 'develop'}",
        f"- Source: {task.source.value}",
        f"- Branch: {task.branch_name or '-'}",
        f"- Worktree: {task.worktree_path or '-'}",
        f"- Parent: {task.parent_id or '-'}",
        f"- Force Single Task: {'yes' if task.force_no_split else 'no'}",
        "",
        "## Description",
        "",
        task.description or "-",
        "",
        "## Sessions",
        "",
        "### Planner",
        "",
        *_session_lines(planner_sessions),
        "",
        "### Coder",
        "",
        *_session_lines(coder_sessions),
        "",
        "### Reviewer",
        "",
        *_session_lines(reviewer_sessions),
        "",
    ]
    return "\n".join(lines)


def write_task_note(task: Task, db_path: str):
    path = task_note_path(task, db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_task_note(task), encoding="utf-8")


def ensure_skill_initialized(logger: logging.Logger | None = None) -> list[str]:
    logger = logger or log
    created: list[str] = []
    SKILL_BASE_DIR.mkdir(parents=True, exist_ok=True)

    if not SKILL_FILE.exists():
        SKILL_FILE.write_text(_skill_markdown(), encoding="utf-8")
        created.append(str(SKILL_FILE))
        logger.info("Initialized Claude skill file: %s", SKILL_FILE)
    else:
        logger.info("Claude skill file already present: %s", SKILL_FILE)

    script_content = _skill_script()
    if not SKILL_SCRIPT.exists() or SKILL_SCRIPT.read_text(encoding="utf-8") != script_content:
        SKILL_SCRIPT.write_text(script_content, encoding="utf-8")
        current_mode = SKILL_SCRIPT.stat().st_mode
        SKILL_SCRIPT.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        created.append(str(SKILL_SCRIPT))
        logger.info("Initialized Claude skill helper script: %s", SKILL_SCRIPT)
    else:
        logger.info("Claude skill helper script already present: %s", SKILL_SCRIPT)

    return created


def _list_sessions(task: Task, agent_type: str) -> list[str]:
    values = task.session_ids.get(agent_type, [])
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        sid = str(value or "").strip()
        if not sid or sid in seen:
            continue
        result.append(sid)
        seen.add(sid)
    return result


def _session_lines(session_ids: list[str]) -> list[str]:
    if not session_ids:
        return ["- None"]
    return [f"- `{sid}`" for sid in session_ids]


def _skill_markdown() -> str:
    script_name = SKILL_SCRIPT.name
    return textwrap.dedent(
        f"""\
        ---
        name: {SKILL_NAME}
        description: Use when you need to ask a question to a known existing opencode session without modifying the original conversation.
        ---

        # OpenCode Session Ask

        ## Overview

        Use this skill when another agent session already exists and you need a direct answer from that session.
        Always fork the target session before asking, so the original session remains unchanged.

        ## Inputs

        You need:
        - a target session id such as `ses_...`
        - a short explicit question
        - the working directory for the related repository

        ## Command

        Run the helper script in this skill directory:

        ```bash
        {script_name} --session <SESSION_ID> --dir <WORK_DIR> --question "<QUESTION>"
        ```

        ## Rules

        - Always use `opencode run -s <SESSION_ID> --fork` under the hood.
        - Never continue the original session directly.
        - Read and return only the final text block from the forked run.
        - If the tool returns no final text block, treat that as failure.
        - Keep the question concise and specific.
        - Use the returned answer exactly like other inter-agent text handoff in this system.

        ## Output

        Return only the extracted final answer text from the forked session.
        Do not include raw JSON events unless debugging is explicitly required.
        """
    )


def _skill_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

session_id=""
work_dir=""
question=""
model=""
agent=""
variant=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      session_id="$2"
      shift 2
      ;;
    --dir)
      work_dir="$2"
      shift 2
      ;;
    --question)
      question="$2"
      shift 2
      ;;
    --model)
      model="$2"
      shift 2
      ;;
    --agent)
      agent="$2"
      shift 2
      ;;
    --variant)
      variant="$2"
      shift 2
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$session_id" || -z "$work_dir" || -z "$question" ]]; then
  printf 'usage: %s --session <sid> --dir <work_dir> --question <text> [--model <model>] [--agent <agent>] [--variant <variant>]\n' "$0" >&2
  exit 2
fi

cmd=(opencode run --dir "$work_dir" --format json -s "$session_id" --fork)
if [[ -n "$model" ]]; then
  cmd+=(--model "$model")
fi
if [[ -n "$agent" ]]; then
  cmd+=(--agent "$agent")
fi
if [[ -n "$variant" ]]; then
  cmd+=(--variant "$variant")
fi
cmd+=("$question")

output="$(${cmd[@]})"

tmp_json="$(mktemp)"
trap 'rm -f "$tmp_json"' EXIT
printf '%s\n' "$output" > "$tmp_json"

python3 - "$tmp_json" <<'PY'
import json
import sys

path = sys.argv[1]
steps = []
current_step = None
with open(path, encoding="utf-8") as f:
    for raw in f:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        ev_type = event.get("type", "")
        part = event.get("part", {})
        if not isinstance(part, dict):
            part = {}
        if ev_type == "step_start":
            current_step = {"texts": [], "reason": ""}
            steps.append(current_step)
        elif ev_type == "text":
            if current_step is not None:
                text = part.get("text", "")
                if text:
                    current_step["texts"].append(text)
        elif ev_type == "step_finish":
            if current_step is not None:
                current_step["reason"] = part.get("reason", "")

for step in reversed(steps):
    if step.get("reason") != "stop":
        continue
    text = "".join(step.get("texts", []))
    if text.strip():
        sys.stdout.write(text)
        sys.exit(0)

sys.stderr.write("No final text block found in forked session output\\n")
sys.exit(1)
PY
"""
