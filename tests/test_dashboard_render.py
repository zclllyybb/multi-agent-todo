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
                "completion_reason": "merge path remains unexplored",
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
                                {"type": "text", "time": "12:00:00", "content": "Exploring scanner and scheduler"}
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
                "completion_reason": "The main concurrency-sensitive flows are covered.",
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
                                {"type": "text", "time": "12:00:02", "content": "Checked queue state transitions"}
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
assert.match(html, /merge path remains unexplored/);
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
    status_payload = {
        "total_tasks": 3,
        "status_counts": {
            "pending": 1,
            "planning": 0,
            "coding": 1,
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
            "created_at": 1710000900,
        },
        {
            "id": "task_child",
            "title": "Refine metrics labels",
            "status": "coding",
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
            "created_at": 1710000700,
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
    assert.match(rowsHtml, /Implement dashboard metrics/);
    assert.match(rowsHtml, /Refine metrics labels/);
    assert.match(rowsHtml, /Queue follow-up polish/);
    assert.match(rowsHtml, /Ship initial stats cards/);
    assert.match(rowsHtml, /blocked/);
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
                                {"type": "text", "time": "12:00:00", "content": "Plan task scope"}
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
                                {"type": "text", "time": "12:01:00", "content": "Looks good overall"}
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


def test_load_sys_info_renders_explorer_and_map_model_selects_successfully():
    cfg_payload = {
        "repo_path": "/repo",
        "base_branch": "main",
        "worktree_dir": "/wt",
        "worktree_hooks": ["hooks/setup.sh"],
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


def test_toggle_category_note_expands_and_collapses():
    _run_dashboard_js(
        r"""
document.getElementById('cat-note').dataset.preview = 'preview text';
document.getElementById('cat-note').dataset.full = 'full text';
document.getElementById('cat-note').dataset.expanded = 'false';
document.getElementById('cat-note').innerHTML = 'preview text';
const trigger = { textContent: 'Show full' };
toggleCategoryNote('cat-note', trigger);
assert.equal(document.getElementById('cat-note').innerHTML, 'full text');
assert.equal(document.getElementById('cat-note').dataset.expanded, 'true');
assert.equal(trigger.textContent, 'Show less');
toggleCategoryNote('cat-note', trigger);
assert.equal(document.getElementById('cat-note').innerHTML, 'preview text');
assert.equal(document.getElementById('cat-note').dataset.expanded, 'false');
assert.equal(trigger.textContent, 'Show full');
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
