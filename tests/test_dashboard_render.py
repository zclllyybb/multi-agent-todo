"""Render tests for dashboard JavaScript without external frontend tooling."""

import re
import json
import subprocess
import textwrap

from web.app import DASHBOARD_HTML


def _extract_dashboard_script() -> str:
    match = re.search(r"<script>(.*)</script>", DASHBOARD_HTML, re.DOTALL)
    assert match, "dashboard script block not found"
    script = match.group(1)
    tail = textwrap.dedent(
        """
        initOverlayClose();
        refresh();
        setInterval(refresh, 5000);
        setInterval(() => {
          if (_isExploreTabActive()) loadExploreQueue();
        }, 3000);
        """
    ).strip()
    assert tail in script, "dashboard boot tail not found"
    return script.replace(tail, "").strip()


def _run_dashboard_js(test_body: str):
    script = _extract_dashboard_script()
    js = f"""
import assert from 'node:assert/strict';
import vm from 'node:vm';

const dashboardSource = {json.dumps(script)};

function escapeHtml(value) {{
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\"/g, '&quot;');
}}

const elements = new Map();
const localStorageState = new Map();

function makeElement(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      innerHTML: '',
      textContent: '',
      value: '',
      dataset: {{}},
      checked: false,
      disabled: false,
      style: {{}},
      classList: {{ add() {{}}, remove() {{}}, toggle() {{ return false; }} }},
      querySelector() {{ return null; }},
      querySelectorAll() {{ return []; }},
      addEventListener() {{}},
      removeEventListener() {{}},
      focus() {{}},
    }});
  }}
  return elements.get(id);
}}

globalThis.window = globalThis;
Object.defineProperty(globalThis, 'navigator', {{
  value: {{ clipboard: {{ writeText() {{ return Promise.resolve(); }} }} }},
  configurable: true,
}});
globalThis.localStorage = {{
  getItem(key) {{ return localStorageState.has(key) ? localStorageState.get(key) : null; }},
  setItem(key, value) {{ localStorageState.set(key, String(value)); }},
  removeItem(key) {{ localStorageState.delete(key); }},
}};
globalThis.performance = {{ now: () => 0 }};
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {{}};
globalThis.fetch = async () => ({{ json: async () => ({{}}) }});
globalThis.document = {{
  body: makeElement('body'),
  getElementById(id) {{
    return makeElement(id);
  }},
  querySelector() {{
    return null;
  }},
  querySelectorAll() {{
    return [];
  }},
  createElement() {{
    let raw = '';
    return {{
      style: {{}},
      classList: {{ add() {{}}, remove() {{}}, toggle() {{ return false; }} }},
      set textContent(value) {{ raw = String(value ?? ''); }},
      get textContent() {{ return raw; }},
      get innerHTML() {{ return escapeHtml(raw); }},
    }};
  }},
}};

vm.runInThisContext(dashboardSource, {{ filename: 'dashboard-inline.js' }});

{test_body}
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", js],
        check=True,
        text=True,
        capture_output=True,
    )


def test_render_session_box_success_path():
    _run_dashboard_js(
        r"""
const html = renderSessionBox('ses_123', 'performance session');
assert.match(html, /performance session/);
assert.match(html, /ses_123/);
assert.match(html, /opencode --session ses_123/);
assert.match(html, /\[copy\]/);
"""
    )


def test_theme_toggle_uses_local_storage_and_updates_body_dataset():
    _run_dashboard_js(
        r"""
localStorage.setItem('multi-agent-dashboard-theme', 'light');
initTheme();
assert.equal(document.body.dataset.theme, 'light');
assert.equal(document.getElementById('theme-toggle-label').textContent, 'Day');
toggleTheme();
assert.equal(document.body.dataset.theme, 'dark');
assert.equal(localStorage.getItem('multi-agent-dashboard-theme'), 'dark');
assert.equal(document.getElementById('theme-toggle-label').textContent, 'Night');
"""
    )


def test_dashboard_html_exposes_theme_toggle_and_light_theme_tokens():
    assert 'id="theme-toggle"' in DASHBOARD_HTML
    assert 'body[data-theme="light"]' in DASHBOARD_HTML
    assert (
        'THEME_STORAGE_KEY = "multi-agent-dashboard-theme"' in DASHBOARD_HTML
        or "THEME_STORAGE_KEY = 'multi-agent-dashboard-theme'" in DASHBOARD_HTML
    )


def test_task_detail_review_verdict_matches_backend_parser_for_approve_with_rejection_text():
    _run_dashboard_js(
        r"""
const detailPayload = {
  task: {
    id: 'task-review-parse',
    title: 'Review parsing task',
    status: 'completed',
    task_mode: 'develop',
    complexity: '',
    priority: 'medium',
    source: 'manual',
    parent_id: '',
    retry_count: 0,
    max_retries: 0,
    depends_on: [],
    branch_name: '',
    worktree_path: '',
    file_path: '',
    line_number: 0,
    created_at: 1710002000,
    started_at: 1710002010,
    completed_at: 1710002600,
    published_at: 0,
    clean_available: false,
    can_publish: false,
    can_assign_jira: true,
    can_cancel: false,
    can_resume: false,
    can_revise: false,
    can_arbitrate: false,
    description: 'desc',
    review_input: '',
    error: '',
    session_ids: {},
    plan_output: '',
    code_output: '',
    review_output: '',
    reviewer_results: [],
    comment_count: 0,
    has_comments: false,
    comments: [],
    jira_issue_key: '',
    jira_issue_url: '',
    jira_status: '',
    jira_error: '',
    jira_payload_preview: '',
    jira_agent_output: '',
  },
  runs: [{
    agent_type: 'reviewer',
    model: 'reviewer-model',
    prompt: 'review prompt',
    parsed: { session_id: 'ses_review', summary: {}, steps: [] },
    session_id: 'ses_review',
    review_verdict: 'approve',
    exit_code: 0,
    duration_sec: 1.0,
  }],
  git_status: {},
};
globalThis.fetch = async (url) => {
  if (url === '/api/tasks/task-review-parse') return { json: async () => detailPayload };
  throw new Error('unexpected url ' + url);
};
await showDetail('task-review-parse');
const html = document.getElementById('detail-content').innerHTML;
assert.match(html, /APPROVE/);
assert.doesNotMatch(html, /REQUEST_CHANGES/);
"""
    )


def test_render_parsed_run_success_path():
    _run_dashboard_js(
        r"""
const html = renderParsedRun({
  session_id: 'ses_run',
  summary: { total_steps: 1, text_segments: 1, tool_calls: 1 },
  steps: [{
    step_num: 1,
    events: [
      { type: 'text', time: '12:00:00', content: 'Analyzing scanner path' },
      { type: 'tool', time: '12:00:01', tool: 'read', status: 'completed', input: 'file=scanner.cpp', output: 'ok' },
    ],
    finish_reason: 'stop',
  }],
});
assert.match(html, /Steps: <b>1<\/b>/);
assert.match(html, /Text: <b>1<\/b>/);
assert.match(html, /Tool calls: <b>1<\/b>/);
assert.match(html, /Analyzing scanner path/);
assert.match(html, /read/);
assert.match(html, /file=scanner.cpp/);
assert.match(html, /-&gt; stop|-> stop/);
"""
    )


def test_show_module_detail_renders_categories_and_runs_successfully():
    payload = {
        "module": {
            "id": "mod1",
            "name": "Exec",
            "path": "be/src/exec",
            "description": "Execution engine",
            "category_status": {
                "performance": "stale",
                "concurrency": "done",
            },
            "category_notes": {
                "performance": "[2026-03-31 10:00:00] focus: scanner loop | explored: scanner.cpp next_batch and scheduler dispatch paths | completion: partial (merge path still needs review) | summary: Checked key hot paths but not merges | note: Continue with merge and spill paths.",
                "concurrency": "short note",
            },
        },
        "runs": [
            {
                "id": "run1",
                "category": "performance",
                "personality": "perf_hunter",
                "model": "test-explorer",
                "prompt": "explore scanner and scheduler",
                "session_id": "ses_partial",
                "focus_point": "scanner and scheduler",
                "actionability_score": 6.5,
                "reliability_score": 8.0,
                "explored_scope": "scanner.cpp and scheduler.cpp core paths",
                "completion_status": "partial",
                "supplemental_note": "Continue with merge path.",
                "map_review_required": False,
                "map_review_reason": "",
                "findings": [],
                "summary": "Explored scanner and scheduler",
                "issue_count": 0,
                "exit_code": 0,
                "duration_sec": 12.3,
                "created_at": 1710000000,
                "parsed": {
                    "session_id": "ses_partial",
                    "summary": {"total_steps": 1, "text_segments": 1, "tool_calls": 0},
                    "steps": [
                        {
                            "step_num": 1,
                            "events": [
                                {
                                    "type": "text",
                                    "time": "12:00:00",
                                    "content": "Exploring scanner and scheduler",
                                }
                            ],
                            "finish_reason": "stop",
                        }
                    ],
                },
            },
            {
                "id": "run2",
                "category": "concurrency",
                "personality": "concurrency_auditor",
                "model": "test-explorer",
                "prompt": "explore shared state",
                "session_id": "ses_complete",
                "focus_point": "shared state",
                "actionability_score": 2.0,
                "reliability_score": 8.5,
                "explored_scope": "critical lock and queue handoff paths",
                "completion_status": "complete",
                "supplemental_note": "No material issues found.",
                "map_review_required": False,
                "map_review_reason": "",
                "findings": [
                    {
                        "severity": "major",
                        "title": "Race on queue stats",
                        "description": "A shared counter is updated without synchronization.",
                        "file_path": "be/src/exec/queue.cpp",
                        "line_number": 42,
                        "suggested_fix": "Guard the counter with the existing mutex.",
                    }
                ],
                "summary": "Checked critical queue state transitions",
                "issue_count": 1,
                "exit_code": 0,
                "duration_sec": 9.1,
                "created_at": 1710000010,
                "parsed": {
                    "session_id": "ses_complete",
                    "summary": {"total_steps": 1, "text_segments": 1, "tool_calls": 0},
                    "steps": [
                        {
                            "step_num": 1,
                            "events": [
                                {
                                    "type": "text",
                                    "time": "12:00:02",
                                    "content": "Checked queue state transitions",
                                }
                            ],
                            "finish_reason": "stop",
                        }
                    ],
                },
            },
        ],
    }
    _run_dashboard_js(
        rf"""
const payload = {json.dumps(payload)};
globalThis.fetch = async () => ({{ json: async () => payload }});
await showModuleDetail('mod1');
const html = document.getElementById('explore-detail').innerHTML;
assert.match(html, /Exec/);
assert.match(html, /Categories/);
assert.match(html, /Exploration Runs \(2\)/);
assert.match(html, /completion: partial/);
assert.match(html, /completion: complete/);
assert.match(html, /Input prompt/);
assert.match(html, /Session transcript/);
assert.match(html, /opencode --session ses_partial/);
assert.match(html, /Race on queue stats/);
assert.match(html, /Continue with merge path\./);
assert.match(html, /Show full/);
assert.doesNotMatch(html, /<details><summary/);
"""
    )


def test_show_module_detail_renders_module_without_runs_successfully():
    payload = {
        "module": {
            "id": "mod2",
            "name": "Frontend",
            "path": "fe/src",
            "description": "Frontend planner",
            "category_status": {
                "performance": "todo",
            },
            "category_notes": {
                "performance": "",
            },
        },
        "runs": [],
    }
    _run_dashboard_js(
        rf"""
const payload = {json.dumps(payload)};
globalThis.fetch = async () => ({{ json: async () => payload }});
await showModuleDetail('mod2');
const html = document.getElementById('explore-detail').innerHTML;
assert.match(html, /Frontend/);
assert.match(html, /fe\/src/);
assert.match(html, /Categories/);
assert.doesNotMatch(html, /Exploration Runs/);
assert.match(html, /performance/);
"""
    )


def test_refresh_renders_task_list_successfully():
    assert "task-check-all" in DASHBOARD_HTML
    status_payload = {
        "total_tasks": 3,
        "active_task_count": 1,
        "status_counts": {
            "pending": 1,
            "planning": 0,
            "coding": 0,
            "jira_assigning": 1,
            "reviewing": 0,
            "completed": 1,
            "needs_arbitration": 0,
            "failed": 0,
            "review_failed": 0,
        },
    }
    tasks_payload = [
        {
            "id": "task_parent",
            "title": "Implement dashboard metrics",
            "status": "pending",
            "priority": "high",
            "source": "manual",
            "session_ids": {"planner": ["ses_plan"]},
            "updated_at": 1710001000,
            "complexity": "complex",
            "published_at": 0,
            "branch_name": "agent/task-parent",
            "task_mode": "develop",
            "parent_id": "",
            "depends_on": ["task_child"],
            "clean_available": False,
            "actual_branch_exists": False,
            "actual_worktree_exists": False,
            "can_publish": False,
            "can_assign_jira": True,
            "can_cancel": True,
            "can_resume": False,
            "can_revise": False,
            "can_arbitrate": False,
            "dependency_satisfied": False,
            "created_at": 1710000900,
        },
        {
            "id": "task_child",
            "title": "Refine metrics labels",
            "status": "jira_assigning",
            "priority": "medium",
            "source": "manual",
            "session_ids": {"coder": ["ses_code", "ses_code_2"]},
            "updated_at": 1710001100,
            "complexity": "medium",
            "published_at": 0,
            "branch_name": "agent/task-child",
            "task_mode": "develop",
            "parent_id": "task_parent",
            "depends_on": [],
            "clean_available": True,
            "actual_branch_exists": True,
            "actual_worktree_exists": True,
            "can_publish": False,
            "can_assign_jira": True,
            "can_cancel": True,
            "can_resume": False,
            "can_revise": False,
            "can_arbitrate": False,
            "dependency_satisfied": False,
            "created_at": 1710000950,
        },
        {
            "id": "task_ready",
            "title": "Queue follow-up polish",
            "status": "pending",
            "priority": "medium",
            "source": "manual",
            "session_ids": {},
            "updated_at": 1710001150,
            "complexity": "simple",
            "published_at": 0,
            "branch_name": "",
            "task_mode": "develop",
            "parent_id": "",
            "depends_on": [],
            "clean_available": False,
            "actual_branch_exists": False,
            "actual_worktree_exists": False,
            "can_publish": False,
            "can_assign_jira": True,
            "can_cancel": True,
            "can_resume": False,
            "can_revise": False,
            "can_arbitrate": False,
            "dependency_satisfied": False,
            "created_at": 1710000960,
        },
        {
            "id": "task_done",
            "title": "Ship initial stats cards",
            "status": "completed",
            "priority": "low",
            "source": "explore",
            "session_ids": {"reviewer": ["ses_review"]},
            "updated_at": 1710000800,
            "complexity": "simple",
            "published_at": 1710001200,
            "branch_name": "agent/task-done",
            "task_mode": "develop",
            "parent_id": "",
            "depends_on": [],
            "clean_available": True,
            "actual_branch_exists": True,
            "actual_worktree_exists": True,
            "can_publish": True,
            "can_assign_jira": True,
            "can_cancel": False,
            "can_resume": False,
            "can_revise": True,
            "can_arbitrate": False,
            "dependency_satisfied": True,
            "created_at": 1710000700,
            "comment_count": 2,
            "has_comments": True,
        },
    ]
    _run_dashboard_js(
        rf"""
const statusPayload = {json.dumps(status_payload)};
const tasksPayload = {json.dumps(tasks_payload)};
globalThis.fetch = async (url) => {{
  if (url === '/api/status') return {{ json: async () => statusPayload }};
  if (url === '/api/tasks') return {{ json: async () => tasksPayload }};
  throw new Error('unexpected url ' + url);
}};
    await refresh();
    const statsHtml = document.getElementById('stats').innerHTML;
    const rowsHtml = document.getElementById('task-list').innerHTML;
    assert.match(statsHtml, /Total/);
    assert.match(statsHtml, />3</);
    assert.match(statsHtml, /Active/);
    assert.match(statsHtml, /style="color:var\(--accent\)">1</);
    assert.match(rowsHtml, /Implement dashboard metrics/);
assert.match(rowsHtml, /Refine metrics labels/);
assert.match(rowsHtml, /Queue follow-up polish/);
assert.match(rowsHtml, /Ship initial stats cards/);
assert.match(rowsHtml, /class="task-check"/);
assert.match(rowsHtml, /blocked/);
assert.match(rowsHtml, /2 comments/);
assert.match(rowsHtml, /2 sessions/);
assert.match(rowsHtml, /1 session/);
assert.match(rowsHtml, /complex/);
assert.match(rowsHtml, /medium/);
assert.match(rowsHtml, /Run/);
assert.match(rowsHtml, /Cancel/);
assert.match(rowsHtml, /Clean/);
assert.match(rowsHtml, /Publish|Re-push/);
"""
    )


def test_show_detail_renders_task_overview_sessions_runs_and_outputs_successfully():
    detail_payload = {
        "task": {
            "id": "task123",
            "title": "Stabilize review flow",
            "status": "completed",
            "task_mode": "develop",
            "complexity": "complex",
            "priority": "high",
            "source": "manual",
            "parent_id": "",
            "retry_count": 1,
            "max_retries": 3,
            "depends_on": ["dep1"],
            "branch_name": "agent/task123",
            "worktree_path": "/tmp/worktree/task123",
            "file_path": "core/orchestrator.py",
            "line_number": 2714,
            "created_at": 1710002000,
            "started_at": 1710002010,
            "completed_at": 1710002600,
            "published_at": 1710002700,
            "clean_available": True,
            "can_publish": True,
            "can_assign_jira": True,
            "can_cancel": False,
            "can_resume": False,
            "can_revise": True,
            "can_arbitrate": False,
            "description": "Tighten the review loop and improve session visibility.",
            "review_input": "Please review concurrency changes.",
            "error": "",
            "session_ids": {
                "planner": ["ses_plan"],
                "coder": ["ses_code"],
                "reviewer": ["ses_review"],
            },
            "plan_output": "Plan: inspect orchestration and update UI.",
            "code_output": "Implemented retry and session rendering changes.",
            "review_output": "APPROVE",
            "reviewer_results": [
                {
                    "passed": True,
                    "model": "reviewer-a",
                    "output": "APPROVE: changes look correct.",
                }
            ],
            "comment_count": 2,
            "has_comments": True,
            "comments": [
                {
                    "id": "c1",
                    "username": "alice",
                    "content": "Please check the retry copy.",
                    "created_at": 1710002100,
                },
                {
                    "id": "c2",
                    "username": "bob",
                    "content": "Also verify reviewer session links.",
                    "created_at": 1710002200,
                },
            ],
        },
        "runs": [
            {
                "agent_type": "planner",
                "model": "planner-model",
                "prompt": "plan the task",
                "parsed": {
                    "session_id": "ses_plan",
                    "summary": {"total_steps": 1, "text_segments": 1, "tool_calls": 0},
                    "steps": [
                        {
                            "step_num": 1,
                            "events": [
                                {
                                    "type": "text",
                                    "time": "12:00:00",
                                    "content": "Plan task scope",
                                }
                            ],
                            "finish_reason": "stop",
                        }
                    ],
                },
                "session_id": "ses_plan",
                "exit_code": 0,
                "duration_sec": 1.2,
            },
            {
                "agent_type": "reviewer",
                "model": "reviewer-model",
                "prompt": "review the changes",
                "parsed": {
                    "session_id": "ses_review",
                    "summary": {"total_steps": 1, "text_segments": 1, "tool_calls": 0},
                    "steps": [
                        {
                            "step_num": 1,
                            "events": [
                                {
                                    "type": "text",
                                    "time": "12:01:00",
                                    "content": "Looks good overall",
                                }
                            ],
                            "finish_reason": "stop",
                        }
                    ],
                },
                "session_id": "ses_review",
                "review_verdict": "approve",
                "exit_code": 0,
                "duration_sec": 2.5,
            },
            {
                "agent_type": "manual_review",
                "model": "human",
                "prompt": "",
                "output": "Manual review note",
                "parsed": {"session_id": "", "steps": [], "summary": {}},
                "session_id": "",
                "exit_code": 0,
                "duration_sec": 0.3,
            },
        ],
        "git_status": {
            "branch": "agent/task123",
            "ahead": 1,
            "staged": ["core/orchestrator.py"],
            "unstaged": ["web/app.py"],
            "untracked": ["tests/test_dashboard_render.py"],
            "raw": "## agent/task123...origin/agent/task123 [ahead 1]",
        },
    }
    dep_task = {"dep1": {"id": "dep1", "status": "completed"}}
    _run_dashboard_js(
        rf"""
const detailPayload = {json.dumps(detail_payload)};
window._taskById = {json.dumps(dep_task)};
globalThis.fetch = async (url) => {{
  if (url === '/api/tasks/task123') return {{ json: async () => detailPayload }};
  throw new Error('unexpected url ' + url);
}};
await showDetail('task123');
const detailTitle = document.getElementById('detail-title').textContent;
const html = document.getElementById('detail-content').innerHTML;
assert.equal(detailTitle, 'Stabilize review flow');
assert.match(html, /Overview/);
assert.match(html, /Sessions/);
assert.match(html, /Agent Runs \(3\)/);
assert.match(html, /Git Status/);
assert.match(html, /Outputs/);
assert.match(html, /Description/);
assert.match(html, /Comments/);
assert.match(html, /Please check the retry copy/);
assert.match(html, /Also verify reviewer session links/);
assert.match(html, /Add Comment/);
assert.match(html, /Review Input/);
assert.match(html, /Depends On/);
assert.match(html, /dep1/);
assert.match(html, /Published/);
assert.match(html, /Publish branch to remote|Re-push to remote/);
assert.match(html, /Clean up worktree/);
assert.match(html, /opencode --session ses_plan/);
assert.match(html, /opencode --session ses_review/);
assert.match(html, /Per-Run Sessions/);
assert.match(html, /planner-model/);
assert.match(html, /reviewer-model/);
assert.match(html, /APPROVE/);
assert.match(html, /Manual review note/);
assert.match(html, /Plan task scope/);
assert.match(html, /Run Command/);
assert.match(html, /core\/orchestrator.py/);
assert.match(html, /web\/app.py/);
assert.match(html, /tests\/test_dashboard_render.py/);
assert.match(html, /Plan Output/);
assert.match(html, /Code Output/);
assert.match(html, /Review Results \(1 reviewer\)/);
assert.match(html, /changes look correct/);
"""
    )


def test_show_detail_preserves_unsent_text_inputs_across_close_and_reopen():
    detail_payload = {
        "task": {
            "id": "task_drafts",
            "title": "Draft persistence task",
            "status": "review_failed",
            "task_mode": "develop",
            "complexity": "",
            "priority": "medium",
            "source": "manual",
            "parent_id": "",
            "retry_count": 0,
            "max_retries": 3,
            "depends_on": [],
            "branch_name": "agent/task_drafts",
            "worktree_path": "/tmp/worktree/task_drafts",
            "file_path": "",
            "line_number": 0,
            "created_at": 1710002000,
            "started_at": 1710002010,
            "completed_at": 0,
            "published_at": 0,
            "clean_available": False,
            "can_publish": False,
            "can_assign_jira": False,
            "can_cancel": False,
            "can_resume": True,
            "can_revise": True,
            "can_arbitrate": True,
            "description": "Keep unsent text when closing the detail panel.",
            "review_input": "",
            "error": "",
            "session_ids": {},
            "plan_output": "plan",
            "code_output": "code",
            "review_output": "review",
            "reviewer_results": [],
            "comment_count": 0,
            "has_comments": False,
            "comments": [],
            "jira_issue_key": "",
            "jira_issue_url": "",
            "jira_status": "",
            "jira_error": "",
            "jira_payload_preview": "",
            "jira_agent_output": "",
        },
        "runs": [],
        "git_status": {
            "branch": "agent/task_drafts",
            "ahead": 0,
            "staged": [],
            "unstaged": [],
            "untracked": [],
            "raw": "## agent/task_drafts",
        },
    }
    _run_dashboard_js(
        rf"""
const detailPayload = {json.dumps(detail_payload)};
globalThis.fetch = async (url) => {{
  if (url === '/api/tasks/task_drafts') return {{ json: async () => detailPayload }};
  throw new Error('unexpected url ' + url);
}};
await showDetail('task_drafts');
let html = document.getElementById('detail-content').innerHTML;
assert.match(html, /comment-username-task_drafts/);
assert.match(html, /comment-content-task_drafts/);
assert.match(html, /revise-feedback-task_drafts/);
assert.match(html, /resume-message-task_drafts/);
assert.match(html, /arbitrate-feedback-task_drafts/);
assert.match(html, /exec-cmd-input/);
document.getElementById('comment-username-task_drafts').value = 'alice';
document.getElementById('comment-content-task_drafts').value = 'unsent comment';
document.getElementById('revise-feedback-task_drafts').value = 'keep revise feedback';
document.getElementById('resume-message-task_drafts').value = 'resume with custom instruction';
document.getElementById('arbitrate-feedback-task_drafts').value = 'keep arbitration note';
document.getElementById('exec-cmd-input').value = 'git status --short';
closeModals();
await showDetail('task_drafts');
assert.equal(document.getElementById('comment-username-task_drafts').value, 'alice');
assert.equal(document.getElementById('comment-content-task_drafts').value, 'unsent comment');
assert.equal(document.getElementById('revise-feedback-task_drafts').value, 'keep revise feedback');
assert.equal(document.getElementById('resume-message-task_drafts').value, 'resume with custom instruction');
assert.equal(document.getElementById('arbitrate-feedback-task_drafts').value, 'keep arbitration note');
assert.equal(document.getElementById('exec-cmd-input').value, 'git status --short');
"""
    )


def test_refresh_renders_jira_mode_badge_successfully():
    status_payload = {
        "total_tasks": 1,
        "active_task_count": 0,
        "status_counts": {"completed": 1},
    }
    tasks_payload = [
        {
            "id": "jira1",
            "title": "Create Jira for flaky test",
            "status": "completed",
            "priority": "medium",
            "source": "manual",
            "session_ids": {"coder": ["ses_jira"]},
            "updated_at": 1710001300,
            "complexity": "",
            "published_at": 0,
            "branch_name": "",
            "task_mode": "jira",
            "parent_id": "",
            "depends_on": [],
            "clean_available": False,
            "actual_branch_exists": False,
            "actual_worktree_exists": False,
            "created_at": 1710001200,
            "jira_issue_key": "QA-123",
            "jira_issue_url": "https://jira.example/browse/QA-123",
        }
    ]
    _run_dashboard_js(
        rf"""
const statusPayload = {json.dumps(status_payload)};
const tasksPayload = {json.dumps(tasks_payload)};
globalThis.fetch = async (url) => {{
  if (url === '/api/status') return {{ json: async () => statusPayload }};
  if (url === '/api/tasks') return {{ json: async () => tasksPayload }};
  throw new Error('unexpected url ' + url);
}};
        await refresh();
        const rowsHtml = document.getElementById('task-list').innerHTML;
        assert.match(rowsHtml, /Create Jira for flaky test/);
        assert.match(rowsHtml, /QA-123/);
        assert.match(rowsHtml, /href="https:\/\/jira\.example\/browse\/QA-123"/);
        assert.match(rowsHtml, /target="_blank"/);
        assert.match(rowsHtml, /event\.stopPropagation\(\)/);
        assert.doesNotMatch(rowsHtml, />jira</);
        assert.doesNotMatch(rowsHtml, /Publish/);
        """
    )


def test_show_detail_renders_jira_result_successfully():
    detail_payload = {
        "task": {
            "id": "jira123",
            "title": "File Jira issue",
            "status": "completed",
            "task_mode": "jira",
            "complexity": "",
            "priority": "medium",
            "source": "manual",
            "parent_id": "",
            "retry_count": 0,
            "max_retries": 0,
            "depends_on": [],
            "branch_name": "",
            "worktree_path": "",
            "file_path": "",
            "line_number": 0,
            "created_at": 1710002000,
            "started_at": 1710002010,
            "completed_at": 1710002600,
            "published_at": 0,
            "clean_available": False,
            "can_publish": False,
            "can_assign_jira": False,
            "can_cancel": False,
            "can_resume": False,
            "can_revise": False,
            "can_arbitrate": False,
            "description": "Draft a Jira issue for the flaky test.",
            "review_input": "",
            "error": "",
            "session_ids": {"coder": ["ses_jira"]},
            "plan_output": "Jira target: QA / Bug",
            "code_output": "",
            "review_output": "",
            "reviewer_results": [],
            "comment_count": 0,
            "has_comments": False,
            "comments": [],
            "jira_issue_key": "QA-123",
            "jira_issue_url": "https://jira.example/browse/QA-123",
            "jira_status": "created",
            "jira_payload_preview": "",
            "jira_agent_output": "key=QA-123\nself=https://jira.example/rest/api/2/issue/123",
        },
        "runs": [],
        "git_status": {},
    }
    _run_dashboard_js(
        rf"""
const detailPayload = {json.dumps(detail_payload)};
globalThis.fetch = async (url) => {{
  if (url === '/api/tasks/jira123') return {{ json: async () => detailPayload }};
  throw new Error('unexpected url ' + url);
}};
await showDetail('jira123');
const detailTitle = document.getElementById('detail-title').textContent;
const html = document.getElementById('detail-content').innerHTML;
assert.equal(detailTitle, 'File Jira issue (QA-123)');
assert.match(html, /Jira Result/);
assert.match(html, /QA-123/);
assert.match(html, /Jira status: <code>created<\/code>/);
assert.match(html, /Jira Agent Output/);
assert.match(html, /self=https:\/\/jira\.example\/rest\/api\/2\/issue\/123/);
assert.match(html, /ses_jira/);
"""
    )


def test_load_sys_info_renders_explorer_and_map_model_selects_successfully():
    cfg_payload = {
        "repo_path": "/repo",
        "base_branch": "main",
        "worktree_dir": "/wt",
        "worktree_hooks": ["hooks/setup.sh"],
        "opencode_config_path": "/workspace/opencode.json",
        "planner_model": "planner-x",
        "explorer_model": "explorer-x",
        "map_model": "map-x",
        "coder_model_by_complexity": {
            "simple": "coder-s",
            "complex": "coder-c",
        },
        "coder_model_default": "coder-default",
        "reviewer_models": ["reviewer-a", "reviewer-b"],
        "max_retries": 4,
        "publish_remote": "origin",
    }
    models_payload = {
        "models": [
            "planner-x",
            "explorer-x",
            "map-x",
            "coder-default",
            "coder-s",
            "coder-c",
            "reviewer-a",
            "reviewer-b",
        ]
    }
    _run_dashboard_js(
        rf"""
const cfgPayload = {json.dumps(cfg_payload)};
const modelsPayload = {json.dumps(models_payload)};
globalThis.fetch = async (url) => {{
  if (url === '/api/config') return {{ json: async () => cfgPayload }};
  if (url === '/api/models') return {{ json: async () => modelsPayload }};
  throw new Error('unexpected url ' + url);
}};
await loadSysInfo();
const html = document.getElementById('sysinfo-content').innerHTML;
        assert.match(html, /Planner Model/);
        assert.match(html, /OpenCode Config/);
        assert.match(html, /workspace\/opencode\.json/);
        assert.match(html, /Explorer Model/);
        assert.match(html, /Map Model/);
assert.match(html, /sys-explorer-model/);
assert.match(html, /sys-map-model/);
assert.match(html, /reviewer-a/);
assert.match(html, /coder-default/);
"""
    )


def test_save_sys_models_posts_explorer_and_map_models_in_payload():
    _run_dashboard_js(
        r"""
let captured = null;
globalThis.showSysToast = () => {};
globalThis.loadSysInfo = () => {};
document.getElementById('sys-save-btn').textContent = 'Save';
document.getElementById('sys-planner-model').value = 'planner-save';
document.getElementById('sys-explorer-model').value = 'explorer-save';
document.getElementById('sys-map-model').value = 'map-save';
document.getElementById('sys-coder-default').value = 'coder-save';
document.querySelectorAll = (selector) => {
  if (selector === '[data-complexity]') {
    return [
      { dataset: { complexity: 'simple' }, value: 'coder-simple' },
      { dataset: { complexity: 'complex' }, value: 'coder-complex' },
    ];
  }
  if (selector === '.sys-reviewer-select') {
    return [
      { value: 'reviewer-a' },
      { value: 'reviewer-b' },
    ];
  }
  return [];
};
globalThis.fetch = async (url, opts) => {
  captured = { url, opts };
  return { json: async () => ({ ok: true }) };
};
await saveSysModels();
assert.equal(captured.url, '/api/config');
const body = JSON.parse(captured.opts.body);
assert.equal(body.planner_model, 'planner-save');
assert.equal(body.explorer_model, 'explorer-save');
assert.equal(body.map_model, 'map-save');
assert.equal(body.coder_model_default, 'coder-save');
assert.deepEqual(body.coder_model_by_complexity, { simple: 'coder-simple', complex: 'coder-complex' });
assert.deepEqual(body.reviewer_models, ['reviewer-a', 'reviewer-b']);
"""
    )


def test_add_jira_task_posts_expected_payload():
    _run_dashboard_js(
        r"""
let captured = null;
globalThis.refresh = () => {};
document.getElementById('jira-task-title').value = 'Open Jira for test failure';
document.getElementById('jira-task-desc').value = 'Include stack trace and owner hints';
document.getElementById('jira-task-priority').value = 'high';
globalThis.fetch = async (url, opts) => {
  captured = { url, opts };
  return { json: async () => ({ ok: true }) };
};
await addJiraTask();
assert.equal(captured.url, '/api/tasks/jira');
const body = JSON.parse(captured.opts.body);
assert.equal(body.title, 'Open Jira for test failure');
assert.equal(body.description, 'Include stack trace and owner hints');
assert.equal(body.priority, 'high');
"""
    )


def test_refresh_renders_assign_jira_button_for_existing_task():
    _run_dashboard_js(
        r"""
globalThis.fetch = async () => ({ json: async () => ([{
  id: 'task123',
  title: 'Existing task',
  jira_issue_key: '',
  status: 'completed',
  priority: 'medium',
  source: 'manual',
  session_ids: {},
  comment_count: 0,
  has_comments: false,
  updated_at: 1710000000,
  complexity: '',
  published_at: 0,
  branch_name: '',
  task_mode: 'develop',
  parent_id: '',
  depends_on: [],
  clean_available: false,
  actual_branch_exists: false,
  actual_worktree_exists: false,
  can_publish: false,
  can_assign_jira: true,
  can_cancel: false,
  can_resume: false,
  can_revise: false,
  can_arbitrate: false,
  dependency_satisfied: true,
}]) });
await refresh();
const rowsHtml = document.getElementById('task-list').innerHTML;
assert.match(rowsHtml, /Assign Jira/);
assert.match(rowsHtml, /\/api\/tasks\/task123\/jira|assignJiraForTask\('task123'\)/);
"""
    )


def test_show_detail_renders_assign_jira_button_for_existing_task():
    detail_payload = {
        "task": {
            "id": "task123",
            "title": "Existing task",
            "status": "completed",
            "task_mode": "develop",
            "complexity": "",
            "priority": "medium",
            "source": "manual",
            "parent_id": "",
            "retry_count": 0,
            "max_retries": 4,
            "depends_on": [],
            "branch_name": "",
            "worktree_path": "",
            "file_path": "",
            "line_number": 0,
            "created_at": 1710002000,
            "started_at": 1710002010,
            "completed_at": 1710002600,
            "published_at": 0,
            "clean_available": False,
            "can_publish": False,
            "can_assign_jira": True,
            "can_cancel": False,
            "can_resume": False,
            "can_revise": False,
            "can_arbitrate": False,
            "description": "Task description.",
            "review_input": "",
            "error": "",
            "session_ids": {},
            "plan_output": "plan",
            "code_output": "code",
            "review_output": "review",
            "reviewer_results": [],
            "comment_count": 0,
            "has_comments": False,
            "comments": [],
            "jira_issue_key": "",
            "jira_issue_url": "",
            "jira_status": "",
            "jira_error": "",
            "jira_payload_preview": "",
            "jira_agent_output": "",
        },
        "runs": [],
        "git_status": {},
    }
    _run_dashboard_js(
        rf"""
const detailPayload = {json.dumps(detail_payload)};
globalThis.fetch = async (url) => {{
  if (url === '/api/tasks/task123') return {{ json: async () => detailPayload }};
  throw new Error('unexpected url ' + url);
}};
await showDetail('task123');
const html = document.getElementById('detail-content').innerHTML;
assert.match(html, /Assign Jira for This Task/);
assert.match(html, /assignJiraForTask\('task123'\)/);
assert.match(html, /Runs Jira assignment directly on this task and syncs the created Jira key here/);
"""
    )


def test_assign_jira_for_task_posts_expected_endpoint():
    _run_dashboard_js(
        r"""
let captured = null;
globalThis.refresh = async () => {};
globalThis.showDetail = async () => {};
globalThis.uiAlert = async () => {};
document.getElementById('detail-modal').classList.contains = () => false;
globalThis.fetch = async (url, opts) => {
  captured = { url, opts };
  return { json: async () => ({ ok: true, task: { id: 'task123' } }) };
};
globalThis.event = { target: { disabled: false, textContent: 'Assign Jira' } };
await assignJiraForTask('task123');
assert.equal(captured.url, '/api/tasks/task123/jira');
assert.equal(captured.opts.method, 'POST');
"""
    )


def test_toggle_category_note_expands_and_collapses():
    _run_dashboard_js(
        r"""
globalThis.fetch = async () => ({ json: async () => ({
  module: {
    id: 'mod1',
    name: 'Exec',
    path: 'be/src/exec',
    description: 'module',
    category_status: { performance: 'done' },
    category_notes: { performance: 'preview text full text and more so it expands beyond the preview threshold for testing category note toggling behavior' },
  },
  runs: [],
}) });
await showModuleDetail('mod1');
const noteEl = document.getElementById('cat-note-mod1-performance');
noteEl.dataset.expanded = 'false';
const trigger = { textContent: 'Show full' };
toggleCategoryNote('cat-note-mod1-performance', trigger);
assert.equal(noteEl.dataset.expanded, 'true');
assert.match(noteEl.innerHTML, /preview text full text and more/);
assert.equal(trigger.textContent, 'Show less');
toggleCategoryNote('cat-note-mod1-performance', trigger);
assert.equal(noteEl.dataset.expanded, 'false');
assert.equal(trigger.textContent, 'Show full');
"""
    )


def test_toggle_category_note_preserves_accumulated_note_after_second_explore():
    payload = {
        "module": {
            "id": "mod1",
            "name": "Execution Environment and Fragment Management",
            "path": "be/src/runtime",
            "description": "module",
            "category_status": {"performance": "partial"},
            "category_notes": {
                "performance": "[2026-04-01 09:19:45] | completion: complete | summary: First explore summary\n\n[2026-04-02 10:11:12] | completion: partial | summary: Second explore summary | note: Continue on spill path"
            },
        },
        "runs": [],
    }
    _run_dashboard_js(
        rf"""
const payload = {json.dumps(payload)};
globalThis.fetch = async () => ({{ json: async () => payload }});
await showModuleDetail('mod1');
const noteEl = document.getElementById('cat-note-mod1-performance');
const trigger = {{ textContent: 'Show full' }};
toggleCategoryNote('cat-note-mod1-performance', trigger);
assert.equal(noteEl.dataset.expanded, 'true');
assert.match(noteEl.innerHTML, /First explore summary/);
assert.match(noteEl.innerHTML, /Second explore summary/);
assert.match(noteEl.innerHTML, /Continue on spill path/);
"""
    )


def test_show_module_detail_handles_persisted_note_with_quotes_and_newlines():
    payload = {
        "module": {
            "id": "mod-legacy",
            "name": "Execution Environment and Fragment Management",
            "path": "be/src/runtime",
            "description": "Legacy persisted module note case",
            "category_status": {"performance": "done"},
            "category_notes": {
                "performance": '[2026-04-01 09:19:45] | completion: complete | summary: Explored the control-plane side\nIncludes "quoted" detail and <html>-like text'
            },
        },
        "runs": [],
    }
    _run_dashboard_js(
        rf"""
const payload = {json.dumps(payload)};
globalThis.fetch = async () => ({{ json: async () => payload }});
await showModuleDetail('mod-legacy');
const html = document.getElementById('explore-detail').innerHTML;
assert.match(html, /Execution Environment and Fragment Management/);
assert.match(html, /completion: complete/);
assert.match(html, /Explored the control-plane side/);
assert.match(html, /quoted/);
assert.match(html, /Show full/);
"""
    )


def test_init_and_start_exploration_require_confirmation():
    _run_dashboard_js(
        r"""
const calls = [];
globalThis.uiAlert = async () => {};
globalThis.loadExploreModules = async () => {};
globalThis.showModuleDetail = async () => {};
globalThis.getSelectedExploreModules = () => ['mod1'];
globalThis.getSelectedExploreCategories = () => ['performance'];
globalThis.getExploreFocusPoint = () => '';
globalThis._exploreStatus = { map_ready: true, map_init: { status: 'done' } };
document.getElementById('explore-start-btn').innerHTML = 'start';
globalThis.uiConfirm = async () => false;
globalThis.fetch = async (url, opts) => {
  calls.push(url);
  return { json: async () => ({ accepted: true, started: 1, running: 1, queue: { counts: { queued: 0 } } }) };
};
await initExploreMap();
await startExploration();
assert.deepEqual(calls, []);
"""
    )


def test_start_exploration_confirms_replay_for_done_categories():
    explore_modules = [
        {
            "id": "mod1",
            "name": "Exec",
            "path": "be/src/exec",
            "children": [],
            "category_status": {"performance": "done", "concurrency": "todo"},
            "category_notes": {"performance": "prior summary", "concurrency": ""},
        }
    ]
    _run_dashboard_js(
        rf"""
const calls = [];
_exploreModules = {json.dumps(explore_modules)};
_exploreStatus = {{ map_ready: true, map_init: {{ status: 'done' }} }};
globalThis.getSelectedExploreModules = () => ['mod1'];
globalThis.getSelectedExploreCategories = () => ['performance'];
globalThis.getExploreFocusPoint = () => '';
document.getElementById('explore-start-btn').innerHTML = 'start';
globalThis.uiAlert = async () => {{}};
globalThis.uiConfirm = async (message, title) => {{
  calls.push({{ message, title }});
  return false;
}};
globalThis.fetch = async () => {{
  throw new Error('fetch should not run when replay confirmation is rejected');
}};
const doneSelections = getDoneExploreSelections(['mod1'], ['performance']);
assert.equal(doneSelections.length, 1);
await startExploration();
assert.equal(calls.length, 1);
assert.equal(calls[0].title, 'Re-explore Done Categories');
assert.match(calls[0].message, /already marked done/);
assert.match(calls[0].message, /Exec: performance/);
"""
    )


def test_start_exploration_confirmation_includes_focus_point_from_request_payload():
    _run_dashboard_js(
        r"""
let confirmCalls = [];
let fetchCalled = false;
_exploreModules = [
  {
    id: 'mod1',
    name: 'Exec',
    path: 'be/src/exec',
    children: [],
    category_status: { performance: 'todo' },
    category_notes: { performance: '' },
  }
];
_exploreStatus = { map_ready: true, map_init: { status: 'done' } };
globalThis.getSelectedExploreModules = () => ['mod1'];
globalThis.getSelectedExploreCategories = () => ['performance'];
globalThis.getExploreFocusPoint = () => 'scanner hot loop';
globalThis.uiAlert = async () => {};
globalThis.loadExploreModules = async () => {};
globalThis.showModuleDetail = async () => {};
document.getElementById('explore-start-btn').innerHTML = 'start';
globalThis.uiConfirm = async (message, title) => {
  confirmCalls.push({ message, title });
  return false;
};
globalThis.fetch = async () => {
  fetchCalled = true;
  return { json: async () => ({}) };
};
await startExploration();
assert.equal(confirmCalls.length, 1);
assert.equal(confirmCalls[0].title, 'Start Exploration');
assert.match(confirmCalls[0].message, /POST \/api\/explore\/start/);
assert.match(confirmCalls[0].message, /"focus_point":"scanner hot loop"/);
assert.match(confirmCalls[0].message, /"module_ids":\["mod1"\]/);
assert.equal(fetchCalled, false);
"""
    )


def test_add_task_comment_posts_payload_and_refreshes_views():
    _run_dashboard_js(
        r"""
let captured = null;
const calls = [];
globalThis.uiAlert = async () => {};
globalThis.showDetail = async (id) => { calls.push('detail:' + id); };
globalThis.refresh = async () => { calls.push('refresh'); };
document.getElementById('comment-username-task1').value = 'alice';
document.getElementById('comment-content-task1').value = 'Please verify retry handling';
document.getElementById('comment-btn-task1').textContent = 'Add Comment';
globalThis.fetch = async (url, opts) => {
  captured = { url, opts };
  return { json: async () => ({ ok: true, task: { id: 'task1', comment_count: 1, has_comments: true }, comments: [{ username: 'alice', content: 'Please verify retry handling' }] }) };
};
await addTaskComment('task1');
assert.equal(captured.url, '/api/tasks/task1/comments');
const body = JSON.parse(captured.opts.body);
assert.equal(body.username, 'alice');
assert.equal(body.content, 'Please verify retry handling');
assert.deepEqual(calls, ['detail:task1', 'refresh']);
assert.equal(document.getElementById('comment-content-task1').value, '');
"""
    )


def test_delete_selected_tasks_alerts_when_nothing_selected():
    _run_dashboard_js(
        r"""
let alertCall = null;
globalThis.uiAlert = async (message, title) => { alertCall = { message, title }; };
document.querySelectorAll = () => [];
await deleteSelectedTasks();
assert.equal(alertCall.title, 'Nothing Selected');
assert.match(alertCall.message, /Select at least one task to delete/);
"""
    )


def test_delete_selected_tasks_confirmation_renders_real_request_object_and_can_cancel():
    _run_dashboard_js(
        r"""
let confirmCall = null;
let fetchCalled = false;
window._taskById = {
  'task-a': { id: 'task-a', parent_id: '' },
  'task-b': { id: 'task-b', parent_id: 'task-a' },
};
document.querySelectorAll = (selector) => {
  if (selector === '.task-check:checked') {
    return [{ dataset: { id: 'task-a' } }];
  }
  return [];
};
globalThis.uiConfirm = async (message, title) => {
  confirmCall = { message, title };
  return false;
};
globalThis.fetch = async () => {
  fetchCalled = true;
  return { json: async () => ({}) };
};
await deleteSelectedTasks();
assert.equal(confirmCall.title, 'Delete Tasks');
assert.match(confirmCall.message, /POST \/api\/tasks\/delete/);
assert.match(confirmCall.message, /\{"ids":\["task-a","task-b"\],"cascade_descendants":true\}/);
assert.equal(fetchCalled, false);
"""
    )


def test_dashboard_dialog_message_supports_long_request_wrapping():
    assert (
        ".dialog-msg { font-size: 13px; color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }"
        in DASHBOARD_HTML
    )


def test_delete_selected_tasks_posts_same_request_and_reports_partial_failures():
    _run_dashboard_js(
        r"""
const calls = [];
let alertCall = null;
let closed = false;
window._taskById = {
  'task-a': { id: 'task-a', parent_id: '' },
  'task-b': { id: 'task-b', parent_id: 'task-a' },
};
document.querySelectorAll = (selector) => {
  if (selector === '.task-check:checked') {
    return [{ dataset: { id: 'task-a' } }];
  }
  if (selector === '.task-check') {
    return [{ checked: true }, { checked: true }];
  }
  return [];
};
document.getElementById('detail-modal').classList.contains = () => true;
globalThis._currentDetailTaskId = 'task-a';
globalThis.uiConfirm = async () => true;
globalThis.uiAlert = async (message, title) => { alertCall = { message, title }; };
globalThis.refresh = async () => { calls.push('refresh'); };
globalThis.closeModals = () => { closed = true; };
globalThis.fetch = async (url, opts) => {
  calls.push({ url, opts });
  return {
    json: async () => ({
      deleted: 1,
      deleted_ids: ['task-a'],
      errors: { 'task-a': 'Descendant task task-b: Task is referenced by dependent task task-c; delete it first' },
    }),
  };
};
await deleteSelectedTasks();
assert.equal(calls[0].url, '/api/tasks/delete');
assert.equal(calls[0].opts.method, 'POST');
assert.equal(calls[0].opts.body, '{"ids":["task-a","task-b"],"cascade_descendants":true}');
assert.deepEqual(calls.slice(1), ['refresh']);
assert.equal(alertCall.title, 'Delete Tasks Completed with Errors');
assert.match(alertCall.message, /Deleted 1 task\(s\)\./);
assert.match(alertCall.message, /task-a: Descendant task task-b: Task is referenced by dependent task task-c; delete it first/);
"""
    )


def test_show_detail_uses_server_driven_capabilities_for_resume_revise_and_arbitrate():
    detail_payload = {
        "task": {
            "id": "task_caps",
            "title": "Capability-driven task",
            "status": "review_failed",
            "task_mode": "develop",
            "complexity": "",
            "priority": "medium",
            "source": "manual",
            "parent_id": "",
            "retry_count": 0,
            "max_retries": 4,
            "depends_on": [],
            "branch_name": "",
            "worktree_path": "/tmp/wt",
            "file_path": "",
            "line_number": 0,
            "created_at": 1710002000,
            "started_at": 1710002010,
            "completed_at": 0,
            "published_at": 0,
            "clean_available": False,
            "can_publish": False,
            "can_assign_jira": False,
            "can_cancel": False,
            "can_resume": False,
            "can_revise": True,
            "can_arbitrate": True,
            "description": "Task description.",
            "review_input": "",
            "error": "",
            "session_ids": {},
            "plan_output": "plan",
            "code_output": "code",
            "review_output": "review",
            "reviewer_results": [],
            "comment_count": 0,
            "has_comments": False,
            "comments": [],
            "jira_issue_key": "",
            "jira_issue_url": "",
            "jira_status": "",
            "jira_error": "",
            "jira_payload_preview": "",
            "jira_agent_output": "",
        },
        "runs": [],
        "git_status": {},
    }
    _run_dashboard_js(
        rf"""
const detailPayload = {json.dumps(detail_payload)};
globalThis.fetch = async (url) => {{
  if (url === '/api/tasks/task_caps') return {{ json: async () => detailPayload }};
  throw new Error('unexpected url ' + url);
}};
await showDetail('task_caps');
const html = document.getElementById('detail-content').innerHTML;
assert.doesNotMatch(html, /Resume Failed Run/);
assert.match(html, /Revise Task/);
assert.match(html, /Human Arbitration Required/);
"""
    )


def test_refresh_uses_server_driven_capabilities_for_list_actions():
    status_payload = {
        "total_tasks": 1,
        "active_task_count": 0,
        "status_counts": {"review_failed": 1},
    }
    tasks_payload = [
        {
            "id": "task_caps",
            "title": "Capability-driven list task",
            "status": "review_failed",
            "priority": "medium",
            "source": "manual",
            "session_ids": {},
            "comment_count": 0,
            "has_comments": False,
            "updated_at": 1710001100,
            "complexity": "",
            "published_at": 0,
            "branch_name": "agent/task_caps",
            "task_mode": "develop",
            "parent_id": "",
            "depends_on": [],
            "clean_available": False,
            "actual_branch_exists": True,
            "actual_worktree_exists": True,
            "can_publish": False,
            "can_assign_jira": False,
            "can_cancel": False,
            "can_resume": False,
            "can_revise": True,
            "can_arbitrate": False,
            "dependency_satisfied": False,
        }
    ]
    _run_dashboard_js(
        rf"""
const statusPayload = {json.dumps(status_payload)};
const tasksPayload = {json.dumps(tasks_payload)};
globalThis.fetch = async (url) => {{
  if (url === '/api/status') return {{ json: async () => statusPayload }};
  if (url === '/api/tasks') return {{ json: async () => tasksPayload }};
  throw new Error('unexpected url ' + url);
}};
await refresh();
const rowsHtml = document.getElementById('task-list').innerHTML;
assert.doesNotMatch(rowsHtml, /Assign Jira/);
assert.doesNotMatch(rowsHtml, /Cancel/);
assert.doesNotMatch(rowsHtml, /Publish/);
"""
    )
