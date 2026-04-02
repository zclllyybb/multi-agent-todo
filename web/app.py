"""FastAPI web dashboard for observing and managing the multi-agent system."""

import logging
import os
import subprocess
import time
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.orchestrator import Orchestrator
from core.opencode_client import OpenCodeClient

app = FastAPI(title="Multi-Agent TODO Resolver")
log = logging.getLogger(__name__)

# Will be set by the daemon
orchestrator: Optional[Orchestrator] = None


def set_orchestrator(orch: Orchestrator):
    global orchestrator
    orchestrator = orch


@app.middleware("http")
async def api_timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    path = request.url.path
    if path.startswith("/api/"):
        size = response.headers.get("content-length", "-")
        if elapsed_ms >= 800:
            log.warning(
                "API slow: %s %s status=%d elapsed_ms=%.1f content_length=%s",
                request.method, path, response.status_code, elapsed_ms, size,
            )
        else:
            log.info(
                "API: %s %s status=%d elapsed_ms=%.1f content_length=%s",
                request.method, path, response.status_code, elapsed_ms, size,
            )
    return response


def _evaluate_review_verdict(review_text: str) -> str:
    """Determine APPROVE / REQUEST_CHANGES from reviewer output text.

    Mirrors ReviewerAgent._evaluate_review but returns a string verdict.
    """
    upper = review_text.upper()
    if "APPROVE" in upper and "REQUEST_CHANGES" not in upper:
        return "approve"
    if "REQUEST_CHANGES" in upper:
        return "request_changes"
    positive = ["LGTM", "LOOKS GOOD", "APPROVED", "NO ISSUES"]
    negative = ["BUG", "ERROR", "INCORRECT", "WRONG", "MISSING", "SHOULD BE"]
    pos = sum(1 for p in positive if p in upper)
    neg = sum(1 for n in negative if n in upper)
    return "approve" if pos > neg else "request_changes"


def _task_list_item(task_dict: dict) -> dict:
    """Return only fields needed by the task table refresh endpoint."""
    keys = (
        "id",
        "title",
        "status",
        "priority",
        "source",
        "session_ids",
        "comment_count",
        "has_comments",
        "updated_at",
        "complexity",
        "published_at",
        "branch_name",
        "task_mode",
        "parent_id",
        "depends_on",
        "clean_available",
        "actual_branch_exists",
        "actual_worktree_exists",
    )
    return {k: task_dict.get(k) for k in keys}


# ── API Routes ───────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    return orchestrator.get_status()


@app.get("/api/tasks")
async def api_tasks(status: Optional[str] = None):
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    t0 = time.perf_counter()
    tasks = orchestrator.db.get_all_tasks()
    t_db = time.perf_counter()

    if status:
        tasks = [t for t in tasks if t.status.value == status]
    t_filter = time.perf_counter()

    tasks.sort(key=lambda t: t.created_at, reverse=True)
    t_sort = time.perf_counter()

    ui_tasks = orchestrator.serialize_tasks_for_ui(tasks)
    t_serialize = time.perf_counter()

    compact_tasks = [_task_list_item(t) for t in ui_tasks]
    t_compact = time.perf_counter()

    total_ms = (t_compact - t0) * 1000.0
    log.info(
        "api_tasks metrics: count=%d total_ms=%.1f db_ms=%.1f filter_ms=%.1f sort_ms=%.1f "
        "serialize_ms=%.1f compact_ms=%.1f",
        len(compact_tasks),
        total_ms,
        (t_db - t0) * 1000.0,
        (t_filter - t_db) * 1000.0,
        (t_sort - t_filter) * 1000.0,
        (t_serialize - t_sort) * 1000.0,
        (t_compact - t_serialize) * 1000.0,
    )
    return compact_tasks


@app.get("/api/tasks/{task_id}")
async def api_task_detail(task_id: str):
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    task = orchestrator.db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    runs = orchestrator.db.get_runs_for_task(task_id)
    client = orchestrator.client
    parsed_runs = []
    for r in runs:
        rd = r.to_dict()
        rd["parsed"] = client.parse_readable_output(r.output)
        # For reviewer runs, determine the verdict from the output text
        if r.agent_type == "reviewer":
            rd["review_verdict"] = _evaluate_review_verdict(
                client.extract_text_response(r.output)
            )
        # Don't send raw output to frontend (too large) — except manual_review
        if r.agent_type != "manual_review":
            rd.pop("output", None)
        parsed_runs.append(rd)
    # Fetch live git status for the worktree (empty dict if no worktree yet)
    git_status = {}
    if task.worktree_path:
        git_status = orchestrator.worktree_mgr.get_git_status(task.worktree_path)

    return {
        "task": orchestrator.serialize_task_for_ui(task),
        "runs": parsed_runs,
        "git_status": git_status,
    }


@app.post("/api/tasks/{task_id}/comments")
async def api_add_task_comment(task_id: str, request: Request):
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    result = orchestrator.add_task_comment(
        task_id,
        body.get("username", ""),
        body.get("content", ""),
    )
    if "error" in result:
        status = 404 if result["error"] == "Task not found" else 400
        return JSONResponse(result, status_code=status)
    return result


@app.post("/api/tasks")
async def api_add_task(request: Request):
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    # Parse copy_files: newline-separated list of relative paths
    copy_raw = body.get("copy_files", "")
    copy_files = [f.strip() for f in copy_raw.split("\n") if f.strip()] if copy_raw else []
    task = orchestrator.submit_task(
        title=body.get("title", "Untitled"),
        description=body.get("description", ""),
        priority=body.get("priority", "medium"),
        copy_files=copy_files,
    )
    return task.to_dict()


@app.post("/api/tasks/review")
async def api_add_review_task(request: Request):
    """Submit a review-only task (no coding, just runs reviewers)."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    title = body.get("title", "").strip()
    review_input = body.get("review_input", "").strip()
    if not review_input:
        return JSONResponse({"error": "review_input required"}, status_code=400)
    copy_raw = body.get("copy_files", "")
    copy_files = [f.strip() for f in copy_raw.split("\n") if f.strip()] if copy_raw else []
    task = orchestrator.submit_review_task(
        title=title or "Review Task",
        review_input=review_input,
        priority=body.get("priority", "medium"),
        copy_files=copy_files,
    )
    return task.to_dict()


@app.post("/api/tasks/{task_id}/dispatch")
async def api_dispatch_task(task_id: str):
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    ok = orchestrator.dispatch_task(task_id)
    return {"dispatched": ok}


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str):
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    result = orchestrator.cancel_task(task_id)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/tasks/{task_id}/clean")
async def api_clean_task(task_id: str):
    """Remove worktree and branch of a finished task to free resources."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    result = orchestrator.clean_task(task_id)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/tasks/{task_id}/publish")
async def api_publish_task(task_id: str):
    """Push a completed task's branch to the configured remote."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    result = orchestrator.publish_task(task_id)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/tasks/{task_id}/revise")
async def api_revise_task(task_id: str, request: Request):
    """Re-open a completed/failed task with manual review feedback."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    feedback = body.get("feedback", "").strip()
    if not feedback:
        return JSONResponse({"error": "feedback required"}, status_code=400)
    result = orchestrator.revise_task(task_id, feedback)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/tasks/{task_id}/resume")
async def api_resume_task(task_id: str, request: Request):
    """Resume a failed task from the last coder session."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    message = body.get("message", "").strip() or "Continue"
    result = orchestrator.resume_task(task_id, message)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/tasks/{task_id}/arbitrate")
async def api_arbitrate_task(task_id: str, request: Request):
    """Resolve a NEEDS_ARBITRATION task: approve, revise, or reject."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    action = body.get("action", "").strip()
    feedback = body.get("feedback", "").strip()
    if not action:
        return JSONResponse({"error": "action required (approve/revise/reject)"}, status_code=400)
    result = orchestrator.resolve_arbitration(task_id, action, feedback)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/tasks/{task_id}/exec")
async def api_exec_in_worktree(task_id: str, request: Request):
    """Execute a shell command inside a task's worktree directory."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    task = orchestrator.db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    if not task.worktree_path or not os.path.isdir(task.worktree_path):
        return JSONResponse({"error": "Worktree not available"}, status_code=400)
    body = await request.json()
    cmd = body.get("command", "").strip()
    if not cmd:
        return JSONResponse({"error": "command required"}, status_code=400)
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=task.worktree_path,
            capture_output=True, text=True, timeout=30,
        )
        return {
            "stdout": result.stdout[-8000:] if len(result.stdout) > 8000 else result.stdout,
            "stderr": result.stderr[-4000:] if len(result.stderr) > 4000 else result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Command timed out (30s limit)"}, status_code=408)


@app.get("/api/todos")
async def api_get_todos():
    """List all scanned TODO items."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    items = orchestrator.db.get_all_todo_items()
    return [i.to_dict() for i in items]


@app.post("/api/todos/scan")
async def api_scan_todos(request: Request):
    """Scan repo for new TODO comments and store them as TodoItems."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    subdir = str(body.get("subdir", "")).strip()
    limit = int(body.get("limit", 0))
    new_items = orchestrator.scan_todos_raw(subdir=subdir, limit=limit)
    return {"scanned": len(new_items), "items": new_items}


@app.get("/api/config")
async def api_config():
    """Return a safe subset of the current config for the frontend info panel."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    cfg = orchestrator.config
    repo = cfg.get("repo", {})
    oc = cfg.get("opencode", {})
    orch = cfg.get("orchestrator", {})
    return {
        "repo_path": repo.get("path", ""),
        "base_branch": repo.get("base_branch", ""),
        "worktree_dir": repo.get("worktree_dir", ""),
        "worktree_hooks": repo.get("worktree_hooks", []),
        "planner_model": oc.get("planner_model", ""),
        "coder_model_by_complexity": oc.get("coder_model_by_complexity", {}),
        "coder_model_default": oc.get("coder_model_default", ""),
        "reviewer_models": oc.get("reviewer_models", []),
        "explorer_model": cfg.get("explore", {}).get("explorer_model", ""),
        "map_model": cfg.get("explore", {}).get("map_model", ""),
        "max_retries": orch.get("max_retries", 4),
        "publish_remote": cfg.get("publish", {}).get("remote", "origin"),
    }


@app.post("/api/config")
async def api_update_config(request: Request):
    """Update model configuration at runtime."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    try:
        orchestrator.update_models(body)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/models")
async def api_models():
    """Return available opencode model IDs by running `opencode models`."""
    import subprocess as _sp
    try:
        out = _sp.check_output(["opencode", "models"], text=True, timeout=10)
        models = sorted(line.strip() for line in out.splitlines() if line.strip())
        return {"models": models}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/todos/{todo_id}/analyze")
async def api_analyze_todo(todo_id: str):
    """Run the analyzer agent on a single TodoItem.
    Returns 409 if already analyzing or dispatched; 404 if not found; 500 on agent error.
    """
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    result = orchestrator.analyze_todo_item(todo_id)
    if "error" in result:
        http_status = result.pop("status", 400)
        return JSONResponse(result, status_code=http_status)
    return result


@app.get("/api/todos/queue")
async def api_todo_queue():
    """Return all TodoItems currently being analyzed (status=analyzing)."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    items = orchestrator.db.get_all_todo_items()
    analyzing = [i.to_dict() for i in items
                 if i.status.value == "analyzing"]
    return {"analyzing": analyzing, "count": len(analyzing)}


@app.post("/api/todos/dispatch")
async def api_dispatch_todos(request: Request):
    """Send selected TODO items to the planner (creates pending tasks)."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    tasks = orchestrator.dispatch_todos_to_planner(ids)
    return {"dispatched": len(tasks), "tasks": tasks}


@app.post("/api/todos/revert")
async def api_revert_todos(request: Request):
    """Revert dispatched TODO items back to analyzed status."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    count = orchestrator.revert_todo_items(ids)
    return {"reverted": count}


@app.post("/api/todos/delete")
async def api_delete_todos(request: Request):
    """Delete selected TODO items."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    ids = body.get("ids", [])
    count = orchestrator.delete_todo_items(ids)
    return {"deleted": count}


@app.post("/api/dispatch-all")
async def api_dispatch_all():
    """Dispatch all pending tasks."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    pending = orchestrator.db.get_pending_tasks()
    dispatched = 0
    for t in pending:
        if orchestrator.dispatch_task(t.id):
            dispatched += 1
    return {"dispatched": dispatched, "total_pending": len(pending)}


# ── Exploration API ──────────────────────────────────────────────────

@app.get("/api/explore/modules")
async def api_explore_modules():
    """Return all explore modules (flat list; frontend builds tree from parent_id)."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    modules = orchestrator.db.get_all_explore_modules()
    return [m.to_dict() for m in modules]


@app.get("/api/explore/modules/{module_id}")
async def api_explore_module_detail(module_id: str):
    """Return a single module with its exploration runs."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    module = orchestrator.db.get_explore_module(module_id)
    if not module:
        return JSONResponse({"error": "Module not found"}, status_code=404)
    runs = orchestrator.db.get_explore_runs_for_module(module_id)
    client = orchestrator.client
    parsed_runs = []
    for r in runs:
        rd = r.to_dict()
        rd["parsed"] = client.parse_readable_output(r.output)
        rd.pop("output", None)
        parsed_runs.append(rd)
    client = orchestrator.client
    parsed_runs = []
    for r in runs:
        rd = r.to_dict()
        rd["parsed"] = client.parse_readable_output(r.output)
        rd.pop("output", None)
        parsed_runs.append(rd)
    return {
        "module": module.to_dict(),
        "runs": parsed_runs,
    }


@app.post("/api/explore/init-map")
async def api_init_explore_map():
    """Trigger map initialization (stateful, non-reentrant)."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    result = orchestrator.start_init_explore_map()
    if not result.get("accepted", False):
        return JSONResponse(result, status_code=409)
    return result


@app.post("/api/explore/init-map/cancel")
async def api_cancel_init_explore_map():
    """Cancel the current map initialization run."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    return orchestrator.cancel_init_explore_map()


@app.get("/api/explore/status")
async def api_explore_status():
    """Return repository + map-init status for explore UI state gating."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    return orchestrator.get_explore_status()


@app.post("/api/explore/start")
async def api_start_exploration(request: Request):
    """Start exploration on selected modules x categories."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    result = orchestrator.start_exploration(
        module_ids=body.get("module_ids"),
        categories=body.get("categories"),
        focus_point=body.get("focus_point", ""),
    )
    return result


@app.get("/api/explore/queue")
async def api_explore_queue():
    """Return current exploration queue/running state."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    return orchestrator.get_exploration_queue_state()


@app.post("/api/explore/cancel")
async def api_cancel_exploration(request: Request):
    """Cancel exploration jobs by scope; default cancels all categories/modules."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return orchestrator.cancel_exploration(
        module_ids=body.get("module_ids"),
        categories=body.get("categories"),
        include_running=bool(body.get("include_running", True)),
    )


@app.post("/api/explore/modules")
async def api_add_explore_module(request: Request):
    """Manually add a module to the exploration map."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    result = orchestrator.add_explore_module(
        name=name,
        path=path,
        parent_id=body.get("parent_id", ""),
        description=body.get("description", ""),
    )
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/explore/modules/{module_id}/update")
async def api_update_explore_module(module_id: str, request: Request):
    """Update module name, description, or reset category status."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    result = orchestrator.update_explore_module(module_id, body)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


@app.delete("/api/explore/modules/{module_id}")
async def api_delete_explore_module(module_id: str):
    """Delete a module and all its descendants."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    result = orchestrator.delete_explore_module(module_id)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


@app.get("/api/explore/runs")
async def api_explore_runs():
    """List recent exploration runs."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    runs = orchestrator.db.get_all_explore_runs()
    return [r.to_dict() for r in runs[:100]]


@app.get("/api/explore/runs/{run_id}")
async def api_explore_run_detail(run_id: str):
    """Get full details of one exploration run."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    run = orchestrator.db.get_explore_run(run_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    rd = run.to_dict()
    rd.pop("output", None)
    rd.pop("prompt", None)
    return rd


@app.post("/api/explore/runs/{run_id}/create-task")
async def api_create_task_from_finding(run_id: str, request: Request):
    """Create a Task from a specific finding in an explore run."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    body = await request.json()
    finding_index = body.get("finding_index", -1)
    result = orchestrator.create_task_from_finding(run_id, finding_index)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@app.get("/api/explore/categories")
async def api_explore_categories():
    """Return the configured exploration categories."""
    if not orchestrator:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    return {"categories": orchestrator._get_explore_categories()}


# ── Dashboard HTML ───────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


def _fmt_time(ts):
    if ts == 0:
        return "-"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multi-Agent TODO Resolver</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text-dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --purple: #bc8cff;
    --orange: #f0883e;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.5; }
  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
  header { display: flex; justify-content: space-between; align-items: center;
           padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
  header h1 { font-size: 20px; font-weight: 600; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
           gap: 12px; margin-bottom: 24px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border);
               border-radius: 8px; padding: 16px; text-align: center; }
  .stat-card .num { font-size: 28px; font-weight: 700; }
  .stat-card .label { font-size: 12px; color: var(--text-dim); text-transform: uppercase; }
  .actions { display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
  .btn { padding: 8px 16px; border: 1px solid var(--border); border-radius: 6px;
         background: var(--surface); color: var(--text); cursor: pointer; font-size: 13px;
         transition: all 0.15s; }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn-sm { padding: 4px 10px; font-size: 12px; }
  .btn-primary { background: #238636; border-color: #238636; color: white; }
  .btn-primary:hover { background: #2ea043; }
  .task-table { width: 100%; border-collapse: collapse; }
  .task-table th, .task-table td { padding: 10px 12px; text-align: left;
    border-bottom: 1px solid var(--border); font-size: 13px; }
  .task-table th { color: var(--text-dim); font-weight: 600; background: var(--surface); position: sticky; top: 0; }
  .task-table tr:hover { background: rgba(88,166,255,0.04); }
  .badge { padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; display: inline-block; }
  .badge-pending { background: #30363d; color: var(--text-dim); }
  .badge-planning { background: #1f2d3d; color: var(--purple); }
  .badge-coding { background: #0d2d42; color: var(--accent); }
  .badge-reviewing { background: #2a1f0d; color: var(--yellow); }
  .badge-completed { background: #0d2d1a; color: var(--green); }
  .badge-failed, .badge-review_failed { background: #2d0d0d; color: var(--red); }
  .badge-needs_arbitration { background: #2d1f0d; color: #f0a040; border: 1px solid #f0a040; }
  .badge-cancelled { background: #30363d; color: var(--text-dim); }
  .badge-high { color: var(--red); } .badge-medium { color: var(--yellow); }
  .badge-low { color: var(--text-dim); }
  .sys-select { width: 100%; padding: 5px 8px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 6px; color: var(--text);
    font-size: 12px; font-family: monospace; cursor: pointer; }
  .sys-select:focus { outline: none; border-color: var(--accent); }
  .sys-select option { background: var(--surface); color: var(--text); }
  .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.6); z-index: 100; justify-content: center; align-items: flex-start;
    padding-top: 40px; overflow-y: auto; }
  .modal-overlay.active { display: flex; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
           padding: 24px; width: 500px; max-width: 90vw; margin-bottom: 40px; }
  .modal-wide { width: 900px; max-height: 85vh; overflow-y: auto; }
  .modal h2 { margin-bottom: 16px; font-size: 16px; }
  .modal input, .modal textarea, .modal select { width: 100%; padding: 8px 12px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); margin-bottom: 12px; font-family: inherit; font-size: 13px; }
  .modal textarea { min-height: 100px; resize: vertical; }

  /* Detail page sections */
  .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
  .detail-card { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .detail-card h4 { font-size: 11px; color: var(--text-dim); text-transform: uppercase; margin-bottom: 6px; letter-spacing: 0.5px; }
  .detail-card .val { font-size: 13px; word-break: break-all; }
  .detail-section { margin-bottom: 16px; }
  .detail-section h3 { font-size: 14px; color: var(--text); margin-bottom: 8px; font-weight: 600;
    border-bottom: 1px solid var(--border); padding-bottom: 4px; }
  .detail-section pre { background: var(--bg); padding: 12px; border-radius: 6px;
    font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-word;
    max-height: 500px; overflow-y: auto; border: 1px solid var(--border); }

  /* Session info */
  .session-box { background: #0d1d2d; border: 1px solid #1f3d5d; border-radius: 8px;
    padding: 10px 14px; margin-bottom: 12px; font-size: 12px; }
  .session-box .session-label { color: var(--accent); font-weight: 600; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.5px; }
  .session-box code { color: var(--accent); background: rgba(88,166,255,0.1); padding: 2px 6px;
    border-radius: 4px; font-size: 12px; user-select: all; }
  .session-box .cmd { color: var(--text-dim); margin-top: 4px; font-family: monospace; font-size: 11px; user-select: all; }

  /* Agent run card */
  .run-card { background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 12px; overflow: hidden; }
  .run-header { display: flex; justify-content: space-between; align-items: center;
    padding: 10px 14px; background: rgba(255,255,255,0.02); cursor: pointer;
    border-bottom: 1px solid var(--border); }
  .run-header:hover { background: rgba(255,255,255,0.04); }
  .run-header .run-title { font-weight: 600; font-size: 13px; }
  .run-header .run-meta { font-size: 11px; color: var(--text-dim); }
  .run-body { display: none; padding: 12px 14px; max-height: 60vh; overflow-y: auto; }
  .prompt-section { margin-bottom: 10px; }
  .prompt-label { font-size: 12px; color: var(--text-dim); margin-bottom: 4px; cursor: pointer;
    user-select: none; display: flex; gap: 6px; align-items: center; }
  .prompt-label:hover { color: var(--text); }
  .prompt-label .prompt-arrow { font-size: 10px; }
  .prompt-body { margin-top: 0; }
  .prompt-body.collapsed { display: none; }
  .prompt-pre { font-size: 12px; font-family: monospace; color: var(--text);
    padding: 4px 8px; background: rgba(255,255,255,0.02);
    border-radius: 4px; margin: 2px 0; white-space: pre-wrap; word-break: break-word;
    max-height: 300px; overflow-y: auto; }
  .run-body.open { display: block; }
  .run-summary { display: flex; gap: 16px; font-size: 12px; color: var(--text-dim); margin-bottom: 8px; }

  /* Step rendering */
  .step { margin-bottom: 10px; }
  .step-header { font-size: 12px; font-weight: 600; color: var(--purple); margin-bottom: 4px;
    padding: 4px 8px; background: rgba(188,140,255,0.06); border-radius: 4px; display: inline-block; }
  .step-event { padding: 3px 0; font-size: 12px; font-family: monospace; }
  .ev-text { color: var(--text); padding: 4px 8px; background: rgba(255,255,255,0.02);
    border-radius: 4px; margin: 2px 0; white-space: pre-wrap; word-break: break-word;
    max-height: 300px; overflow-y: auto; }
  .ev-tool { padding: 4px 8px; background: rgba(88,166,255,0.04); border-left: 2px solid var(--accent);
    margin: 2px 0; border-radius: 0 4px 4px 0; }
  .ev-tool .tool-name { color: var(--accent); font-weight: 600; }
  .ev-tool .tool-status { font-size: 10px; padding: 1px 6px; border-radius: 8px; margin-left: 6px; }
  .ev-tool .tool-status.completed { background: rgba(63,185,80,0.15); color: var(--green); }
  .ev-tool .tool-status.running { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .ev-tool .tool-status.error { background: rgba(248,81,73,0.15); color: var(--red); }
  .ev-tool .tool-io { font-size: 11px; color: var(--text-dim); margin-top: 2px; word-break: break-all;
    max-height: 200px; overflow-y: auto; }
  .ev-tool .tool-io .io-label { color: var(--text-dim); font-weight: 600; }
  .ev-tool .tool-io .io-val { color: #9ca3af; }
  .step-finish { font-size: 11px; color: var(--text-dim); font-style: italic; margin-top: 2px; }
  .ev-time { color: var(--text-dim); font-size: 10px; margin-right: 6px; }

  #refresh-indicator { color: var(--text-dim); font-size: 12px; }
  #refresh-breakdown { color: var(--text-dim); font-size: 11px; margin-left: 10px; }
  .copy-btn { cursor: pointer; color: var(--text-dim); font-size: 11px; margin-left: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner { display:inline-block; width:12px; height:12px; border:2px solid rgba(255,255,255,0.2);
    border-top-color:var(--yellow); border-radius:50%; animation:spin 0.8s linear infinite;
    vertical-align:middle; margin-right:4px; }
  .analyze-queue-banner { background:rgba(210,153,34,0.1); border:1px solid rgba(210,153,34,0.3);
    border-radius:6px; padding:10px 14px; margin-bottom:12px; font-size:12px; }
  .analyze-queue-banner .aq-title { color:var(--yellow); font-weight:600; margin-bottom:6px; }
  .analyze-queue-item { display:flex; align-items:center; gap:8px; padding:3px 0;
    border-bottom:1px solid rgba(255,255,255,0.05); font-family:monospace; }
  .analyze-queue-item:last-child { border-bottom:none; }
  .prog-row { display:grid; grid-template-columns:2fr 80px 80px 1fr auto; gap:8px;
    align-items:center; padding:8px; border-bottom:1px solid var(--border);
    font-size:12px; }
  .prog-row:last-child { border-bottom:none; }
  .prog-status-waiting  { color:var(--text-dim); }
  .prog-status-running  { color:var(--yellow); }
  .prog-status-done     { color:var(--green); }
  .prog-status-skipped  { color:var(--text-dim); font-style:italic; }
  .prog-status-error    { color:var(--red); }
  .prog-output { background:var(--bg); border:1px solid var(--border); border-radius:4px;
    padding:8px; font-size:11px; font-family:monospace; white-space:pre-wrap;
    max-height:180px; overflow-y:auto; margin-top:6px; color:var(--text-dim); }
  .copy-btn:hover { color: var(--accent); }
  .tab-bar { display: flex; gap: 0; margin-bottom: 16px; border-bottom: 1px solid var(--border); }
  .tab { padding: 8px 16px; cursor: pointer; font-size: 13px; color: var(--text-dim);
    border-bottom: 2px solid transparent; transition: all 0.15s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .modal-wide .tab-content.active { max-height: 65vh; overflow-y: auto; }
  .dialog-msg { font-size: 13px; color: var(--text); white-space: pre-wrap; }
  .modal-danger { border-color: rgba(248,81,73,0.45); box-shadow: 0 0 0 1px rgba(248,81,73,0.15) inset; }

  /* Explore tree */
  .exp-node { margin-left: 0; }
  .exp-node-inner { display: flex; align-items: center; gap: 6px; padding: 6px 8px;
    border-radius: 6px; cursor: pointer; font-size: 13px; transition: background 0.1s; }
  .exp-node-inner:hover { background: rgba(88,166,255,0.06); }
  .exp-node-inner.selected { background: rgba(88,166,255,0.12); border-left: 3px solid var(--accent); }
  .exp-toggle { width: 16px; text-align: center; color: var(--text-dim); font-size: 10px;
    cursor: pointer; flex-shrink: 0; user-select: none; }
  .exp-name { font-weight: 500; flex-shrink: 0; }
  .exp-path { color: var(--text-dim); font-size: 11px; font-family: monospace; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .exp-cats { display: flex; gap: 3px; flex-shrink: 0; }
  .exp-cat-dot { width: 10px; height: 10px; border-radius: 50%; border: 1px solid rgba(255,255,255,0.15); }
  .exp-cat-dot[title]:hover { transform: scale(1.3); }
  .exp-children { margin-left: 20px; border-left: 1px solid var(--border); }
  .exp-detail-cats { display: grid; grid-template-columns: 1fr; gap: 8px; }
  .exp-cat-row { display: flex; align-items: center; gap: 12px; padding: 10px 12px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
  .exp-cat-label { font-size: 12px; font-weight: 600; min-width: 110px; }
  .exp-cat-status { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
  .exp-finding { background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; margin-bottom: 8px; }
  .exp-finding-title { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  .exp-finding-desc { font-size: 12px; color: var(--text-dim); margin-bottom: 6px; }
  .exp-finding-meta { font-size: 11px; color: var(--text-dim); font-family: monospace; }
  .sev-critical { color: var(--red); border-color: var(--red); }
  .sev-major { color: var(--orange); border-color: var(--orange); }
  .sev-minor { color: var(--yellow); border-color: var(--yellow); }
  .sev-info { color: var(--text-dim); border-color: var(--text-dim); }

  .explore-filters { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-bottom:12px; }
  .explore-filter-card { border:1px solid var(--border); border-radius:8px; padding:10px; background:var(--surface); }
  .explore-filter-card h4 { margin:0 0 8px 0; font-size:12px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; }
  .explore-filter-hint { margin-top:6px; font-size:11px; color:var(--text-dim); }
  .explore-category-filters { display:flex; flex-wrap:wrap; gap:8px; }
  .explore-cat-chip { display:inline-flex; align-items:center; gap:6px; font-size:12px; border:1px solid var(--border); border-radius:999px; padding:4px 8px; }
  .explore-cat-chip input { margin:0; }

  .explore-queue-panel { border:1px solid var(--border); border-radius:8px; padding:10px; margin-bottom:12px; background:var(--surface); }
  .explore-queue-header { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:8px; }
  .explore-queue-list { display:flex; flex-direction:column; gap:6px; }
  .explore-queue-item { border:1px solid var(--border); border-radius:6px; padding:8px; font-size:12px; background:var(--bg); display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .explore-queue-item .meta { color:var(--text-dim); font-size:11px; font-family:monospace; }
  .explore-queue-item.running { border-left:3px solid var(--yellow); }
  .explore-queue-item.queued { border-left:3px solid var(--accent); }
  .explore-init-log { max-height:220px; overflow:auto; font-size:11px; font-family:monospace;
    white-space:pre-wrap; word-break:break-word; background:var(--bg); border:1px solid var(--border);
    border-radius:6px; padding:8px; margin-top:8px; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Multi-Agent TODO Resolver</h1>
    <div>
      <span id="refresh-indicator">Auto-refresh: 5s</span>
      <span id="refresh-breakdown"></span>
      <button class="btn" onclick="refresh()">Refresh</button>
    </div>
  </header>

  <div class="stats" id="stats"></div>

  <div class="actions">
    <button class="btn btn-primary" onclick="showAddTask()">+ Submit Task</button>
    <button class="btn" onclick="showTodosPanel()">&#9776; TODOs</button>
    <button class="btn" onclick="dispatchAll()">Dispatch All</button>
  </div>

  <!-- Main view tabs -->
  <div class="tab-bar" id="main-tab-bar">
    <div class="tab active" onclick="switchMainTab(this,'main-tasks')">Tasks</div>
    <div class="tab" onclick="switchMainTab(this,'main-todos')">Scanned TODOs <span id="todo-badge"></span></div>
    <div class="tab" onclick="switchMainTab(this,'main-explore')">Explore</div>
    <div class="tab" onclick="switchMainTab(this,'main-sysinfo')">System Info</div>
  </div>

  <!-- Tasks table -->
  <div id="main-tasks">
    <table class="task-table">
      <thead>
        <tr><th>ID</th><th>Title</th><th>Status</th><th>Priority</th><th>Source</th><th>Sessions</th><th>Updated</th><th>Actions</th></tr>
      </thead>
      <tbody id="task-list"></tbody>
    </table>
  </div>

  <!-- System Info panel -->
  <div id="main-sysinfo" style="display:none">
    <div id="sysinfo-content" style="padding:8px 0"><span style="color:var(--text-dim)">Loading...</span></div>
  </div>

  <!-- Explore panel -->
  <div id="main-explore" style="display:none">
    <div class="actions" style="margin-bottom:12px">
      <button class="btn btn-primary" id="explore-init-btn" onclick="initExploreMap()">&#9881; Initialize Map</button>
      <button class="btn" style="color:var(--green)" id="explore-start-btn" onclick="startExploration()">&#9654; Start Exploration</button>
      <button class="btn" style="color:var(--red)" id="explore-cancel-btn" onclick="cancelExplorationByFilters()">&#10005; Cancel Exploration</button>
      <button class="btn" onclick="showAddModuleModal()">+ Add Module</button>
      <span id="explore-repo-label" style="font-size:12px;color:var(--text-dim)"></span>
      <span id="explore-start-summary" style="font-size:11px;color:var(--text-dim)"></span>
    </div>
    <div id="explore-init-state" class="explore-queue-panel"></div>
    <div class="explore-filters">
      <div class="explore-filter-card">
        <h4>Target Modules</h4>
        <select id="explore-module-select" multiple style="width:100%;min-height:92px;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px"></select>
        <div class="explore-filter-hint">No selection = all leaf modules</div>
      </div>
      <div class="explore-filter-card">
        <h4>Categories</h4>
        <div id="explore-category-filters" class="explore-category-filters"></div>
        <div class="explore-filter-hint">Select categories to run/cancel</div>
      </div>
      <div class="explore-filter-card">
        <h4>Focus Point</h4>
        <input id="explore-focus-point" placeholder="e.g. hash table resize path contention" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px" />
        <div class="explore-filter-hint">Optional: custom concern within selected module/category scope</div>
      </div>
    </div>
    <div id="explore-queue" class="explore-queue-panel"></div>
    <div style="display:flex;gap:16px;align-items:flex-start">
      <!-- Module tree (left pane) -->
      <div id="explore-tree" style="flex:1;min-width:0;max-height:70vh;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--surface)">
        <span style="color:var(--text-dim);font-size:12px">No modules yet. Click "Initialize Map" to scan the repository.</span>
      </div>
      <!-- Module detail (right pane) -->
      <div id="explore-detail" style="flex:1;min-width:0;max-height:70vh;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:16px;background:var(--surface)">
        <span style="color:var(--text-dim);font-size:12px">Select a module to view details.</span>
      </div>
    </div>
  </div>

  <!-- Add Module Modal -->
  <div class="modal-overlay" id="add-module-modal">
    <div class="modal">
      <h2>Add Module</h2>
      <label style="font-size:12px;color:var(--text-dim);display:block;margin-bottom:4px">Module Name</label>
      <input id="add-mod-name" placeholder="e.g. Query Optimizer" />
      <label style="font-size:12px;color:var(--text-dim);display:block;margin-bottom:4px">Path (relative to repo root)</label>
      <input id="add-mod-path" placeholder="e.g. be/src/optimizer" />
      <label style="font-size:12px;color:var(--text-dim);display:block;margin-bottom:4px">Description</label>
      <textarea id="add-mod-desc" placeholder="What does this module do?" style="height:60px"></textarea>
      <label style="font-size:12px;color:var(--text-dim);display:block;margin-bottom:4px">Parent Module</label>
      <select id="add-mod-parent" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);margin-bottom:12px">
        <option value="">(root level)</option>
      </select>
      <div class="actions">
        <button class="btn btn-primary" onclick="doAddModule()">Add</button>
        <button class="btn" onclick="closeModals()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- TODOs panel -->
  <div id="main-todos" style="display:none">
    <div class="actions" style="margin-bottom:12px">
      <button class="btn btn-primary" onclick="showScanModal()">&#8635; Scan TODOs</button>
      <button class="btn" onclick="analyzeSelected()">&#9881; Analyze Selected</button>
      <button class="btn" style="color:var(--green)" onclick="dispatchSelected()">&#9654; Send to Planner</button>
      <button class="btn" style="color:var(--yellow)" onclick="revertSelected()">&#8634; Revert to Analyzed</button>
      <button class="btn" style="color:var(--red)" onclick="deleteSelected()">&#128465; Delete</button>
      <button class="btn btn-sm" onclick="selectAllTodos()">Select All</button>
      <button class="btn btn-sm" onclick="selectNoneTodos()">Select None</button>
    </div>
    <!-- Persistent analyze queue banner (shown when any item is ANALYZING) -->
    <div id="analyze-queue-banner" class="analyze-queue-banner" style="display:none">
      <div class="aq-title"><span class="spinner"></span>Analysis in progress</div>
      <div id="analyze-queue-items"></div>
    </div>
    <table class="task-table" id="todo-table">
      <thead>
        <tr>
          <th style="width:32px"><input type="checkbox" id="todo-check-all" onchange="toggleAllTodos(this)"></th>
          <th>File</th>
          <th>Description</th>
          <th style="width:100px">Feasibility</th>
          <th style="width:100px">Difficulty</th>
          <th>Analysis Note</th>
          <th style="width:100px">Status</th>
          <th style="width:80px">Actions</th>
        </tr>
      </thead>
      <tbody id="todo-list"></tbody>
    </table>
  </div>
</div>

<!-- Analyze Progress Modal -->
<div class="modal-overlay" id="analyze-progress-modal">
  <div class="modal modal-wide">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 style="margin:0">&#9881; Analyze Progress</h2>
      <button class="btn btn-sm" id="analyze-close-btn" onclick="closeAnalyzeModal()">Close</button>
    </div>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:12px">
      Scores: <strong style="color:var(--green)">Feasibility</strong> = can/should it be done now (higher is better);
      <strong style="color:var(--yellow)">Difficulty</strong> = how hard to implement (higher is harder).
    </div>
    <!-- Header row -->
    <div class="prog-row" style="background:var(--surface);font-weight:600;color:var(--text-dim);font-size:11px;text-transform:uppercase;letter-spacing:0.4px">
      <div>TODO</div><div>Feasibility</div><div>Difficulty</div><div>Note</div><div>Status</div>
    </div>
    <div id="analyze-prog-list"></div>
    <div id="analyze-prog-summary" style="margin-top:12px;font-size:12px;color:var(--text-dim)"></div>
  </div>
</div>

<!-- Scan TODOs Modal -->
<div class="modal-overlay" id="scan-modal">
  <div class="modal">
    <h2>Scan TODOs</h2>
    <p style="font-size:12px;color:var(--text-dim);margin-bottom:10px">Scan a subdirectory for TODO/FIXME/HACK/XXX comments. Leave directory empty to scan the whole repository.</p>
    <label style="font-size:12px;color:var(--text-dim);display:block;margin-bottom:4px">Subdirectory (relative to repo root)</label>
    <input id="scan-subdir" placeholder="e.g. be/src/olap  (empty = whole repo)" />
    <label style="font-size:12px;color:var(--text-dim);display:block;margin:8px 0 4px">Max results</label>
    <input id="scan-limit" type="number" min="0" placeholder="0 = no limit" value="100" style="width:120px" />
    <div class="actions" style="margin-top:12px">
      <button class="btn btn-primary" id="scan-submit-btn" onclick="doScanTodos()">Scan</button>
      <button class="btn" onclick="closeModals()">Cancel</button>
    </div>
    <div id="scan-result" style="margin-top:10px;font-size:12px"></div>
  </div>
</div>

<!-- Add Task Modal (tabbed: Develop / Review) -->
<div class="modal-overlay" id="add-modal">
  <div class="modal">
    <h2>Submit Task</h2>
    <div id="add-task-base-branch-banner" style="margin-bottom:12px;padding:10px 12px;border:1px solid var(--yellow);border-radius:8px;background:rgba(245,158,11,0.08);color:var(--yellow);font-size:12px;line-height:1.5">
      <div style="font-weight:700;margin-bottom:4px">Base branch notice</div>
      <div>
        New task worktrees are created from the configured base branch
        <code id="add-task-base-branch-name">loading...</code>
        (remote ref <code id="add-task-base-branch-ref">origin/loading...</code>), not from your current local checkout branch.
      </div>
    </div>
    <div class="tab-bar" id="add-task-tabs">
      <div class="tab active" onclick="switchAddTab(this, 'add-develop')">Develop</div>
      <div class="tab" onclick="switchAddTab(this, 'add-review')">Review</div>
    </div>

    <!-- Develop tab -->
    <div class="tab-content active" id="add-develop">
      <p style="font-size:12px;color:var(--text-dim);margin-bottom:10px">The Planner will analyze this and either implement it directly or break it into sub-tasks automatically.</p>
      <input id="task-title" placeholder="Task title" />
      <textarea id="task-desc" placeholder="Describe the task. Can be simple (one TODO) or complex (multi-module refactor)."></textarea>
      <select id="task-priority">
        <option value="high">High</option>
        <option value="medium" selected>Medium</option>
        <option value="low">Low</option>
      </select>
      <label style="font-size:12px;color:var(--text-dim);display:block;margin:8px 0 4px">Copy files to worktree <span style="font-size:11px">(one path per line, relative to repo root)</span></label>
      <textarea id="task-copy-files" placeholder="e.g. test_data/input.csv&#10;debug/repro.sql" style="height:60px;font-family:monospace;font-size:12px"></textarea>
      <div class="actions">
        <button class="btn btn-primary" onclick="addTask()">Submit Develop Task</button>
        <button class="btn" onclick="closeModals()">Cancel</button>
      </div>
    </div>

    <!-- Review tab -->
    <div class="tab-content" id="add-review">
      <p style="font-size:12px;color:var(--text-dim);margin-bottom:10px">Submit a patch, GitHub PR link, or code diff for review. Only reviewers will run (no coding agent).</p>
      <input id="review-task-title" placeholder="Review title (optional)" />
      <textarea id="review-task-input" placeholder="Paste a patch / diff, GitHub PR URL, or describe what to review..." style="height:160px;font-family:monospace;font-size:12px"></textarea>
      <select id="review-task-priority">
        <option value="high">High</option>
        <option value="medium" selected>Medium</option>
        <option value="low">Low</option>
      </select>
      <label style="font-size:12px;color:var(--text-dim);display:block;margin:8px 0 4px">Copy files to worktree <span style="font-size:11px">(one path per line, relative to repo root; e.g. patch files for reviewer to read)</span></label>
      <textarea id="review-task-copy-files" placeholder="e.g. patches/fix.patch&#10;docs/design.md" style="height:60px;font-family:monospace;font-size:12px"></textarea>
      <div class="actions">
        <button class="btn btn-primary" onclick="addReviewTask()">Submit Review Task</button>
        <button class="btn" onclick="closeModals()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<!-- Task Detail Modal -->
<div class="modal-overlay" id="detail-modal">
  <div class="modal modal-wide">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 id="detail-title" style="margin:0">Task Detail</h2>
      <button class="btn btn-sm" onclick="closeModals()">Close</button>
    </div>
    <div id="detail-content"></div>
  </div>
</div>

<!-- Generic Alert / Confirm Modal -->
<div class="modal-overlay" id="dialog-modal">
  <div class="modal" id="dialog-box">
    <h2 id="dialog-title">Notice</h2>
    <div id="dialog-message" class="dialog-msg" style="margin-bottom:14px"></div>
    <div class="actions" style="justify-content:flex-end; margin-bottom:0">
      <button class="btn" id="dialog-cancel-btn" style="display:none">Cancel</button>
      <button class="btn btn-primary" id="dialog-ok-btn">OK</button>
    </div>
  </div>
</div>

<script>
const API = '';

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: {'Content-Type': 'application/json'}, ...opts
  });
  return res.json();
}

function fmtTime(ts) {
  if (!ts) return '-';
  return new Date(ts * 1000).toLocaleString();
}

function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML.replace(/"/g, '&quot;');
}

function fmtMs(ms) {
  return `${ms.toFixed(1)}ms`;
}

function updateRefreshPerfLabel(m) {
  const indicator = document.getElementById('refresh-indicator');
  const breakdown = document.getElementById('refresh-breakdown');
  if (!indicator || !breakdown) return;
  indicator.textContent = 'Auto-refresh: 5s';

  if (!m) {
    breakdown.textContent = '';
    return;
  }

  if (m.skipped) {
    breakdown.textContent = `hash-hit total=${fmtMs(m.total_ms)} fetch=${fmtMs(m.fetch_ms)} hash=${fmtMs(m.hash_ms)}`;
    return;
  }

  breakdown.textContent = `total=${fmtMs(m.total_ms)} fetch=${fmtMs(m.fetch_ms)} hash=${fmtMs(m.hash_ms)} stats=${fmtMs(m.stats_ms)} tree=${fmtMs(m.tree_ms)} html=${fmtMs(m.rows_ms)} dom=${fmtMs(m.dom_ms)}`;
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => {
    // brief visual feedback could be added here
  });
}

// Render a single session box
function renderSessionBox(sessionId, label) {
  if (!sessionId) return '';
  const cmd = `opencode --session ${sessionId}`;
  // Session IDs are alphanumeric+underscore — safe to embed directly in JS
  // string literals.  Using esc() here would HTML-encode the value and break
  // the onclick handler.
  const safeCmd = cmd.replace(/'/g, "\\'");
  return `<div class="session-box">
    <div class="session-label">${esc(label)}</div>
    <code>${esc(sessionId)}</code>
    <span class="copy-btn" onclick="copyText('${safeCmd}')" title="Copy command">[copy]</span>
    <div class="cmd">${esc(cmd)}</div>
  </div>`;
}

// Render all session info for a task
function renderSessions(sessionIds) {
  if (!sessionIds || !Object.keys(sessionIds).length) return '<span style="color:var(--text-dim)">-</span>';
  let html = '';
  for (const [agent, ids] of Object.entries(sessionIds)) {
    if (Array.isArray(ids)) {
      for (let i = 0; i < ids.length; i++) {
        const label = ids.length > 1 ? `${agent} #${i+1}` : agent;
        html += renderSessionBox(ids[i], label);
      }
    }
  }
  return html || '<span style="color:var(--text-dim)">-</span>';
}

// Count total sessions for the task list table
function countSessions(sessionIds) {
  if (!sessionIds) return 0;
  let n = 0;
  for (const ids of Object.values(sessionIds)) {
    if (Array.isArray(ids)) n += ids.length;
  }
  return n;
}

function renderTaskComments(comments) {
  if (!comments || !comments.length) {
    return '<span style="color:var(--text-dim)">No comments yet.</span>';
  }
  return comments.map(c => `
    <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px;background:var(--bg)">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:6px">
        <strong style="font-size:12px;color:var(--text)">${esc(c.username || '-')}</strong>
        <span style="font-size:11px;color:var(--text-dim)">${fmtTime(c.created_at)}</span>
      </div>
      <div style="font-size:12px;color:var(--text-dim);white-space:pre-wrap;word-break:break-word">${esc(c.content || '')}</div>
    </div>
  `).join('');
}

// Render prompt/input section for a run — default open, same style as output
function renderPromptSection(prompt, idx) {
  if (!prompt) return '';
  const bodyId = `prompt-body-${idx}`;
  return `<div class="prompt-section">
    <div class="prompt-label" onclick="togglePromptBody('${bodyId}', this)">
      <span class="prompt-arrow">&#9660;</span>
      <span>Input prompt</span>
    </div>
    <div class="prompt-body" id="${bodyId}">
      <div class="step-event"><div class="ev-text prompt-pre">${esc(prompt)}</div></div>
    </div>
  </div>`;
}

function togglePromptBody(id, labelEl) {
  const body = document.getElementById(id);
  const collapsed = body.classList.toggle('collapsed');
  labelEl.querySelector('.prompt-arrow').innerHTML = collapsed ? '&#9654;' : '&#9660;';
}

// Render structured step events from a parsed run
function renderParsedRun(parsed) {
  if (!parsed || !parsed.steps || !parsed.steps.length) {
    if (parsed && parsed.raw_fallback) {
      return `<pre style="font-size:12px;color:var(--text-dim)">${esc(parsed.raw_fallback)}</pre>`;
    }
    return '<span style="color:var(--text-dim)">No events</span>';
  }

  let html = '';
  const s = parsed.summary || {};
  html += `<div class="run-summary">
    <span>Steps: <b>${s.total_steps||0}</b></span>
    <span>Text: <b>${s.text_segments||0}</b></span>
    <span>Tool calls: <b>${s.tool_calls||0}</b></span>
  </div>`;

  for (const step of parsed.steps) {
    html += `<div class="step">`;
    html += `<div class="step-header">Step ${step.step_num}</div>`;
    for (const ev of (step.events || [])) {
      if (ev.type === 'text') {
        html += `<div class="step-event"><span class="ev-time">${esc(ev.time)}</span><div class="ev-text">${esc(ev.content)}</div></div>`;
      } else if (ev.type === 'tool') {
        const statusCls = (ev.status === 'completed') ? 'completed' : (ev.status === 'error' ? 'error' : 'running');
        html += `<div class="step-event"><span class="ev-time">${esc(ev.time)}</span><div class="ev-tool">`;
        html += `<span class="tool-name">${esc(ev.tool)}</span>`;
        html += `<span class="tool-status ${statusCls}">${esc(ev.status)}</span>`;
        if (ev.input) {
          html += `<div class="tool-io"><span class="io-label">in: </span><span class="io-val">${esc(ev.input)}</span></div>`;
        }
        if (ev.output) {
          html += `<div class="tool-io"><span class="io-label">out: </span><span class="io-val">${esc(ev.output)}</span></div>`;
        }
        html += `</div></div>`;
      }
    }
    if (step.finish_reason) {
      html += `<div class="step-finish">-> ${esc(step.finish_reason)}</div>`;
    }
    html += `</div>`;
  }
  return html;
}

// Render git status for the worktree
function renderGitStatus(gs, worktreePath, branchName, taskId) {
  if (!gs || gs.error) {
    const msg = (gs && gs.error) ? gs.error : 'No worktree assigned yet';
    return `<span style="color:var(--text-dim)">${esc(msg)}</span>`;
  }

  const totalChanged = (gs.staged||[]).length + (gs.unstaged||[]).length + (gs.untracked||[]).length;
  const cleanMsg = totalChanged === 0
    ? `<span style="color:var(--green)">&#10003; Working tree clean</span>`
    : '';

  let html = `<div style="margin-bottom:12px">`;

  // Header row
  html += `<div style="display:flex;gap:16px;align-items:center;margin-bottom:10px;flex-wrap:wrap">`;
  if (gs.branch) {
    html += `<span style="background:rgba(188,140,255,0.1);color:var(--purple);padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600">&#9900; ${esc(gs.branch)}</span>`;
  }
  if (gs.ahead > 0) {
    html += `<span style="color:var(--yellow);font-size:12px">&#8593; ${gs.ahead} ahead</span>`;
  }
  if (worktreePath) {
    html += `<span style="color:var(--text-dim);font-size:11px;font-family:monospace">${esc(worktreePath)}</span>`;
  }
  html += `</div>`;

  if (cleanMsg) {
    html += `<div style="padding:8px 0;font-size:13px">${cleanMsg}</div>`;
  }

  // Staged files
  if (gs.staged && gs.staged.length) {
    html += `<div class="detail-section"><h3 style="color:var(--green)">Staged (${gs.staged.length})</h3>`;
    html += `<div style="font-family:monospace;font-size:12px">`;
    for (const f of gs.staged) {
      html += `<div style="padding:2px 0;color:var(--green)">&#43; ${esc(f)}</div>`;
    }
    html += `</div></div>`;
  }

  // Unstaged modified
  if (gs.unstaged && gs.unstaged.length) {
    html += `<div class="detail-section"><h3 style="color:var(--yellow)">Modified / Unstaged (${gs.unstaged.length})</h3>`;
    html += `<div style="font-family:monospace;font-size:12px">`;
    for (const f of gs.unstaged) {
      html += `<div style="padding:2px 0;color:var(--yellow)">&#9998; ${esc(f)}</div>`;
    }
    html += `</div></div>`;
  }

  // Untracked
  if (gs.untracked && gs.untracked.length) {
    html += `<div class="detail-section"><h3 style="color:var(--text-dim)">Untracked (${gs.untracked.length})</h3>`;
    html += `<div style="font-family:monospace;font-size:12px">`;
    for (const f of gs.untracked) {
      html += `<div style="padding:2px 0;color:var(--text-dim)">? ${esc(f)}</div>`;
    }
    html += `</div></div>`;
  }

  // Raw output (collapsible)
  if (gs.raw && gs.raw.trim()) {
    html += `<div class="detail-section">
      <h3 style="cursor:pointer" onclick="this.nextElementSibling.classList.toggle('open');this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none'">
        Raw git status &#9660;
      </h3>
      <pre style="display:none">${esc(gs.raw)}</pre>
    </div>`;
  }

  // Command execution input
  if (taskId && worktreePath) {
    html += `<div class="detail-section" style="margin-top:16px;border-top:1px solid var(--border);padding-top:12px">
      <h3>Run Command</h3>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
        <input id="exec-cmd-input" type="text" placeholder="e.g. git log --oneline -10" value="git log --oneline -10"
          style="flex:1;padding:6px 10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-family:monospace;font-size:12px;margin:0"
          onkeydown="if(event.key==='Enter')execWorktreeCmd('${taskId}')">
        <button class="btn btn-sm" id="exec-cmd-btn" onclick="execWorktreeCmd('${taskId}')">Run</button>
      </div>
      <pre id="exec-cmd-output" style="background:var(--bg);padding:10px;border-radius:6px;font-size:11px;max-height:400px;overflow-y:auto;border:1px solid var(--border);display:none;white-space:pre-wrap"></pre>
    </div>`;
  }

  html += `</div>`;
  return html;
}

let _lastRefreshHash = '';
let _currentDetailTaskId = '';
async function refresh() {
  const t0 = performance.now();
  const [status, tasks] = await Promise.all([api('/api/status'), api('/api/tasks')]);
  const tFetch = performance.now();
  // Skip DOM rebuild if data unchanged (fast path for auto-refresh)
  const hash = JSON.stringify([
    status.status_counts,
    tasks.map(t => [
      t.id,
      t.status,
      t.updated_at,
      !!t.clean_available,
      !!t.actual_branch_exists,
      !!t.actual_worktree_exists,
    ]),
  ]);
  const tHash = performance.now();
  if (hash === _lastRefreshHash) {
    updateRefreshPerfLabel({
      skipped: true,
      total_ms: tHash - t0,
      fetch_ms: tFetch - t0,
      hash_ms: tHash - tFetch,
    });
    return;
  }
  _lastRefreshHash = hash;

  const sc = status.status_counts || {};
  document.getElementById('stats').innerHTML = `
    <div class="stat-card"><div class="num">${status.total_tasks||0}</div><div class="label">Total</div></div>
    <div class="stat-card"><div class="num" style="color:var(--text-dim)">${sc.pending||0}</div><div class="label">Pending</div></div>
    <div class="stat-card"><div class="num" style="color:var(--accent)">${(sc.planning||0)+(sc.coding||0)+(sc.reviewing||0)}</div><div class="label">Active</div></div>
    <div class="stat-card"><div class="num" style="color:var(--green)">${sc.completed||0}</div><div class="label">Completed</div></div>
    <div class="stat-card"><div class="num" style="color:#f0a040">${sc.needs_arbitration||0}</div><div class="label">Arbitration</div></div>
    <div class="stat-card"><div class="num" style="color:var(--red)">${(sc.failed||0)+(sc.review_failed||0)}</div><div class="label">Failed</div></div>
  `;
  const tStats = performance.now();

  // Build parent-child tree: roots first, children indented below their parent
  const byId = Object.fromEntries(tasks.map(t => [t.id, t]));
  window._taskById = byId;  // expose for detail view dep lookup
  const childrenOf = {};
  const roots = [];
  for (const t of tasks) {
    if (t.parent_id && byId[t.parent_id]) {
      (childrenOf[t.parent_id] = childrenOf[t.parent_id] || []).push(t);
    } else {
      roots.push(t);
    }
  }
  // Flatten into ordered list with depth info
  const ordered = [];
  function walk(list, depth) {
    for (const t of list) {
      ordered.push({t, depth});
      if (childrenOf[t.id]) walk(childrenOf[t.id], depth + 1);
    }
  }
  walk(roots, 0);
  const tTree = performance.now();

  const tbody = document.getElementById('task-list');
  const rowsHtml = ordered.map(({t, depth}) => {
    const nSes = countSessions(t.session_ids);
    const complexityBadge = t.complexity
      ? `<span style="font-size:10px;margin-left:4px;color:${
          t.complexity==='very_complex'?'var(--red)':
          t.complexity==='complex'?'var(--yellow)':
          t.complexity==='medium'?'var(--accent)':'var(--text-dim)'
        };border:1px solid currentColor;padding:1px 4px;border-radius:3px">${t.complexity.replace('_',' ')}</span>`
      : '';
    const isPublished = t.published_at > 0;
    const publishBtn = (t.status==='completed' && t.branch_name && t.task_mode !== 'review')
      ? `<button class="btn btn-sm" style="color:var(--green)" onclick="publishTask('${t.id}')">${isPublished ? '&#8635; Re-push' : '&#8593; Publish'}</button>`
      : '';
    const indent = depth > 0 ? `padding-left:${depth * 24}px` : '';
    const childIcon = depth > 0 ? '<span style="color:var(--text-dim);margin-right:4px">&#8627;</span>' : '';
    const childCount = (childrenOf[t.id] || []).length;
    const childBadge = childCount > 0
      ? `<span style="font-size:10px;margin-left:6px;color:var(--text-dim);border:1px solid var(--border);padding:1px 5px;border-radius:3px">${childCount} sub</span>`
      : '';
    const modeBadge = t.task_mode === 'review'
      ? '<span style="font-size:10px;margin-left:4px;color:var(--purple);border:1px solid var(--purple);padding:1px 4px;border-radius:3px">review</span>'
      : '';
    const deps = t.depends_on || [];
    // "blocked" badge: pending task with unresolved deps (at least one dep not completed)
    const isBlocked = t.status === 'pending' && deps.length > 0 && deps.some(depId => {
      const dep = byId[depId]; return !dep || dep.status !== 'completed';
    });
    const blockedBadge = isBlocked
      ? `<span title="Waiting for: ${deps.map(d=>d).join(', ')}" style="font-size:10px;margin-left:5px;color:var(--yellow);border:1px solid var(--yellow);padding:1px 5px;border-radius:3px">&#9203; blocked</span>`
      : '';
    const depsBadge = !isBlocked && deps.length > 0
      ? `<span style="font-size:10px;margin-left:5px;color:var(--text-dim);border:1px solid var(--border);padding:1px 5px;border-radius:3px">after ${deps.length}</span>`
      : '';
    const commentsBadge = t.has_comments
      ? `<span style="font-size:10px;margin-left:5px;color:var(--purple);border:1px solid var(--purple);padding:1px 5px;border-radius:3px">${t.comment_count || 0} comment${(t.comment_count || 0) === 1 ? '' : 's'}</span>`
      : '';
    return `<tr style="${depth > 0 ? 'background:rgba(88,166,255,0.02)' : ''}">
      <td><code style="font-size:${depth > 0 ? '10' : '12'}px">${t.id}</code></td>
      <td style="cursor:pointer;color:var(--accent);${indent}" onclick="showDetail('${t.id}')">${childIcon}${esc(t.title)}${modeBadge}${complexityBadge}${childBadge}${blockedBadge}${depsBadge}${commentsBadge}</td>
      <td><span class="badge badge-${t.status}">${t.status}</span></td>
      <td><span class="badge-${t.priority}">${t.priority}</span></td>
      <td>${t.source}</td>
      <td style="color:var(--text-dim)">${nSes > 0 ? nSes + ' session' + (nSes>1?'s':'') : '-'}</td>
      <td>${fmtTime(t.updated_at)}</td>
      <td style="white-space:nowrap">
        ${t.status==='pending'&&!isBlocked?`<button class="btn btn-sm" onclick="dispatch('${t.id}')">Run</button>`:''}
        ${publishBtn}
        ${!['completed','cancelled'].includes(t.status)?`<button class="btn btn-sm" onclick="cancel('${t.id}')">Cancel</button>`:''}
        ${t.clean_available?`<button class="btn btn-sm" style="color:var(--red)" onclick="cleanTask('${t.id}')">Clean</button>`:''}
      </td>
    </tr>`;
  }).join('');
  const tRows = performance.now();
  tbody.innerHTML = rowsHtml;
  const tDom = performance.now();
  updateRefreshPerfLabel({
    skipped: false,
    total_ms: tDom - t0,
    fetch_ms: tFetch - t0,
    hash_ms: tHash - tFetch,
    stats_ms: tStats - tHash,
    tree_ms: tTree - tStats,
    rows_ms: tRows - tTree,
    dom_ms: tDom - tRows,
  });
}

async function showDetail(id) {
  _currentDetailTaskId = id;
  const data = await api(`/api/tasks/${id}`);
  const t = data.task;
  document.getElementById('detail-title').textContent = t.title;

  // Tab bar
  let html = `<div class="tab-bar">
    <div class="tab active" onclick="switchTab(this, 'tab-overview')">Overview</div>
    <div class="tab" onclick="switchTab(this, 'tab-sessions')">Sessions</div>
    <div class="tab" onclick="switchTab(this, 'tab-runs')">Agent Runs (${(data.runs||[]).length})</div>
    <div class="tab" onclick="switchTab(this, 'tab-gitstatus')">Git Status</div>
    <div class="tab" onclick="switchTab(this, 'tab-output')">Outputs</div>
  </div>`;

  // ── Tab: Overview ──
  html += `<div class="tab-content active" id="tab-overview">`;
  const isPublished = t.published_at > 0;
  const modeColor = t.task_mode === 'review' ? 'var(--purple)' : 'var(--accent)';
  html += `<div class="detail-grid">
    <div class="detail-card"><h4>Status</h4><div class="val"><span class="badge badge-${t.status}">${t.status}</span></div></div>
    <div class="detail-card"><h4>Mode</h4><div class="val"><span style="color:${modeColor}">${t.task_mode||'develop'}</span></div></div>
    <div class="detail-card"><h4>Complexity</h4><div class="val"><span style="color:${
      t.complexity==='very_complex'?'var(--red)':
      t.complexity==='complex'?'var(--yellow)':
      t.complexity==='medium'?'var(--accent)':'var(--text-dim)'
    }">${t.complexity||'-'}</span></div></div>
    <div class="detail-card"><h4>Priority</h4><div class="val"><span class="badge-${t.priority}">${t.priority}</span></div></div>
    <div class="detail-card"><h4>Source</h4><div class="val">${t.source}${t.parent_id ? ` <span style="font-size:11px;color:var(--text-dim)">(sub-task of <code style="cursor:pointer;color:var(--accent)" onclick="showDetail(this.dataset.id)" data-id="${t.parent_id}">${t.parent_id}</code>)</span>` : ''}</div></div>
    <div class="detail-card"><h4>Retries</h4><div class="val">${t.retry_count} / ${t.max_retries}</div></div>
    ${(t.depends_on && t.depends_on.length) ? `<div class="detail-card" style="grid-column:1/-1"><h4>Depends On</h4><div class="val" style="display:flex;flex-wrap:wrap;gap:6px">${t.depends_on.map(depId => {
      const dep = (window._taskById||{})[depId];
      const depStatus = dep ? dep.status : '?';
      const statusColor = depStatus==='completed'?'var(--green)':depStatus==='failed'?'var(--red)':depStatus==='pending'?'var(--yellow)':'var(--accent)';
      return `<span style="font-size:12px"><code style="cursor:pointer;color:var(--accent)" onclick="showDetail('${depId}')">${depId}</code> <span class="badge badge-${depStatus}" style="font-size:10px">${depStatus}</span></span>`;
    }).join('')}</div></div>` : ''}
    <div class="detail-card"><h4>Branch</h4><div class="val"><code style="font-size:11px">${esc(t.branch_name||'-')}</code></div></div>
    <div class="detail-card"><h4>Worktree</h4><div class="val" style="font-size:11px">${esc(t.worktree_path||'-')}</div></div>
    <div class="detail-card"><h4>File</h4><div class="val">${esc(t.file_path||'-')}${t.line_number ? ':'+t.line_number : ''}</div></div>
    <div class="detail-card"><h4>Created</h4><div class="val">${fmtTime(t.created_at)}</div></div>
    <div class="detail-card"><h4>Started</h4><div class="val">${fmtTime(t.started_at)}</div></div>
    <div class="detail-card"><h4>Completed</h4><div class="val">${fmtTime(t.completed_at)}</div></div>
    <div class="detail-card"><h4>Published</h4><div class="val">${isPublished ? fmtTime(t.published_at) : '-'}</div></div>
  </div>`;
  if (t.status === 'completed' && t.branch_name && t.task_mode !== 'review') {
    html += `<div style="margin:12px 0">
      ${isPublished ? `<span style="color:var(--green);margin-right:8px">&#10003; Published</span>` : ''}
      <button class="btn" style="color:var(--green)" onclick="publishTask('${t.id}')">${isPublished ? '&#8635; Re-push to remote' : '&#8593; Publish branch to remote'}</button>
    </div>`;
  }
  if (t.clean_available) {
    html += `<div style="margin:8px 0">
      <button class="btn" style="color:var(--red);border-color:var(--red)" onclick="cleanTask('${t.id}')">&#128465; Clean up worktree &amp; branch</button>
      <span style="font-size:11px;color:var(--text-dim);margin-left:8px">Frees disk/git resources. Cannot be undone.</span>
    </div>`;
  }
  if (t.description) {
    html += `<div class="detail-section"><h3>Description</h3><pre>${esc(t.description)}</pre></div>`;
  }
  const commentsHeading = t.comment_count
    ? `Comments <span style="font-size:11px;color:var(--text-dim)">(${t.comment_count})</span>`
    : 'Comments';
  html += `<div class="detail-section" style="margin-top:16px;border:1px solid var(--border);border-radius:8px;padding:12px">
    <h3 style="margin-top:0">${commentsHeading}</h3>
    <div style="display:grid;grid-template-columns:minmax(140px,180px) 1fr auto;gap:8px;align-items:start;margin-bottom:12px">
      <input id="comment-username-${t.id}" type="text" placeholder="Your name" style="padding:6px 10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);margin:0">
      <textarea id="comment-content-${t.id}" placeholder="Add a comment to this task..." style="height:72px;font-size:13px;margin:0"></textarea>
      <button class="btn btn-primary" id="comment-btn-${t.id}" onclick="addTaskComment('${t.id}')">Add Comment</button>
    </div>
    <div id="task-comments-${t.id}">${renderTaskComments(t.comments || [])}</div>
  </div>`;
  if (t.review_input) {
    html += `<div class="detail-section"><h3 style="color:var(--purple)">Review Input</h3><pre style="font-size:12px;max-height:300px;overflow:auto">${esc(t.review_input)}</pre></div>`;
  }
  if (t.error) {
    html += `<div class="detail-section"><h3 style="color:var(--red)">Error</h3><pre style="color:var(--red)">${esc(t.error)}</pre></div>`;
  }
  // Arbitration section for needs_arbitration tasks
  if (t.status === 'needs_arbitration' && t.worktree_path) {
    html += `<div class="detail-section" style="margin-top:16px;border:1px solid #f0a040;border-radius:8px;padding:12px">
      <h3 style="margin-top:0;color:#f0a040">&#9888; Human Arbitration Required</h3>
      <p style="font-size:12px;color:var(--text-dim);margin-bottom:12px">The coder and reviewer could not reach agreement after all retry attempts. Review the code and reviewer feedback above, then choose an action:</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
        <button class="btn" style="color:var(--green);border-color:var(--green)" onclick="arbitrate('${t.id}','approve')">&#10003; Force Approve</button>
        <button class="btn" style="color:var(--red);border-color:var(--red)" onclick="arbitrate('${t.id}','reject')">&#10007; Reject (fail task)</button>
      </div>
      <p style="font-size:12px;color:var(--text-dim);margin-bottom:4px">Or provide feedback and revise (restarts the code\u2192review loop):</p>
      <textarea id="arbitrate-feedback-${t.id}" placeholder="Enter arbitration feedback for the coder..." style="width:100%;height:80px;font-size:13px;margin-bottom:8px"></textarea>
      <button class="btn btn-primary" onclick="arbitrate('${t.id}','revise')">Revise with Feedback</button>
    </div>`;
  }
  // Revise section for completed/failed tasks with a worktree
  if (['failed','review_failed'].includes(t.status) && t.worktree_path) {
    html += `<div class="detail-section" style="margin-top:16px;border:1px solid var(--yellow);border-radius:8px;padding:12px">
      <h3 style="margin-top:0">Resume Failed Run</h3>
      <p style="font-size:12px;color:var(--text-dim);margin-bottom:8px">Continue from the last coder session after timeout/interruption. Use <code>Continue</code> or provide a custom resume instruction.</p>
      <textarea id="resume-message-${t.id}" style="width:100%;height:80px;font-size:13px;margin-bottom:8px">Continue</textarea>
      <button class="btn" id="resume-btn-${t.id}" style="color:var(--yellow);border-color:var(--yellow)" onclick="resumeTask('${t.id}')">Resume Run</button>
    </div>`;
  }

  if (['completed','failed','review_failed'].includes(t.status) && t.worktree_path) {
    html += `<div class="detail-section" style="margin-top:16px;border:1px solid var(--border);border-radius:8px;padding:12px">
      <h3 style="margin-top:0">Revise Task</h3>
      <p style="font-size:12px;color:var(--text-dim);margin-bottom:8px">Provide additional review feedback. The coder will receive this feedback and re-enter the code\u2192review loop with retries reset.</p>
      <textarea id="revise-feedback-${t.id}" placeholder="Enter your review feedback / revision instructions..." style="width:100%;height:80px;font-size:13px;margin-bottom:8px"></textarea>
      <button class="btn btn-primary" id="revise-btn-${t.id}" onclick="reviseTask('${t.id}')">Revise</button>
    </div>`;
  }
  html += `</div>`;

  // ── Tab: Sessions ──
  html += `<div class="tab-content" id="tab-sessions">`;
  html += renderSessions(t.session_ids);
  // Also show per-run sessions
  if (data.runs && data.runs.length) {
    html += `<div class="detail-section" style="margin-top:12px"><h3>Per-Run Sessions</h3>`;
    for (const r of data.runs) {
      const sid = r.session_id || (r.parsed && r.parsed.session_id) || '';
      if (sid) {
        html += renderSessionBox(sid, `${r.agent_type} (${r.duration_sec.toFixed(1)}s)`);
      }
    }
    html += `</div>`;
  }
  html += `</div>`;

  // ── Tab: Agent Runs ──
  html += `<div class="tab-content" id="tab-runs">`;
  if (data.runs && data.runs.length) {
    for (let i = 0; i < data.runs.length; i++) {
      const r = data.runs[i];
      const sid = r.session_id || (r.parsed && r.parsed.session_id) || '';
      const exitColor = r.exit_code === 0 ? 'var(--green)' : 'var(--red)';
      const failStyle = r.exit_code !== 0 ? 'border-left:3px solid var(--red)' : '';
      html += `<div class="run-card" style="${failStyle}">
        <div class="run-header" onclick="toggleRun(this)">
          <div>
            <span class="run-title" style="color:${
              r.agent_type==='planner' ? 'var(--purple)' :
              r.agent_type==='coder' ? 'var(--accent)' :
              r.agent_type==='reviewer' ? 'var(--yellow)' :
              r.agent_type==='manual_review' ? '#f0883e' : 'var(--text)'
            }">${r.agent_type === 'manual_review' ? 'manual review' : r.agent_type}</span>
            <span class="run-meta" style="margin-left:8px">${r.model}</span>
            ${r.review_verdict ? `<span style="margin-left:8px;font-weight:bold;font-size:12px;padding:1px 8px;border-radius:3px;${
              r.review_verdict==='approve'
                ? 'color:var(--green);border:1px solid var(--green)'
                : 'color:var(--red);border:1px solid var(--red)'
            }">${r.review_verdict==='approve' ? 'APPROVE' : 'REQUEST_CHANGES'}</span>` : ''}
          </div>
          <div>
            <span style="color:${exitColor};font-size:12px">exit=${r.exit_code}</span>
            <span class="run-meta" style="margin-left:8px">${r.duration_sec.toFixed(1)}s</span>
            ${sid ? `<span class="run-meta" style="margin-left:8px">session: ${sid.substring(0,20)}...</span>` : ''}
          </div>
        </div>
        <div class="run-body" id="run-body-${i}">
          ${sid ? renderSessionBox(sid, r.agent_type + ' session') : ''}
          ${renderPromptSection(r.prompt, i)}
          ${r.agent_type === 'manual_review'
            ? `<pre style="font-size:12px;white-space:pre-wrap;word-break:break-word;color:var(--text)">${esc(r.output||'(no content)')}</pre>`
            : renderParsedRun(r.parsed)}
        </div>
      </div>`;
    }
  } else {
    html += '<span style="color:var(--text-dim)">No agent runs yet</span>';
  }
  html += `</div>`;

  // ── Tab: Git Status ──
  html += `<div class="tab-content" id="tab-gitstatus">`;
  html += renderGitStatus(data.git_status, t.worktree_path, t.branch_name, t.id);
  html += `</div>`;

  // ── Tab: Outputs ──
  html += `<div class="tab-content" id="tab-output">`;
  html += `<div class="detail-section"><h3>Plan Output</h3><pre>${esc(t.plan_output||'-')}</pre></div>`;
  html += `<div class="detail-section"><h3>Code Output</h3><pre>${esc(t.code_output||'-')}</pre></div>`;
  // Per-reviewer results
  if (t.reviewer_results && t.reviewer_results.length) {
    html += `<div class="detail-section"><h3>Review Results (${t.reviewer_results.length} reviewer${t.reviewer_results.length>1?'s':''})</h3>`;
    for (const rr of t.reviewer_results) {
      const verdictColor = rr.passed ? 'var(--green)' : 'var(--red)';
      const verdict = rr.passed ? 'APPROVE' : 'REQUEST_CHANGES';
      html += `<div style="margin-bottom:16px;border:1px solid ${verdictColor};border-radius:6px;overflow:hidden">
        <div style="background:rgba(0,0,0,0.3);padding:8px 12px;display:flex;align-items:center;gap:12px">
          <span style="color:${verdictColor};font-weight:bold">${verdict}</span>
          <span style="color:var(--text-dim);font-size:12px">${esc(rr.model)}</span>
        </div>
        <pre style="margin:0;padding:12px;border-radius:0;font-size:12px;max-height:500px;overflow-y:auto;white-space:pre-wrap;word-break:break-word">${esc(rr.output||'-')}</pre>
      </div>`;
    }
    html += `</div>`;
  } else {
    html += `<div class="detail-section"><h3>Review Output</h3><pre>${esc(t.review_output||'-')}</pre></div>`;
  }
  html += `</div>`;

  document.getElementById('detail-content').innerHTML = html;
  document.getElementById('detail-modal').classList.add('active');
}

function switchTab(el, tabId) {
  // Deactivate all tabs and content in this modal
  el.parentElement.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  const container = el.parentElement.parentElement;
  container.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  container.querySelector('#' + tabId).classList.add('active');
}

function toggleRun(header) {
  const body = header.nextElementSibling;
  body.classList.toggle('open');
  // Scroll to top of run body when opening
  if (body.classList.contains('open')) body.scrollTop = 0;
}

function toggleCategoryNote(noteId, trigger) {
  const el = document.getElementById(noteId);
  if (!el) return;
  const stored = _exploreCategoryNoteStore[noteId] || { preview: '', full: '' };
  const expanded = el.dataset.expanded === 'true';
  if (expanded) {
    el.innerHTML = esc(stored.preview || '');
    el.dataset.expanded = 'false';
    if (trigger) trigger.textContent = 'Show full';
    return;
  }
  el.innerHTML = esc(stored.full || '');
  el.dataset.expanded = 'true';
  if (trigger) trigger.textContent = 'Show less';
}

let _addTaskBaseBranch = '';

async function refreshAddTaskBaseBranch() {
  const nameEl = document.getElementById('add-task-base-branch-name');
  const refEl = document.getElementById('add-task-base-branch-ref');
  if (!nameEl || !refEl) return;
  if (_addTaskBaseBranch) {
    nameEl.textContent = _addTaskBaseBranch;
    refEl.textContent = `origin/${_addTaskBaseBranch}`;
    return;
  }
  try {
    const cfg = await api('/api/config');
    const baseBranch = cfg && cfg.base_branch ? cfg.base_branch : 'master';
    _addTaskBaseBranch = baseBranch;
    nameEl.textContent = baseBranch;
    refEl.textContent = `origin/${baseBranch}`;
  } catch (e) {
    nameEl.textContent = 'master';
    refEl.textContent = 'origin/master';
  }
}

function renderExploreInitState() {
  const panel = document.getElementById('explore-init-state');
  if (!panel) return;
  const repoLabel = document.getElementById('explore-repo-label');

  const status = _exploreStatus || _emptyExploreStatus();
  const init = status.map_init || {};
  const st = init.status || 'idle';
  const stColor = st === 'done' ? 'var(--green)' : st === 'in_progress' ? 'var(--yellow)' : st === 'failed' ? 'var(--red)' : 'var(--text-dim)';
  const startedTxt = init.started_at ? new Date(init.started_at * 1000).toLocaleString() : '-';
  const finishedTxt = init.finished_at ? new Date(init.finished_at * 1000).toLocaleString() : '-';
  const output = (init.readable_output || init.output || '').trim();

  if (repoLabel) {
    const rn = status.repo_name || '-';
    const rp = status.repo_path || '';
    repoLabel.innerHTML = `Repo: <strong>${esc(rn)}</strong>${rp ? ` <span style="font-size:11px">(${esc(rp)})</span>` : ''}`;
  }

  panel.innerHTML = `
    <div class="explore-queue-header" style="margin-bottom:4px">
      <div>
        <strong>Map Initialization</strong>
        <span style="font-size:11px;color:${stColor};margin-left:8px">${esc(st)}</span>
      </div>
      <button class="btn btn-sm" style="color:var(--red)" onclick="cancelInitExploreMap()" ${st === 'in_progress' ? '' : 'disabled'}>Cancel Init</button>
    </div>
    <div style="font-size:11px;color:var(--text-dim)">
      started: ${esc(startedTxt)} · finished: ${esc(finishedTxt)} · modules: ${init.modules_created || 0}
      ${init.session_id ? ` · session: <code>${esc(init.session_id)}</code>` : ''}
      ${init.model ? ` · model: <code>${esc(init.model)}</code>` : ''}
    </div>
    ${init.map_review_required ? `<div style="margin-top:6px;font-size:11px;color:var(--yellow)">Map review requested${init.map_review_reason ? `: ${esc(init.map_review_reason)}` : ''}</div>` : ''}
    ${init.error ? `<div style="margin-top:6px;font-size:11px;color:var(--red)">${esc(init.error)}</div>` : ''}
    ${output ? `<details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px;color:var(--text-dim)">Live init output</summary><div class="explore-init-log">${esc(output)}</div></details>` : ''}
  `;

  const initBtn = document.getElementById('explore-init-btn');
  if (initBtn) {
    initBtn.disabled = st === 'in_progress';
    initBtn.innerHTML = st === 'in_progress' ? '&#9881; Initializing...' : '&#9881; Initialize Map';
  }

  const startBtn = document.getElementById('explore-start-btn');
  if (startBtn) {
    startBtn.disabled = !status.map_ready || st === 'in_progress';
  }
}

function showAddTask() {
  document.getElementById('add-modal').classList.add('active');
  refreshAddTaskBaseBranch();
}
function closeModals() {
  document.querySelectorAll('.modal-overlay').forEach(m => m.classList.remove('active'));
  _currentDetailTaskId = '';
}

function uiAlert(message, title = 'Notice') {
  return new Promise((resolve) => {
    const overlay = document.getElementById('dialog-modal');
    const box = document.getElementById('dialog-box');
    const titleEl = document.getElementById('dialog-title');
    const msgEl = document.getElementById('dialog-message');
    const okBtn = document.getElementById('dialog-ok-btn');
    const cancelBtn = document.getElementById('dialog-cancel-btn');

    box.classList.remove('modal-danger');
    titleEl.textContent = title;
    msgEl.textContent = String(message || '');
    cancelBtn.style.display = 'none';

    const cleanup = () => {
      okBtn.onclick = null;
      overlay.classList.remove('active');
      resolve();
    };
    okBtn.onclick = cleanup;
    overlay.classList.add('active');
  });
}

function uiConfirm(message, title = 'Please Confirm') {
  return new Promise((resolve) => {
    const overlay = document.getElementById('dialog-modal');
    const box = document.getElementById('dialog-box');
    const titleEl = document.getElementById('dialog-title');
    const msgEl = document.getElementById('dialog-message');
    const okBtn = document.getElementById('dialog-ok-btn');
    const cancelBtn = document.getElementById('dialog-cancel-btn');

    box.classList.add('modal-danger');
    titleEl.textContent = title;
    msgEl.textContent = String(message || '');
    cancelBtn.style.display = '';

    const finish = (val) => {
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      overlay.classList.remove('active');
      resolve(val);
    };
    okBtn.onclick = () => finish(true);
    cancelBtn.onclick = () => finish(false);
    overlay.classList.add('active');
  });
}

async function execWorktreeCmd(taskId) {
  const input = document.getElementById('exec-cmd-input');
  const btn = document.getElementById('exec-cmd-btn');
  const output = document.getElementById('exec-cmd-output');
  const cmd = input.value.trim();
  if (!cmd) return;
  btn.disabled = true; btn.textContent = 'Running...';
  output.style.display = 'block';
  output.textContent = '$ ' + cmd + '\\n...';
  try {
    const res = await api(`/api/tasks/${taskId}/exec`, {method:'POST', body: JSON.stringify({command: cmd})});
    if (res.error) {
      output.innerHTML = `<span style="color:var(--text-dim)">$ ${esc(cmd)}</span>\n<span style="color:var(--red)">${esc(res.error)}</span>`;
    } else {
      let text = `<span style="color:var(--text-dim)">$ ${esc(cmd)}</span>  <span style="color:${res.exit_code===0?'var(--green)':'var(--red)'}">exit=${res.exit_code}</span>\n`;
      if (res.stdout) text += esc(res.stdout);
      if (res.stderr) text += `<span style="color:var(--red)">${esc(res.stderr)}</span>`;
      if (!res.stdout && !res.stderr) text += '<span style="color:var(--text-dim)">(no output)</span>';
      output.innerHTML = text;
    }
  } catch(e) {
    output.innerHTML = `<span style="color:var(--red)">Request failed: ${esc(String(e))}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Run';
  }
}

function initOverlayClose() {
  const detail = document.getElementById('detail-modal');
  if (detail) {
    detail.addEventListener('click', (e) => {
      if (e.target === detail) {
        closeModals();
      }
    });
  }
}

async function addTask() {
  const title = document.getElementById('task-title').value.trim();
  if (!title) return;
  await api('/api/tasks', { method:'POST', body: JSON.stringify({
    title,
    description: document.getElementById('task-desc').value,
    priority: document.getElementById('task-priority').value,
    copy_files: document.getElementById('task-copy-files').value,
  })});
  closeModals(); refresh();
}

async function addReviewTask() {
  const reviewInput = document.getElementById('review-task-input').value.trim();
  if (!reviewInput) return;
  await api('/api/tasks/review', { method:'POST', body: JSON.stringify({
    title: document.getElementById('review-task-title').value.trim(),
    review_input: reviewInput,
    priority: document.getElementById('review-task-priority').value,
    copy_files: document.getElementById('review-task-copy-files').value,
  })});
  closeModals(); refresh();
}

function switchAddTab(el, tabId) {
  const modal = el.closest('.modal');
  modal.querySelectorAll('#add-task-tabs .tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  modal.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  modal.querySelector('#' + tabId).classList.add('active');
}

async function dispatchAll() { await api('/api/dispatch-all', {method:'POST'}); refresh(); }
async function dispatch(id) { await api(`/api/tasks/${id}/dispatch`, {method:'POST'}); refresh(); }
async function cancel(id) {
  const res = await api(`/api/tasks/${id}/cancel`, {method:'POST'});
  if (res && res.error) { await uiAlert(res.error, 'Cancel failed'); return; }
  refresh();
}

async function cleanTask(id) {
  const confirmed = await uiConfirm(
    `Delete the git branch and worktree directory for task ${id.slice(0,8)}? The task record is kept. This cannot be undone.`,
    'Clean up worktree & branch'
  );
  if (!confirmed) return;
  const res = await api(`/api/tasks/${id}/clean`, {method:'POST'});
  if (res && res.error) { await uiAlert(res.error, 'Clean failed'); return; }
  const msg = res.branch
    ? `Branch "${res.branch}" and its worktree have been removed.`
    : 'Worktree resources have been removed.';
  await uiAlert(msg, 'Cleaned');
  await refresh();
  const detailOpen = document.getElementById('detail-modal')?.classList.contains('active');
  if (detailOpen && _currentDetailTaskId === id) {
    await showDetail(id);
  }
}

async function publishTask(id) {
  const btn = event && event.target;
  if (btn) { btn.disabled = true; btn.textContent = 'Publishing...'; }
  try {
    const res = await api(`/api/tasks/${id}/publish`, {method:'POST'});
    if (res.success) {
      await uiAlert(`Branch "${res.branch}" pushed to remote "${res.remote}" successfully.\n\nYou can now open a PR from this branch.`, 'Publish Succeeded');
      refresh();
    } else {
      await uiAlert(`Publish failed:\n${res.message || res.error}`, 'Publish Failed');
    }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#8593; Publish'; }
  }
}

async function reviseTask(id) {
  const textarea = document.getElementById('revise-feedback-' + id);
  const feedback = textarea ? textarea.value.trim() : '';
  if (!feedback) { await uiAlert('Please enter review feedback.'); return; }
  const btn = document.getElementById('revise-btn-' + id);
  if (btn) { btn.disabled = true; btn.textContent = 'Submitting...'; }
  try {
    const res = await api(`/api/tasks/${id}/revise`, {method:'POST', body: JSON.stringify({feedback})});
    if (res.error) {
      await uiAlert('Revise failed: ' + res.error);
    } else {
      closeModals(); refresh();
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Revise'; }
  }
}

async function resumeTask(id) {
  const textarea = document.getElementById('resume-message-' + id);
  const message = textarea ? textarea.value.trim() : '';
  const btn = document.getElementById('resume-btn-' + id);
  if (btn) { btn.disabled = true; btn.textContent = 'Resuming...'; }
  try {
    const res = await api(`/api/tasks/${id}/resume`, {
      method:'POST',
      body: JSON.stringify({message: message || 'Continue'}),
    });
    if (res.error) {
      await uiAlert('Resume failed: ' + res.error);
    } else {
      closeModals();
      refresh();
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Resume Run'; }
  }
}

async function addTaskComment(id) {
  const userEl = document.getElementById(`comment-username-${id}`);
  const contentEl = document.getElementById(`comment-content-${id}`);
  const btn = document.getElementById(`comment-btn-${id}`);
  const username = (userEl && userEl.value || '').trim();
  const content = (contentEl && contentEl.value || '').trim();
  if (!username) {
    await uiAlert('Please enter your name before posting a comment.', 'Comment Required');
    return;
  }
  if (!content) {
    await uiAlert('Please enter comment content before posting.', 'Comment Required');
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Posting...';
  }
  try {
    const res = await api(`/api/tasks/${id}/comments`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ username, content }),
    });
    if (res.error) {
      await uiAlert(String(res.error), 'Add Comment');
      return;
    }
    if (contentEl) contentEl.value = '';
    await showDetail(id);
    await refresh();
  } catch (e) {
    await uiAlert('Failed to add comment: ' + e, 'Add Comment');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Add Comment';
    }
  }
}

async function arbitrate(id, action) {
  let feedback = '';
  if (action === 'revise') {
    const textarea = document.getElementById('arbitrate-feedback-' + id);
    feedback = textarea ? textarea.value.trim() : '';
    if (!feedback) { await uiAlert('Please enter feedback for the coder.'); return; }
  }
  if (action === 'approve') {
    const ok = await uiConfirm('Force-approve the coder\\'s current work, overriding reviewer objections?', 'Force Approve');
    if (!ok) return;
  }
  if (action === 'reject') {
    const ok = await uiConfirm('Permanently fail this task?', 'Reject Task');
    if (!ok) return;
  }
  const res = await api(`/api/tasks/${id}/arbitrate`, {method:'POST', body: JSON.stringify({action, feedback})});
  if (res && res.error) { await uiAlert('Arbitration failed: ' + res.error); return; }
  closeModals(); refresh();
}

// ── Main tab switching ──
function switchMainTab(el, panelId) {
  document.getElementById('main-tab-bar').querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  ['main-tasks','main-todos','main-explore','main-sysinfo'].forEach(id => {
    document.getElementById(id).style.display = (id === panelId) ? '' : 'none';
  });
  if (panelId === 'main-todos') loadTodos();
  if (panelId === 'main-explore') {
    loadExploreModules();
    loadExploreQueue();
  }
  if (panelId === 'main-sysinfo') loadSysInfo();
}

function showSysToast(msg, ok) {
  const el = document.getElementById('sysinfo-toast');
  if (!el) return;
  el.textContent = msg;
  el.style.color = ok ? 'var(--green)' : 'var(--red)';
  el.style.display = 'inline';
  setTimeout(() => { el.style.display = 'none'; }, 3500);
}

function buildModelSelect(id, models, current, extraAttr) {
  // Build <select> options; if current not in list, prepend it so it's always selectable
  const all = models.includes(current) ? models : (current ? [current, ...models] : models);
  let opts = all.map(m =>
    `<option value="${esc(m)}"${m === current ? ' selected' : ''}>${esc(m)}</option>`
  ).join('');
  return `<select class="sys-select" id="${id}" ${extraAttr||''}>${opts}</select>`;
}

function addReviewerRow(models, value) {
  const container = document.getElementById('sys-reviewer-list');
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:6px;align-items:center;margin-bottom:6px';
  const all = (value && !models.includes(value)) ? [value, ...models] : models;
  const opts = all.map(m =>
    `<option value="${esc(m)}"${m === value ? ' selected' : ''}>${esc(m)}</option>`
  ).join('');
  row.innerHTML = `<select class="sys-select sys-reviewer-select" style="flex:1">${opts}</select>
    <button class="btn btn-sm" style="color:var(--red);padding:2px 7px;flex-shrink:0" onclick="this.parentElement.remove()" title="Remove">&times;</button>`;
  container.appendChild(row);
}

async function saveSysModels() {
  const btn = document.getElementById('sys-save-btn');
  btn.disabled = true; btn.textContent = 'Saving...';
  try {
    const cmap = {};
    document.querySelectorAll('[data-complexity]').forEach(el => {
      cmap[el.dataset.complexity] = el.value;
    });
    const reviewerModels = [];
    document.querySelectorAll('.sys-reviewer-select').forEach(el => {
      if (el.value) reviewerModels.push(el.value);
    });
    const payload = {
      planner_model: document.getElementById('sys-planner-model').value,
      explorer_model: document.getElementById('sys-explorer-model').value,
      map_model: document.getElementById('sys-map-model').value,
      coder_model_default: document.getElementById('sys-coder-default').value,
      coder_model_by_complexity: cmap,
      reviewer_models: reviewerModels,
    };
    const res = await api('/api/config', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      showSysToast('✓ Models updated successfully', true);
      // Reload from server to confirm stored values are in sync
      loadSysInfo();
    } else {
      showSysToast('✗ Failed: ' + (res.error || 'unknown error'), false);
      btn.disabled = false; btn.textContent = 'Save Model Changes';
    }
  } catch(e) {
    showSysToast('✗ Error: ' + e.message, false);
    btn.disabled = false; btn.textContent = 'Save Model Changes';
  }
}

async function loadSysInfo() {
  const content = document.getElementById('sysinfo-content');
  content.innerHTML = `<span style="color:var(--text-dim);font-size:12px">Loading...</span>`;

  // Fetch config (source of truth) and model list in parallel
  const [cfg, modelsResp] = await Promise.all([
    api('/api/config'),
    api('/api/models'),
  ]);

  if (cfg.error) {
    content.innerHTML = `<span style="color:var(--red)">${esc(cfg.error)}</span>`;
    return;
  }
  _addTaskBaseBranch = cfg.base_branch || '';
  const models = (modelsResp && modelsResp.models) ? modelsResp.models : [];

  const complexityColors = {
    very_complex: 'var(--red)', complex: 'var(--yellow)',
    medium: 'var(--accent)', simple: 'var(--text-dim)'
  };

  let html = `<div class="detail-grid">`;
  html += `<div class="detail-card" style="grid-column:span 2">
    <h4>Repository</h4>
    <div class="val" style="font-size:12px;word-break:break-all">${esc(cfg.repo_path)}</div>
    <div style="font-size:11px;color:var(--text-dim);margin-top:4px">base branch: <code>${esc(cfg.base_branch)}</code></div>
  </div>`;
  html += `<div class="detail-card" style="grid-column:span 2">
    <h4>Worktree Directory</h4>
    <div class="val" style="font-size:12px;word-break:break-all">${esc(cfg.worktree_dir)}</div>
  </div>`;
  html += `<div class="detail-card">
    <h4>Planner Model</h4>
    ${buildModelSelect('sys-planner-model', models, cfg.planner_model)}
  </div>`;
  html += `<div class="detail-card">
    <h4>Explorer Model</h4>
    ${buildModelSelect('sys-explorer-model', models, cfg.explorer_model)}
  </div>`;
  html += `<div class="detail-card">
    <h4>Map Model</h4>
    ${buildModelSelect('sys-map-model', models, cfg.map_model)}
  </div>`;
  html += `<div class="detail-card">
    <h4>Default Coder Model</h4>
    ${buildModelSelect('sys-coder-default', models, cfg.coder_model_default)}
  </div>`;
  html += `</div>`;

  html += `<div class="detail-section"><h3>Coder Model by Complexity</h3>`;
  html += `<table style="width:100%;border-collapse:collapse">`;
  for (const [level, model] of Object.entries(cfg.coder_model_by_complexity || {})) {
    const color = complexityColors[level] || 'var(--text)';
    html += `<tr>
      <td style="padding:5px 12px 5px 0;font-size:12px;white-space:nowrap;width:1%">
        <span style="color:${color};border:1px solid ${color};padding:1px 7px;border-radius:3px">${esc(level.replace('_',' '))}</span>
      </td>
      <td style="padding:5px 0">${buildModelSelect('', models, model, `data-complexity="${esc(level)}"`)}</td>
    </tr>`;
  }
  html += `</table></div>`;

  html += `<div class="detail-section"><h3>Reviewer Models <span style="font-size:11px;color:var(--text-dim)">(all must approve)</span></h3>`;
  html += `<div id="sys-reviewer-list">`;
  if (cfg.reviewer_models && cfg.reviewer_models.length) {
    for (const m of cfg.reviewer_models) {
      const all = models.includes(m) ? models : [m, ...models];
      const opts = all.map(x =>
        `<option value="${esc(x)}"${x === m ? ' selected' : ''}>${esc(x)}</option>`
      ).join('');
      html += `<div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
        <select class="sys-select sys-reviewer-select" style="flex:1">${opts}</select>
        <button class="btn btn-sm" style="color:var(--red);padding:2px 7px;flex-shrink:0" onclick="this.parentElement.remove()" title="Remove">&times;</button>
      </div>`;
    }
  }
  html += `</div>`;
  // Store models list on a hidden element so addReviewerRow can access it
  html += `<button class="btn btn-sm" style="margin-top:6px;color:var(--accent)"
    onclick="addReviewerRow(_sysModels,'')">+ Add reviewer</button>`;
  html += `</div>`;

  html += `<div style="margin:16px 0;display:flex;align-items:center;gap:14px">
    <button class="btn" id="sys-save-btn" onclick="saveSysModels()" style="color:var(--green);font-weight:600">Save Model Changes</button>
    <span id="sysinfo-toast" style="display:none;font-size:12px;font-weight:bold"></span>
  </div>`;

  html += `<div class="detail-section"><h3>Worktree Hooks <span style="font-size:11px;color:var(--text-dim)">(run after worktree creation, in order)</span></h3>`;
  if (cfg.worktree_hooks && cfg.worktree_hooks.length) {
    html += `<ol style="margin:0;padding-left:20px">`;
    for (const h of cfg.worktree_hooks) {
      html += `<li style="font-family:monospace;font-size:12px;margin-bottom:4px">${esc(h)}</li>`;
    }
    html += `</ol>`;
  } else {
    html += `<span style="color:var(--text-dim);font-size:12px">No hooks configured</span>`;
  }
  html += `</div>`;

  html += `<div class="detail-section"><h3>Publish Remote</h3>`;
  html += `<code style="font-size:12px">${esc(cfg.publish_remote)}</code></div>`;

  html += `<div class="detail-section"><h3>Execution</h3>`;
  html += `<div style="font-size:12px;color:var(--text-dim)">Max retries per task: <code>${esc(String(cfg.max_retries ?? '-'))}</code></div></div>`;

  content.innerHTML = html;
  // Expose model list for the dynamic "Add reviewer" button
  window._sysModels = models;
}

function showTodosPanel() {
  const tab = document.querySelector('#main-tab-bar .tab:nth-child(2)');
  switchMainTab(tab, 'main-todos');
}

// ── Score bar rendering ──
// invertColor: if true, low score is green (e.g. difficulty: lower = better)
function scoreBar(score, invertColor) {
  if (score < 0) return '<span style="color:var(--text-dim)">-</span>';
  const pct = Math.round(score * 10);
  let color;
  if (invertColor) {
    color = score <= 3 ? 'var(--green)' : score <= 6 ? 'var(--yellow)' : 'var(--red)';
  } else {
    color = score >= 7 ? 'var(--green)' : score >= 4 ? 'var(--yellow)' : 'var(--red)';
  }
  return `<div style="display:flex;align-items:center;gap:6px">
    <div style="flex:1;height:6px;background:rgba(255,255,255,0.1);border-radius:3px">
      <div style="width:${pct}%;height:100%;background:${color};border-radius:3px"></div>
    </div>
    <span style="font-size:11px;color:${color};width:24px">${score.toFixed(1)}</span>
  </div>`;
}

// ── Load and render TODOs ──
async function loadTodos() {
  const items = await api('/api/todos');
  const tbody = document.getElementById('todo-list');
  const badge = document.getElementById('todo-badge');
  const active = items.filter(i => i.status !== 'deleted');
  badge.textContent = active.length ? `(${active.length})` : '';

  if (!active.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--text-dim);padding:32px">
      No TODOs yet. Click "Scan TODOs" to scan the repository.
    </td></tr>`;
    updateQueueBanner(items);
    return;
  }

  tbody.innerHTML = active.map(item => {
    const isAnalyzing = item.status === 'analyzing';
    const statusColor = {
      pending_analysis: 'var(--text-dim)',
      analyzing:        'var(--yellow)',
      analyzed:         'var(--accent)',
      dispatched:       'var(--green)',
    }[item.status] || 'var(--text-dim)';
    const statusLabel = isAnalyzing
      ? `<span class="spinner"></span><span style="color:var(--yellow);font-size:11px">analyzing</span>`
      : item.status === 'dispatched'
        ? `<span style="color:var(--green);font-size:11px">&#10003; sent</span>`
        : `<span style="font-size:11px;color:${statusColor}">${item.status.replace(/_/g,' ')}</span>`;
    const relFile = item.file_path.split('/').slice(-3).join('/');
    const canAnalyze = item.status === 'pending_analysis' || item.status === 'analyzed';
    const analyzeBtn = isAnalyzing
      ? `<button class="btn btn-sm" disabled style="color:var(--text-dim)"><span class="spinner"></span></button>`
      : canAnalyze
        ? `<button class="btn btn-sm" onclick="analyzeSingle('${item.id}')">Analyze</button>`
        : '';
    const disableCheck = isAnalyzing;
    return `<tr id="todo-row-${item.id}">
      <td><input type="checkbox" class="todo-check" data-id="${item.id}" ${disableCheck?'disabled':''}></td>
      <td style="font-family:monospace;font-size:11px" title="${esc(item.file_path)}">${esc(relFile)}:${item.line_number}</td>
      <td style="font-size:12px">${esc(item.description)}</td>
      <td>${scoreBar(item.feasibility_score, false)}</td>
      <td>${scoreBar(item.difficulty_score, true)}</td>
      <td style="font-size:11px;color:var(--text-dim)">${esc(item.analysis_note)||'<span style="color:var(--text-dim)">-</span>'}</td>
      <td>${statusLabel}</td>
      <td>${analyzeBtn}</td>
    </tr>`;
  }).join('');

  updateQueueBanner(items);
}

// ── Persistent queue banner (shown when any item is in ANALYZING state) ──
function updateQueueBanner(items) {
  const analyzing = items.filter(i => i.status === 'analyzing');
  const banner = document.getElementById('analyze-queue-banner');
  const list = document.getElementById('analyze-queue-items');
  if (!analyzing.length) { banner.style.display = 'none'; return; }
  banner.style.display = '';
  list.innerHTML = analyzing.map(i => {
    const relFile = i.file_path.split('/').slice(-3).join('/');
    return `<div class="analyze-queue-item">
      <span style="color:var(--text-dim);font-size:10px">[${i.id}]</span>
      <span>${esc(relFile)}:${i.line_number}</span>
      <span style="color:var(--text-dim)">—</span>
      <span style="color:var(--text)">${esc(i.description.slice(0,60))}</span>
    </div>`;
  }).join('');
}

function showScanModal() {
  document.getElementById('scan-result').textContent = '';
  document.getElementById('scan-modal').classList.add('active');
}

async function doScanTodos() {
  const btn = document.getElementById('scan-submit-btn');
  const subdir = document.getElementById('scan-subdir').value.trim();
  const limit = parseInt(document.getElementById('scan-limit').value, 10) || 0;
  const resultEl = document.getElementById('scan-result');
  btn.disabled = true; btn.textContent = 'Scanning...';
  resultEl.textContent = '';
  try {
    const res = await api('/api/todos/scan', {method:'POST', body: JSON.stringify({subdir, limit})});
    await loadTodos();
    if (res.scanned === 0) {
      resultEl.style.color = 'var(--text-dim)';
      resultEl.textContent = 'No new TODOs found (duplicates skipped).';
    } else {
      resultEl.style.color = 'var(--green)';
      resultEl.textContent = `Found ${res.scanned} new TODO item(s). Switch to the TODOs tab to review.`;
    }
  } catch(e) {
    resultEl.style.color = 'var(--red)';
    resultEl.textContent = 'Scan failed: ' + e;
  } finally {
    btn.disabled = false; btn.textContent = 'Scan';
  }
}

function getCheckedTodoIds() {
  return Array.from(document.querySelectorAll('.todo-check:checked')).map(cb => cb.dataset.id);
}

function selectAllTodos() {
  document.querySelectorAll('.todo-check:not(:disabled)').forEach(cb => cb.checked = true);
}
function selectNoneTodos() {
  document.querySelectorAll('.todo-check').forEach(cb => cb.checked = false);
}
function toggleAllTodos(master) {
  document.querySelectorAll('.todo-check:not(:disabled)').forEach(cb => cb.checked = master.checked);
}

// ── Analyze single from row button ──
async function analyzeSingle(todoId) {
  await runAnalyzeModal([todoId]);
}

// ── Analyze selected (batch) ──
async function analyzeSelected() {
  const ids = getCheckedTodoIds();
  if (!ids.length) { await uiAlert('Select at least one TODO to analyze.', 'Nothing Selected'); return; }
  await runAnalyzeModal(ids);
}

// ── Core: open progress modal and run analysis for a list of todo IDs ──
async function runAnalyzeModal(ids) {
  // Fetch current items to get descriptions
  const allItems = await api('/api/todos');
  const byId = Object.fromEntries(allItems.map(i => [i.id, i]));

  // Build initial modal rows (all waiting)
  const progList = document.getElementById('analyze-prog-list');
  const summary = document.getElementById('analyze-prog-summary');
  summary.textContent = '';

  function makeRow(id, status, feasibility, difficulty, note, output) {
    const item = byId[id] || {};
    const relFile = (item.file_path || '').split('/').slice(-2).join('/');
    const desc = esc((item.description || id).slice(0, 60));
    const statusClass = `prog-status-${status}`;
    const statusIcon = {
      waiting: '&#8230;',
      running: '<span class="spinner"></span>',
      done: '&#10003;',
      skipped: '&#10227;',
      error: '&#10007;',
    }[status] || '';
    const feasHtml = feasibility >= 0 ? scoreBar(feasibility, false) : '<span style="color:var(--text-dim)">-</span>';
    const diffHtml = difficulty >= 0 ? scoreBar(difficulty, true) : '<span style="color:var(--text-dim)">-</span>';
    const noteHtml = note ? `<span title="${esc(note)}">${esc(note.slice(0,80))}</span>` : '<span style="color:var(--text-dim)">-</span>';
    const outputToggle = output
      ? `<button class="btn btn-sm" style="font-size:10px;padding:2px 6px" onclick="toggleOutput('out-${id}')">&#8897; Output</button>
         <div id="out-${id}" class="prog-output" style="display:none;grid-column:span 5">${esc(output)}</div>`
      : '';
    return `<div class="prog-row" id="prow-${id}">
      <div style="overflow:hidden">
        <div style="font-family:monospace;font-size:11px;color:var(--text-dim)">${esc(relFile)}</div>
        <div>${desc}</div>
      </div>
      <div>${feasHtml}</div>
      <div>${diffHtml}</div>
      <div style="font-size:11px">${noteHtml}</div>
      <div class="${statusClass}">${statusIcon} ${status}</div>
    </div>${outputToggle ? '<div style="padding:0 8px 8px;grid-column:span 5">' + outputToggle + '</div>' : ''}`;
  }

  progList.innerHTML = ids.map(id => makeRow(id, 'waiting', -1, -1, '', '')).join('');

  document.getElementById('analyze-progress-modal').classList.add('active');

  let doneCount = 0, errorCount = 0, skippedCount = 0;

  for (const id of ids) {
    // Update row to "running"
    const row = document.getElementById(`prow-${id}`);
    if (row) row.outerHTML = makeRow(id, 'running', -1, -1, '', '');

    let result;
    try {
      const resp = await fetch(`/api/todos/${id}/analyze`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
      });
      result = await resp.json();
      if (!resp.ok) {
        const errMsg = result.error || `HTTP ${resp.status}`;
        if (resp.status === 409) {
          const newRow = document.getElementById(`prow-${id}`);
          if (newRow) newRow.outerHTML = makeRow(id, 'skipped', -1, -1, errMsg, '');
          skippedCount++;
        } else {
          const newRow = document.getElementById(`prow-${id}`);
          if (newRow) newRow.outerHTML = makeRow(id, 'error', -1, -1, errMsg, '');
          errorCount++;
        }
        continue;
      }
    } catch(e) {
      const newRow = document.getElementById(`prow-${id}`);
      if (newRow) newRow.outerHTML = makeRow(id, 'error', -1, -1, String(e), '');
      errorCount++;
      continue;
    }

    // Update byId with fresh data
    byId[id] = result;
    const newRow = document.getElementById(`prow-${id}`);
    if (newRow) newRow.outerHTML = makeRow(
      id, 'done',
      result.feasibility_score ?? -1,
      result.difficulty_score ?? -1,
      result.analysis_note || '',
      result.analyze_output || '',
    );
    doneCount++;
  }

  summary.innerHTML = `Done: <span style="color:var(--green)">${doneCount}</span>` +
    (skippedCount ? `&nbsp; Skipped (already running): <span style="color:var(--text-dim)">${skippedCount}</span>` : '') +
    (errorCount ? `&nbsp; Errors: <span style="color:var(--red)">${errorCount}</span>` : '');

  await loadTodos();
}

function toggleOutput(elemId) {
  const el = document.getElementById(elemId);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function closeAnalyzeModal() {
  document.getElementById('analyze-progress-modal').classList.remove('active');
}

async function dispatchSelected() {
  const ids = getCheckedTodoIds();
  if (!ids.length) { await uiAlert('Select at least one TODO to send to the planner.', 'Nothing Selected'); return; }
  const res = await api('/api/todos/dispatch', {method:'POST', body: JSON.stringify({ids})});
  await loadTodos();
  refresh();
  await uiAlert(`Sent ${res.dispatched} task(s) to planner.`, 'Dispatch Complete');
}

async function revertSelected() {
  const ids = getCheckedTodoIds();
  if (!ids.length) { await uiAlert('Select at least one dispatched TODO to revert.', 'Nothing Selected'); return; }
  const res = await api('/api/todos/revert', {method:'POST', body: JSON.stringify({ids})});
  await loadTodos();
  await uiAlert(`Reverted ${res.reverted} TODO item(s) back to analyzed.`, 'Revert Complete');
}

async function deleteSelected() {
  const ids = getCheckedTodoIds();
  if (!ids.length) { await uiAlert('Select at least one TODO to delete.', 'Nothing Selected'); return; }
  if (!await uiConfirm(`Delete ${ids.length} TODO item(s)?`, 'Confirm Deletion')) return;
  await api('/api/todos/delete', {method:'POST', body: JSON.stringify({ids})});
  await loadTodos();
}

// ── Explore System ──────────────────────────────────────────────────

let _exploreModules = [];
let _selectedModuleId = null;
let _exploreCategories = [];
let _exploreQueue = {
  running: [],
  queued: [],
  counts: { running: 0, queued: 0, total: 0 },
  max_parallel_runs: 1,
};
let _exploreStatus = {
  repo_name: '',
  repo_path: '',
  map_ready: false,
  map_init: {
    status: 'idle',
    started_at: 0,
    finished_at: 0,
    updated_at: 0,
    session_id: '',
    model: '',
    output: '',
    readable_output: '',
    error: '',
    cancel_requested: false,
    modules_created: 0,
  },
};
const _catColors = {
  performance: '#f85149',
  concurrency: '#d29922',
  error_handling: '#f0883e',
  maintainability: '#58a6ff',
  security: '#bc8cff',
};

function catColor(cat) { return _catColors[cat] || 'var(--text-dim)'; }

function statusDotColor(status) {
  if (status === 'done') return 'var(--green)';
  if (status === 'in_progress') return 'var(--yellow)';
  if (status === 'stale') return 'var(--orange)';
  return 'rgba(255,255,255,0.15)';
}

function _emptyExploreQueueState() {
  return {
    running: [],
    queued: [],
    counts: { running: 0, queued: 0, total: 0 },
    max_parallel_runs: 1,
  };
}

function _emptyExploreStatus() {
  return {
    repo_name: '',
    repo_path: '',
    map_ready: false,
    map_init: {
      status: 'idle',
      started_at: 0,
      finished_at: 0,
      updated_at: 0,
      session_id: '',
      model: '',
      output: '',
      readable_output: '',
      error: '',
      cancel_requested: false,
      modules_created: 0,
    },
  };
}

function _isExploreTabActive() {
  const panel = document.getElementById('main-explore');
  return panel && panel.style.display !== 'none';
}

function getSelectedExploreCategories() {
  return Array.from(
    document.querySelectorAll('#explore-category-filters input[type="checkbox"]:checked')
  ).map(x => x.value);
}

function getSelectedExploreModules() {
  const sel = document.getElementById('explore-module-select');
  if (!sel) return [];
  return Array.from(sel.selectedOptions || []).map(o => o.value).filter(Boolean);
}

function getModuleDetailSelectedCategories(moduleId) {
  return Array.from(
    document.querySelectorAll(`.exp-module-cat-check[data-module-id="${moduleId}"]:checked`)
  ).map(x => x.value);
}

function getDoneExploreSelections(moduleIds, categories) {
  const sourceModules = Array.isArray(moduleIds) && moduleIds.length
    ? (_exploreModules || []).filter(m => moduleIds.includes(m.id))
    : (_exploreModules || []).filter(m => !(m.children && m.children.length));
  const out = [];
  for (const mod of sourceModules) {
    const catStatus = mod.category_status || {};
    for (const cat of (categories || [])) {
      if (catStatus[cat] === 'done') {
        out.push({ moduleId: mod.id, moduleName: mod.name, category: cat });
      }
    }
  }
  return out;
}

async function confirmDoneCategoryReplay(doneSelections) {
  if (!doneSelections.length) return true;
  const preview = doneSelections
    .slice(0, 6)
    .map(x => `${x.moduleName}: ${x.category.replace(/_/g, ' ')}`)
    .join('\\n');
  const more = doneSelections.length > 6 ? `\\n...and ${doneSelections.length - 6} more` : '';
  return await uiConfirm(
    `Some selected categories are already marked done. This will start a new exploration run for them and carry forward the prior summary/context.\\n\\n${preview}${more}\\n\\nContinue?`,
    'Re-explore Done Categories'
  );
}

function getExploreFocusPoint() {
  const el = document.getElementById('explore-focus-point');
  return el ? el.value.trim() : '';
}

function renderExploreFilters() {
  const moduleSelect = document.getElementById('explore-module-select');
  if (moduleSelect) {
    moduleSelect.innerHTML = '';
    for (const m of _exploreModules) {
      const indent = '&nbsp;'.repeat((m.depth || 0) * 2);
      moduleSelect.innerHTML += `<option value="${m.id}">${indent}${esc(m.name)} (${esc(m.path)})</option>`;
    }
  }

  const catWrap = document.getElementById('explore-category-filters');
  if (catWrap) {
    if (!_exploreCategories.length) {
      catWrap.innerHTML = '<span style="color:var(--text-dim);font-size:12px">No categories configured</span>';
    } else {
      catWrap.innerHTML = _exploreCategories.map(cat => `
        <label class="explore-cat-chip" style="border-color:${catColor(cat)}66">
          <input type="checkbox" value="${cat}" checked>
          <span style="color:${catColor(cat)}">${cat.replace(/_/g, ' ')}</span>
        </label>
      `).join('');
    }
  }
}

function renderExploreQueue() {
  const panel = document.getElementById('explore-queue');
  if (!panel) return;

  const queueState = _exploreQueue || _emptyExploreQueueState();
  const counts = queueState.counts || { running: 0, queued: 0, total: 0 };
  const running = queueState.running || [];
  const queued = queueState.queued || [];
  const rows = [...running, ...queued];

  let html = `<div class="explore-queue-header">
    <div>
      <strong>Exploration Queue</strong>
      <span style="font-size:11px;color:var(--text-dim);margin-left:8px">parallel=${queueState.max_parallel_runs || 1} running=${counts.running} queued=${counts.queued}</span>
    </div>
    <button class="btn btn-sm" style="color:var(--red)" onclick="cancelAllExploration()">Cancel All</button>
  </div>`;

  if (!rows.length) {
    html += '<span style="color:var(--text-dim);font-size:12px">No exploration jobs in progress.</span>';
    panel.innerHTML = html;
    return;
  }

  html += '<div class="explore-queue-list">';
  for (const j of rows) {
    const state = j.state || 'queued';
    const when = state === 'running' ? (j.started_at || 0) : (j.queued_at || 0);
    const whenTxt = when ? new Date(when * 1000).toLocaleTimeString() : '-';
    html += `<div class="explore-queue-item ${state}">
      <div style="min-width:0">
        <div><span style="color:${catColor(j.category)}">${(j.category || '').replace(/_/g, ' ')}</span> · ${esc(j.module_name || j.module_id || '')}</div>
        <div class="meta">${esc(j.module_path || '')} · ${state} @ ${whenTxt}</div>
        ${j.focus_point ? `<div class="meta">focus: ${esc(j.focus_point)}</div>` : ''}
      </div>
      <button class="btn btn-sm" style="color:var(--red)" onclick="cancelExploreJob('${j.module_id}','${j.category}')">Cancel</button>
    </div>`;
  }
  html += '</div>';
  panel.innerHTML = html;
}

async function loadExploreQueue() {
  try {
    const [queue, status] = await Promise.all([
      api('/api/explore/queue'),
      api('/api/explore/status'),
    ]);
    _exploreQueue = queue || _emptyExploreQueueState();
    _exploreStatus = status || _emptyExploreStatus();
  } catch(e) {
    _exploreQueue = _emptyExploreQueueState();
    _exploreStatus = _emptyExploreStatus();
  }
  renderExploreInitState();
  renderExploreQueue();
}

async function loadExploreModules() {
  try {
    const [mods, status, queue] = await Promise.all([
      api('/api/explore/modules'),
      api('/api/explore/status'),
      api('/api/explore/queue'),
    ]);
    _exploreModules = Array.isArray(mods) ? mods : [];
    _exploreStatus = status || _emptyExploreStatus();
    _exploreCategories = (status && Array.isArray(status.categories)) ? status.categories : [];
    _exploreQueue = queue || _emptyExploreQueueState();
  } catch(e) {
    _exploreModules = [];
    _exploreStatus = _emptyExploreStatus();
    _exploreCategories = [];
    _exploreQueue = _emptyExploreQueueState();
  }
  renderExploreFilters();
  renderExploreInitState();
  renderExploreQueue();
  renderExploreTree();
  if (_selectedModuleId) showModuleDetail(_selectedModuleId);
}

function renderExploreTree() {
  const tree = document.getElementById('explore-tree');
  if (!_exploreModules.length) {
    tree.innerHTML = '<span style="color:var(--text-dim);font-size:12px">No modules yet. Click "Initialize Map" to scan the repository.</span>';
    return;
  }
  const byParent = {};
  for (const m of _exploreModules) {
    const pid = m.parent_id || '';
    (byParent[pid] = byParent[pid] || []).push(m);
  }

  function renderNode(mod) {
    const children = byParent[mod.id] || [];
    const hasChildren = children.length > 0;
    const catStatus = mod.category_status || {};
    const dots = Object.entries(catStatus).map(([cat, st]) =>
      `<span class="exp-cat-dot" style="background:${statusDotColor(st)};border-color:${catColor(cat)}" title="${cat}: ${st}"></span>`
    ).join('');
    const sel = mod.id === _selectedModuleId ? ' selected' : '';
    let html = `<div class="exp-node" data-id="${mod.id}">
      <div class="exp-node-inner${sel}" onclick="selectModule('${mod.id}')">
        <span class="exp-toggle" onclick="event.stopPropagation();toggleExpNode(this)">${hasChildren ? '&#9660;' : '&nbsp;'}</span>
        <span class="exp-name">${esc(mod.name)}</span>
        <span class="exp-path" title="${esc(mod.path)}">${esc(mod.path)}</span>
        <span class="exp-cats">${dots}</span>
      </div>`;
    if (hasChildren) {
      html += `<div class="exp-children">`;
      for (const child of children) html += renderNode(child);
      html += `</div>`;
    }
    html += `</div>`;
    return html;
  }

  const roots = byParent[''] || byParent[undefined] || [];
  tree.innerHTML = roots.map(r => renderNode(r)).join('');
}

function toggleExpNode(toggleEl) {
  const node = toggleEl.closest('.exp-node');
  const children = node.querySelector('.exp-children');
  if (!children) return;
  const collapsed = children.style.display === 'none';
  children.style.display = collapsed ? '' : 'none';
  toggleEl.innerHTML = collapsed ? '&#9660;' : '&#9654;';
}

function selectModule(moduleId) {
  _selectedModuleId = moduleId;
  // Update selected state in tree
  document.querySelectorAll('.exp-node-inner').forEach(el => el.classList.remove('selected'));
  const node = document.querySelector(`.exp-node[data-id="${moduleId}"] > .exp-node-inner`);
  if (node) node.classList.add('selected');
  showModuleDetail(moduleId);
}

async function showModuleDetail(moduleId) {
  const panel = document.getElementById('explore-detail');
  panel.innerHTML = '<span style="color:var(--text-dim);font-size:12px">Loading...</span>';
  try {
    const data = await api(`/api/explore/modules/${moduleId}`);
    const m = data.module;
    const runs = data.runs || [];

    let html = `<div style="margin-bottom:12px">
      <h3 style="font-size:16px;font-weight:600;margin-bottom:4px">${esc(m.name)}</h3>
      <div style="font-family:monospace;font-size:12px;color:var(--text-dim);margin-bottom:6px">${esc(m.path)}</div>
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px">${esc(m.description || 'No description')}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
        <button class="btn btn-sm" style="color:var(--green)" onclick="exploreModule('${m.id}')">&#9654; Explore Selected Categories</button>
        <button class="btn btn-sm" style="color:var(--red)" onclick="cancelModuleExploration('${m.id}')">&#10005; Cancel Selected Categories</button>
        <button class="btn btn-sm" style="color:var(--red)" onclick="deleteModule('${m.id}')">&#128465; Delete</button>
        <button class="btn btn-sm" onclick="resetModuleStatuses('${m.id}')">&#8634; Reset</button>
      </div>
    </div>`;

    // Category grid
    const catStatus = m.category_status || {};
    const catNotes = m.category_notes || {};
    _exploreCategoryNoteStore = {};
    html += `<div style="margin-bottom:16px"><h4 style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Categories</h4>`;
    html += `<div class="exp-detail-cats">`;
    for (const [cat, st] of Object.entries(catStatus)) {
      const note = catNotes[cat] || '';
      const stColor = statusDotColor(st);
      const stBg = st === 'done' ? 'rgba(63,185,80,0.1)' : st === 'in_progress' ? 'rgba(210,153,34,0.1)' : 'transparent';
      const noteId = `cat-note-${m.id}-${cat}`;
      const notePreviewRaw = note.length > 120 ? `${note.slice(0,120)}...` : note;
      const expandable = note.length > 120;
      _exploreCategoryNoteStore[noteId] = {
        preview: notePreviewRaw,
        full: note,
      };
      html += `<div class="exp-cat-row" style="border-left:3px solid ${catColor(cat)};background:${stBg}">
        <input type="checkbox" class="exp-module-cat-check" data-module-id="${m.id}" value="${cat}" ${st !== 'done' ? 'checked' : ''}>
        <div class="exp-cat-body">
          <div class="exp-cat-head">
            <span class="exp-cat-label" style="color:${catColor(cat)}">${cat.replace(/_/g,' ')}</span>
            <span class="exp-cat-status" style="color:${stColor}">${st}</span>
          </div>
          ${note
            ? `<div class="exp-cat-note" id="${noteId}" data-expanded="false">${esc(expandable ? notePreviewRaw : note)}</div>${expandable ? `<span class="exp-cat-note-toggle" onclick="toggleCategoryNote('${noteId}', this)">Show full</span>` : ''}`
            : `<div class="exp-cat-note">-</div>`}
        </div>
      </div>`;
    }
    html += `</div></div>`;

    // Runs & findings
    if (runs.length) {
      html += `<h4 style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Exploration Runs (${runs.length})</h4>`;
      for (let i = 0; i < runs.length; i++) {
        const run = runs[i];
        const findings = run.findings || [];
        const sid = run.session_id || (run.parsed && run.parsed.session_id) || '';
        const exitColor = run.exit_code === 0 ? 'var(--green)' : 'var(--red)';
        const failStyle = run.exit_code !== 0 ? 'border-left:3px solid var(--red)' : '';
        const completionColor = run.completion_status === 'complete' ? 'var(--green)' : 'var(--orange)';
        html += `<div class="run-card" style="${failStyle}">
          <div class="run-header" onclick="toggleRun(this)">
            <div>
              <span style="font-weight:600;color:${catColor(run.category)}">${run.category.replace(/_/g,' ')}</span>
              <span style="font-size:11px;color:var(--text-dim);margin-left:8px">${esc(run.personality)}</span>
              <span style="font-size:11px;color:var(--text-dim);margin-left:8px">${fmtTime(run.created_at)}</span>
              <span style="font-size:11px;color:var(--text-dim);margin-left:8px">${run.duration_sec.toFixed(1)}s</span>
            </div>
            <div>
              <span style="font-size:12px;color:${completionColor}">${esc(run.completion_status || 'complete')}</span>
              <span style="color:${exitColor};font-size:12px;margin-left:8px">exit=${run.exit_code}</span>
              <span style="font-size:12px;color:${findings.length ? 'var(--yellow)' : 'var(--green)'};margin-left:8px">${findings.length} finding${findings.length !== 1 ? 's' : ''}</span>
              ${sid ? `<span class="run-meta" style="margin-left:8px">session: ${esc(sid.substring(0,20))}...</span>` : ''}
            </div>
          </div>
          <div class="run-body" id="explore-run-body-${i}">`;
        if (sid) {
          html += renderSessionBox(sid, `${run.category} explorer session`);
        }
        html += renderPromptSection(run.prompt, `explore-${run.id}`);
        if (run.summary) {
          html += `<div style="font-size:12px;color:var(--text-dim);margin-bottom:10px;padding:8px;background:var(--bg);border-radius:6px">${esc(run.summary)}</div>`;
        }
        html += `<div style="font-size:11px;color:var(--text-dim);margin-bottom:8px">
          ${run.focus_point ? `focus: ${esc(run.focus_point)} · ` : ''}
          actionability: ${run.actionability_score >= 0 ? Number(run.actionability_score).toFixed(1) : '-'} / 10 ·
          reliability: ${run.reliability_score >= 0 ? Number(run.reliability_score).toFixed(1) : '-'} / 10
        </div>`;
        if (run.explored_scope) {
          html += `<div style="font-size:11px;color:var(--text-dim);margin-bottom:8px">explored: ${esc(run.explored_scope)}</div>`;
        }
        html += `<div style="font-size:11px;color:${completionColor};margin-bottom:8px">completion: ${esc(run.completion_status || 'complete')}</div>`;
        if (run.supplemental_note) {
          html += `<div style="font-size:11px;color:var(--text-dim);margin-bottom:8px">note: ${esc(run.supplemental_note)}</div>`;
        }
        if (run.map_review_required) {
          html += `<div style="font-size:11px;color:var(--yellow);margin-bottom:8px">map review requested${run.map_review_reason ? `: ${esc(run.map_review_reason)}` : ''}</div>`;
        }
        html += `<details style="margin-bottom:10px"><summary style="cursor:pointer;font-size:12px;color:var(--text-dim)">Session transcript</summary><div style="margin-top:8px">${renderParsedRun(run.parsed)}</div></details>`;
        for (let fi = 0; fi < findings.length; fi++) {
          const f = findings[fi];
          html += `<div class="exp-finding" style="border-left:3px solid">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px">
              <div class="exp-finding-title sev-${f.severity}">[${f.severity}] ${esc(f.title)}</div>
              <button class="btn btn-sm" style="font-size:10px;flex-shrink:0" onclick="createTaskFromFinding('${run.id}',${fi})">&#8594; Task</button>
            </div>
            <div class="exp-finding-desc">${esc(f.description)}</div>
            <div class="exp-finding-meta">${esc(f.file_path)}${f.line_number ? ':' + f.line_number : ''}</div>
            ${f.suggested_fix ? `<div style="font-size:11px;color:var(--accent);margin-top:4px"><b>Fix:</b> ${esc(f.suggested_fix)}</div>` : ''}
          </div>`;
        }
        html += `</div></div>`;
      }
    }

    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = `<span style="color:var(--red)">${esc(String(e))}</span>`;
  }
}

async function initExploreMap() {
  const st = (_exploreStatus && _exploreStatus.map_init && _exploreStatus.map_init.status) || 'idle';
  if (st === 'in_progress') {
    await uiAlert('Map initialization is already running.', 'Initialize Explore Map');
    return;
  }
  if (!await uiConfirm('This will delete all explorer map/modules/runs/queue state and rebuild the map. Existing generated tasks will be kept. Continue?', 'Reinitialize Explore Map')) return;
  try {
    const res = await api('/api/explore/init-map', {method:'POST'});
    if (res && res.accepted === false) {
      await uiAlert(res.error || 'Map initialization is already running.', 'Initialize Explore Map');
    } else {
      await uiAlert('Map initialization started.', 'Map Init Started');
    }
  } catch(e) {
    await uiAlert('Init failed: ' + e, 'Error');
  }
  await loadExploreModules();
}

async function cancelInitExploreMap() {
  try {
    const init = (_exploreStatus && _exploreStatus.map_init) || {};
    if ((init.status || '') !== 'in_progress') {
      return;
    }
    if (!await uiConfirm('Cancel current map initialization?', 'Cancel Map Initialization')) return;
    await api('/api/explore/init-map/cancel', {method:'POST'});
  } catch(e) {
    await uiAlert('Cancel init failed: ' + e, 'Error');
  }
  await loadExploreModules();
}

async function startExploration() {
  const btn = document.getElementById('explore-start-btn');
  const init = (_exploreStatus && _exploreStatus.map_init) || {};
  if (!_exploreStatus.map_ready || init.status === 'in_progress') {
    await uiAlert('Map is not ready yet. Please finish Initialize Map first.', 'Map Not Ready');
    return;
  }
  btn.disabled = true; btn.textContent = 'Starting...';
  try {
    const moduleIds = getSelectedExploreModules();
    const categories = getSelectedExploreCategories();
    const focusPoint = getExploreFocusPoint();
    if (!categories.length) {
      await uiAlert('Select at least one category before starting exploration.', 'No Category Selected');
      return;
    }
    const doneSelections = getDoneExploreSelections(moduleIds, categories);
    if (!await confirmDoneCategoryReplay(doneSelections)) return;
    const scopeText = moduleIds.length ? `${moduleIds.length} selected module(s)` : 'all matching modules';
    if (!await uiConfirm(`Start exploration for ${scopeText} across categories: ${categories.join(', ')}?`, 'Start Exploration')) return;
    const payload = { categories };
    if (moduleIds.length) payload.module_ids = moduleIds;
    if (focusPoint) payload.focus_point = focusPoint;
    const res = await api('/api/explore/start', {method:'POST', body: JSON.stringify(payload)});
    if (res && res.error) {
      await uiAlert(String(res.error), 'Start Exploration');
      return;
    }
    const extra = [];
    if (res.rejected_in_progress) extra.push(`rejected(in_progress): ${res.rejected_in_progress}`);
    if (res.skipped_non_todo) extra.push(`skipped(non-todo): ${res.skipped_non_todo}`);
    if (Array.isArray(res.invalid_categories) && res.invalid_categories.length) {
      extra.push(`invalid: ${res.invalid_categories.join(', ')}`);
    }
    const summary = document.getElementById('explore-start-summary');
    if (summary) summary.textContent = extra.join(' · ');
    if (res.started > 0) {
      const focusText = (res.focus_point || focusPoint) ? ` Focus: ${(res.focus_point || focusPoint)}` : '';
      await uiAlert(`Started ${res.started} run(s): running=${res.running || 0}, queued=${res.queue?.counts?.queued || 0}.${focusText}`, 'Exploration Started');
    } else {
      await uiAlert('No TODO categories matched the current filters.', 'Nothing to Explore');
    }
  } catch(e) {
    await uiAlert('Start failed: ' + e, 'Error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '&#9654; Start Exploration';
    renderExploreInitState();
  }
  await loadExploreModules();
  if (_selectedModuleId) showModuleDetail(_selectedModuleId);
}

async function exploreModule(moduleId) {
  try {
    const init = (_exploreStatus && _exploreStatus.map_init) || {};
    if (!_exploreStatus.map_ready || init.status === 'in_progress') {
      await uiAlert('Map is not ready yet. Please finish Initialize Map first.', 'Map Not Ready');
      return;
    }
    const categories = getModuleDetailSelectedCategories(moduleId);
    const focusPoint = getExploreFocusPoint();
    if (!categories.length) {
      await uiAlert('Select at least one category in the module detail first.', 'No Category Selected');
      return;
    }
    const doneSelections = getDoneExploreSelections([moduleId], categories);
    if (!await confirmDoneCategoryReplay(doneSelections)) return;
    if (!await uiConfirm(`Start exploration for this module across categories: ${categories.join(', ')}?`, 'Start Exploration')) return;
    const payload = {module_ids: [moduleId], categories};
    if (focusPoint) payload.focus_point = focusPoint;
    const res = await api('/api/explore/start', {
      method:'POST',
      body: JSON.stringify(payload),
    });
    if (res && res.error) {
      await uiAlert(String(res.error), 'Start Exploration');
      return;
    }
    await uiAlert(`Started ${res.started} run(s) for this module.`, 'Exploration Started');
    await loadExploreModules();
    showModuleDetail(moduleId);
  } catch(e) {
    await uiAlert('Explore failed: ' + e, 'Error');
  }
}

async function cancelAllExploration() {
  if (!await uiConfirm('Cancel all queued/running exploration jobs and reset in-progress states?', 'Cancel All Exploration')) return;
  await cancelExplorationRequest({}, 'Cancel All Exploration');
}

async function cancelExplorationByFilters() {
  const moduleIds = getSelectedExploreModules();
  const categories = getSelectedExploreCategories();
  if (!categories.length) {
    await uiAlert('Select at least one category before cancelling.', 'No Category Selected');
    return;
  }
  const payload = { categories };
  if (moduleIds.length) payload.module_ids = moduleIds;
  await cancelExplorationRequest(payload, 'Cancel Exploration');
}

async function cancelModuleExploration(moduleId) {
  const categories = getModuleDetailSelectedCategories(moduleId);
  if (!categories.length) {
    await uiAlert('Select at least one category in module detail before cancelling.', 'No Category Selected');
    return;
  }
  await cancelExplorationRequest({ module_ids: [moduleId], categories }, 'Cancel Module Exploration');
  showModuleDetail(moduleId);
}

async function cancelExploreJob(moduleId, category) {
  await cancelExplorationRequest({ module_ids: [moduleId], categories: [category] }, 'Cancel Exploration Job');
  if (_selectedModuleId === moduleId) showModuleDetail(moduleId);
}

async function cancelExplorationRequest(payload, title) {
  try {
    const res = await api('/api/explore/cancel', {
      method:'POST',
      body: JSON.stringify(payload || {}),
    });
    const summary = document.getElementById('explore-start-summary');
    if (summary) {
      summary.textContent = `cancelled=${res.cancelled || 0} (running=${res.cancelled_running || 0}, queued=${res.cancelled_queued || 0}, stale=${res.reset_stale || 0})`;
    }
    await uiAlert(`Cancelled ${res.cancelled || 0} exploration item(s).`, title || 'Exploration Cancelled');
  } catch(e) {
    await uiAlert('Cancel failed: ' + e, 'Error');
  }
  await loadExploreModules();
}

async function deleteModule(moduleId) {
  if (!await uiConfirm('Delete this module and all its children?', 'Delete Module')) return;
  try {
    await fetch(`/api/explore/modules/${moduleId}`, {method:'DELETE'});
    _selectedModuleId = null;
    document.getElementById('explore-detail').innerHTML = '<span style="color:var(--text-dim);font-size:12px">Module deleted.</span>';
    await loadExploreModules();
  } catch(e) {
    await uiAlert('Delete failed: ' + e, 'Error');
  }
}

async function resetModuleStatuses(moduleId) {
  if (!await uiConfirm('Reset all category statuses to TODO for this module?', 'Reset Module')) return;
  try {
    const mod = _exploreModules.find(m => m.id === moduleId);
    if (!mod) return;
    const resetStatus = {};
    for (const cat of Object.keys(mod.category_status || {})) resetStatus[cat] = 'todo';
    await api(`/api/explore/modules/${moduleId}/update`, {method:'POST', body: JSON.stringify({category_status: resetStatus})});
    await loadExploreModules();
    showModuleDetail(moduleId);
  } catch(e) {
    await uiAlert('Reset failed: ' + e, 'Error');
  }
}

async function createTaskFromFinding(runId, findingIndex) {
  try {
    const res = await api(`/api/explore/runs/${runId}/create-task`, {
      method:'POST', body: JSON.stringify({finding_index: findingIndex})
    });
    if (res.error) { await uiAlert(res.error, 'Error'); return; }
    await uiAlert(`Task created: ${res.title}`, 'Task Created');
    refresh();
  } catch(e) {
    await uiAlert('Failed: ' + e, 'Error');
  }
}

function showAddModuleModal() {
  // Populate parent select
  const sel = document.getElementById('add-mod-parent');
  sel.innerHTML = '<option value="">(root level)</option>';
  for (const m of _exploreModules) {
    const indent = '  '.repeat(m.depth || 0);
    sel.innerHTML += `<option value="${m.id}">${indent}${esc(m.name)} (${esc(m.path)})</option>`;
  }
  document.getElementById('add-mod-name').value = '';
  document.getElementById('add-mod-path').value = '';
  document.getElementById('add-mod-desc').value = '';
  document.getElementById('add-module-modal').classList.add('active');
}

async function doAddModule() {
  const name = document.getElementById('add-mod-name').value.trim();
  const path = document.getElementById('add-mod-path').value.trim();
  const desc = document.getElementById('add-mod-desc').value.trim();
  const parentId = document.getElementById('add-mod-parent').value;
  if (!name) { await uiAlert('Name is required.'); return; }
  try {
    const res = await api('/api/explore/modules', {method:'POST', body: JSON.stringify({
      name, path, description: desc, parent_id: parentId
    })});
    if (res.error) { await uiAlert(res.error, 'Error'); return; }
    closeModals();
    await loadExploreModules();
  } catch(e) {
    await uiAlert('Add failed: ' + e, 'Error');
  }
}

initOverlayClose();
refresh();
setInterval(refresh, 5000);
setInterval(() => {
  if (_isExploreTabActive()) loadExploreQueue();
}, 3000);
</script>
</body>
</html>"""
