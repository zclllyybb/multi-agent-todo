#!/usr/bin/env python3
"""CLI entry point for the multi-agent TODO resolver."""

import argparse
import json
import os
import sys
from urllib import error, request

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import load_config
from core.orchestrator import Orchestrator
from core.database import Database
import daemon as daemon_mod


def cmd_start(args):
    """Start the daemon (orchestrator + web dashboard)."""
    daemon_mod.start(config_path=args.config, foreground=args.foreground)


def cmd_stop(args):
    """Stop the daemon."""
    daemon_mod.stop(config_path=args.config)


def cmd_status(args):
    """Show daemon and system status."""
    daemon_mod.status(config_path=args.config)
    config = load_config(args.config)
    db = Database(config["database"]["path"])
    tasks = db.get_all_tasks()
    counts = {}
    for t in tasks:
        s = t.status.value
        counts[s] = counts.get(s, 0) + 1
    print(f"\nTasks: {len(tasks)} total")
    for s, c in sorted(counts.items()):
        print(f"  {s}: {c}")


def cmd_add(args):
    """Submit a task (planner will analyze and optionally split it)."""
    config = load_config(args.config)
    host = config["web"]["host"]
    if host == "0.0.0.0":
        host = "127.0.0.1"
    url = f"http://{host}:{config['web']['port']}/api/tasks"
    payload = json.dumps(
        {
            "title": args.title,
            "description": args.description or "",
            "priority": args.priority,
        }
    ).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            task = json.loads(response.read().decode("utf-8"))
    except error.URLError as exc:
        print(f"Could not reach daemon API at {url}: {exc}")
        print("Start the daemon first with `cli.py start`.")
        return
    print(f"Submitted task: [{task['id']}] {task['title']}")
    print(
        "(The planner will decide whether to split this into sub-tasks during execution.)"
    )


def cmd_scan(args):
    """Scan repository for TODOs and store them for review."""
    config = load_config(args.config)
    orch = Orchestrator(config)
    new_items = orch.scan_todos_raw(limit=args.limit)
    print(
        f"Found {len(new_items)} new TODO items (use 'todos' command or web UI to review them)"
    )


def cmd_todos(args):
    """List, analyze, or dispatch TODO items."""
    config = load_config(args.config)
    orch = Orchestrator(config)

    if args.action == "list":
        items = orch.db.get_all_todo_items()
        if args.json:
            print(json.dumps([i.to_dict() for i in items], indent=2))
            return
        if not items:
            print("No TODO items found. Run 'scan' first.")
            return
        print(
            f"{'ID':<14} {'Status':<20} {'Rel':>5} {'Feas':>5}  {'File:Line':<40} {'Description'}"
        )
        print("-" * 120)
        for i in items:
            rel = f"{i.relevance_score:.1f}" if i.relevance_score >= 0 else "-"
            feas = f"{i.feasibility_score:.1f}" if i.feasibility_score >= 0 else "-"
            loc = f"{i.file_path.split('/')[-1]}:{i.line_number}"
            print(
                f"{i.id:<14} {i.status.value:<20} {rel:>5} {feas:>5}  {loc:<40} {i.description[:60]}"
            )

    elif args.action == "analyze":
        ids = args.ids
        if not ids:
            # analyze all pending
            items = orch.db.get_all_todo_items()
            ids = [i.id for i in items if i.status.value == "pending_analysis"]
        for tid in ids:
            print(f"Analyzing {tid}...")
            result = orch.analyze_todo_item(tid)
            if "error" in result:
                print(f"  ERROR: {result['error']}")
            else:
                print(
                    f"  relevance={result['relevance_score']:.1f} "
                    f"feasibility={result['feasibility_score']:.1f}  "
                    f"note: {result['analysis_note']}"
                )

    elif args.action == "dispatch":
        ids = args.ids
        if not ids:
            print(
                "Specify TODO IDs to dispatch, or use the web UI for batch selection."
            )
            return
        tasks = orch.dispatch_todos_to_planner(ids)
        print(f"Dispatched {len(tasks)} task(s):")
        for t in tasks:
            print(f"  [{t['id']}] {t['title']}")

    elif args.action == "delete":
        ids = args.ids
        if not ids:
            print("Specify TODO IDs to delete.")
            return
        count = orch.delete_todo_items(ids)
        print(f"Deleted {count} item(s).")


def cmd_list(args):
    """List all tasks."""
    config = load_config(args.config)
    db = Database(config["database"]["path"])
    tasks = db.get_all_tasks()
    if args.status:
        tasks = [t for t in tasks if t.status.value == args.status]
    tasks.sort(key=lambda t: t.created_at, reverse=True)

    if args.json:
        print(json.dumps([t.to_dict() for t in tasks], indent=2))
        return

    if not tasks:
        print("No tasks found.")
        return
    print(f"{'ID':<14} {'Status':<15} {'Priority':<10} {'Title'}")
    print("-" * 80)
    for t in tasks:
        print(f"{t.id:<14} {t.status.value:<15} {t.priority.value:<10} {t.title[:50]}")


def cmd_show(args):
    """Show task details."""
    config = load_config(args.config)
    db = Database(config["database"]["path"])
    task = db.get_task(args.task_id)
    if not task:
        print(f"Task not found: {args.task_id}")
        return
    runs = db.get_runs_for_task(args.task_id)
    if args.json:
        print(
            json.dumps(
                {"task": task.to_dict(), "runs": [r.to_dict() for r in runs]}, indent=2
            )
        )
        return
    print(f"ID:          {task.id}")
    print(f"Title:       {task.title}")
    print(f"Status:      {task.status.value}")
    print(f"Priority:    {task.priority.value}")
    print(f"Source:      {task.source.value}")
    print(f"File:        {task.file_path}:{task.line_number}")
    print(f"Branch:      {task.branch_name}")
    print(f"Worktree:    {task.worktree_path}")
    print(f"Retries:     {task.retry_count}/{task.max_retries}")
    print(f"Error:       {task.error or '-'}")
    if task.task_mode == "jira":
        print(f"Jira Status: {task.jira_status or '-'}")
        print(f"Jira Key:    {task.jira_issue_key or '-'}")
        print(f"Jira URL:    {task.jira_issue_url or '-'}")
    if task.plan_output:
        print(f"\n--- Plan ---\n{task.plan_output[:500]}")
    if task.task_mode == "jira" and task.jira_agent_output:
        print(f"\n--- Jira Agent Output ---\n{task.jira_agent_output[:500]}")
    if task.task_mode != "jira" and task.code_output:
        print(f"\n--- Code Output ---\n{task.code_output[:500]}")
    if task.task_mode != "jira" and task.review_output:
        print(f"\n--- Review ---\n{task.review_output[:500]}")
    if runs:
        print(f"\n--- Agent Runs ({len(runs)}) ---")
        for r in runs:
            print(
                f"  [{r.agent_type}] model={r.model} exit={r.exit_code} "
                f"duration={r.duration_sec:.1f}s"
            )


def cmd_dispatch(args):
    """Dispatch a specific task or all pending tasks."""
    config = load_config(args.config)
    host = config["web"]["host"]
    if host == "0.0.0.0":
        host = "127.0.0.1"
    base_url = f"http://{host}:{config['web']['port']}"
    if args.task_id == "all":
        req = request.Request(f"{base_url}/api/dispatch-all", data=b"{}", method="POST")
        try:
            with request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            print(f"Could not reach daemon API at {base_url}: {exc}")
            print("Start the daemon first with `cli.py start`.")
            return
        print(f"Dispatched {result['dispatched']}/{result['total_pending']} tasks")
    else:
        req = request.Request(
            f"{base_url}/api/tasks/{args.task_id}/dispatch",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            print(f"Could not reach daemon API at {base_url}: {exc}")
            print("Start the daemon first with `cli.py start`.")
            return
        print(f"Dispatched: {result['dispatched']}")
        if result.get("queued"):
            print("Queued: True")


def cmd_run_one(args):
    """Run a single task synchronously (for testing)."""
    config = load_config(args.config)
    import logging

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    orch = Orchestrator(config)
    if args.task_id:
        task_id = args.task_id
    else:
        # Create a quick test task
        task = orch.submit_task(
            title=args.title or "Test task",
            description=args.description or "This is a test task",
            priority="medium",
        )
        task_id = task.id
        print(f"Created task: [{task_id}]")

    print(f"Running task {task_id} synchronously...")
    orch._execute_task(task_id)
    task = orch.db.get_task(task_id)
    print(f"Result: {task.status.value}")
    if task.error:
        print(f"Error: {task.error}")


def cmd_cancel(args):
    """Cancel a task."""
    config = load_config(args.config)
    orch = Orchestrator(config)
    ok = orch.cancel_task(args.task_id)
    print(f"Cancelled: {ok}")


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Agent TODO Resolver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s start                           # Start daemon (background)
  %(prog)s start --foreground              # Start in foreground
  %(prog)s stop                            # Stop daemon
  %(prog)s status                          # Show system status
  %(prog)s add -t "Fix bug" -d "..."       # Submit task (planner decides if split needed)
  %(prog)s scan [--limit 50]               # Scan for TODOs (stores for review)
  %(prog)s todos list                      # List scanned TODO items with scores
  %(prog)s todos analyze [id1 id2 ...]     # Analyze TODOs (all pending if no IDs given)
  %(prog)s todos dispatch id1 id2 ...      # Send TODOs to planner
  %(prog)s todos delete id1 id2 ...        # Delete TODO items
  %(prog)s list                            # List all tasks
  %(prog)s list --status pending           # List pending tasks
  %(prog)s show <task_id>                  # Show task details
  %(prog)s run-one -t "Test" -d "..."      # Run a single task synchronously
        """,
    )
    parser.add_argument("-c", "--config", help="Config file path")

    sub = parser.add_subparsers(dest="command")

    # start
    p = sub.add_parser("start", help="Start the daemon")
    p.add_argument("--foreground", "-f", action="store_true", help="Run in foreground")
    p.set_defaults(func=cmd_start)

    # stop
    p = sub.add_parser("stop", help="Stop the daemon")
    p.set_defaults(func=cmd_stop)

    # status
    p = sub.add_parser("status", help="Show status")
    p.set_defaults(func=cmd_status)

    # add
    p = sub.add_parser("add", help="Add a task")
    p.add_argument("-t", "--title", required=True)
    p.add_argument("-d", "--description", default="")
    p.add_argument(
        "-p", "--priority", default="medium", choices=["high", "medium", "low"]
    )
    p.set_defaults(func=cmd_add)

    # scan
    p = sub.add_parser(
        "scan", help="Scan for TODOs (stores them for review, does not create tasks)"
    )
    p.add_argument(
        "--limit", type=int, default=50, help="Max new items to store per scan"
    )
    p.set_defaults(func=cmd_scan)

    # todos
    p = sub.add_parser("todos", help="Manage scanned TODO items")
    p.add_argument("action", choices=["list", "analyze", "dispatch", "delete"])
    p.add_argument("ids", nargs="*", help="TODO item IDs (optional for list/analyze)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_todos)

    # list
    p = sub.add_parser("list", help="List tasks")
    p.add_argument("--status", "-s")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    # show
    p = sub.add_parser("show", help="Show task details")
    p.add_argument("task_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    # dispatch
    p = sub.add_parser("dispatch", help="Dispatch task(s)")
    p.add_argument("task_id", help="Task ID or 'all'")
    p.set_defaults(func=cmd_dispatch)

    # run-one
    p = sub.add_parser("run-one", help="Run a single task synchronously")
    p.add_argument("--task-id", help="Existing task ID")
    p.add_argument("-t", "--title", help="Title for new task")
    p.add_argument("-d", "--description", help="Description for new task")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_run_one)

    # cancel
    p = sub.add_parser("cancel", help="Cancel a task")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_cancel)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
