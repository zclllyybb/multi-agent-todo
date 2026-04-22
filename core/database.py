"""SQLite persistence layer for tasks and agent runs."""

import json
import os
import sqlite3
import threading
from typing import List, Optional

from core.models import (
    Task,
    TaskStatus,
    AgentRun,
    TodoItem,
    TodoItemStatus,
    ExploreModule,
    ExploreRun,
)
from core.task_artifacts import write_task_note


class Database:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id);
            CREATE TABLE IF NOT EXISTS todo_items (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_todo_items_status ON todo_items(status);

            CREATE TABLE IF NOT EXISTS explore_modules (
                id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_explore_modules_parent
                ON explore_modules(parent_id);

            CREATE TABLE IF NOT EXISTS explore_runs (
                id TEXT PRIMARY KEY,
                module_id TEXT NOT NULL,
                category TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_explore_runs_module
                ON explore_runs(module_id);

            CREATE TABLE IF NOT EXISTS explore_queue_jobs (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_explore_queue_jobs_state
                ON explore_queue_jobs(state);

            CREATE TABLE IF NOT EXISTS orchestrator_state (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()

    def save_task(self, task: Task):
        self._conn.execute(
            "INSERT OR REPLACE INTO tasks (id, data) VALUES (?, ?)",
            (task.id, json.dumps(task.to_dict())),
        )
        self._conn.commit()
        write_task_note(task, self._db_path)

    def get_task(self, task_id: str) -> Optional[Task]:
        row = self._conn.execute(
            "SELECT data FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row:
            return Task.from_dict(json.loads(row[0]))
        return None

    def get_all_tasks(self) -> List[Task]:
        rows = self._conn.execute("SELECT data FROM tasks").fetchall()
        return [Task.from_dict(json.loads(r[0])) for r in rows]

    def get_tasks_by_status(self, status: TaskStatus) -> List[Task]:
        tasks = self.get_all_tasks()
        return [t for t in tasks if t.status == status]

    def get_active_tasks(self) -> List[Task]:
        tasks = self.get_all_tasks()
        return [t for t in tasks if TaskStatus.is_active(t.status)]

    def get_pending_tasks(self) -> List[Task]:
        return self.get_tasks_by_status(TaskStatus.PENDING)

    def delete_task(self, task_id: str):
        self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()

    def delete_agent_runs_for_task(self, task_id: str):
        self._conn.execute("DELETE FROM agent_runs WHERE task_id = ?", (task_id,))
        self._conn.commit()

    # ── TodoItem CRUD ─────────────────────────────────────────────────

    def save_todo_item(self, item: TodoItem):
        self._conn.execute(
            "INSERT OR REPLACE INTO todo_items (id, status, data) VALUES (?, ?, ?)",
            (item.id, item.status.value, json.dumps(item.to_dict())),
        )
        self._conn.commit()

    def get_todo_item(self, item_id: str) -> Optional[TodoItem]:
        row = self._conn.execute(
            "SELECT data FROM todo_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row:
            return TodoItem.from_dict(json.loads(row[0]))
        return None

    def get_all_todo_items(self) -> List[TodoItem]:
        rows = self._conn.execute(
            "SELECT data FROM todo_items ORDER BY rowid DESC"
        ).fetchall()
        return [TodoItem.from_dict(json.loads(r[0])) for r in rows]

    def get_todo_items_by_status(self, status: TodoItemStatus) -> List[TodoItem]:
        rows = self._conn.execute(
            "SELECT data FROM todo_items WHERE status = ? ORDER BY rowid DESC",
            (status.value,),
        ).fetchall()
        return [TodoItem.from_dict(json.loads(r[0])) for r in rows]

    def delete_todo_item(self, item_id: str):
        self._conn.execute("DELETE FROM todo_items WHERE id = ?", (item_id,))
        self._conn.commit()

    # ── AgentRun CRUD ─────────────────────────────────────────────────

    def save_agent_run(self, run: AgentRun):
        self._conn.execute(
            "INSERT OR REPLACE INTO agent_runs (id, task_id, data) VALUES (?, ?, ?)",
            (run.id, run.task_id, json.dumps(run.to_dict())),
        )
        self._conn.commit()

    def get_runs_for_task(self, task_id: str) -> List[AgentRun]:
        rows = self._conn.execute(
            "SELECT data FROM agent_runs WHERE task_id = ?", (task_id,)
        ).fetchall()
        return [AgentRun.from_dict(json.loads(r[0])) for r in rows]

    # ── ExploreModule CRUD ──────────────────────────────────────────────

    def save_explore_module(self, module: ExploreModule):
        self._conn.execute(
            "INSERT OR REPLACE INTO explore_modules (id, parent_id, data) VALUES (?, ?, ?)",
            (module.id, module.parent_id, json.dumps(module.to_dict())),
        )
        self._conn.commit()

    def get_explore_module(self, module_id: str) -> Optional[ExploreModule]:
        row = self._conn.execute(
            "SELECT data FROM explore_modules WHERE id = ?", (module_id,)
        ).fetchone()
        if row:
            return ExploreModule.from_dict(json.loads(row[0]))
        return None

    def get_all_explore_modules(self) -> List[ExploreModule]:
        rows = self._conn.execute(
            "SELECT data FROM explore_modules ORDER BY rowid"
        ).fetchall()
        return [ExploreModule.from_dict(json.loads(r[0])) for r in rows]

    def get_child_modules(self, parent_id: str) -> List[ExploreModule]:
        rows = self._conn.execute(
            "SELECT data FROM explore_modules WHERE parent_id = ? ORDER BY rowid",
            (parent_id,),
        ).fetchall()
        return [ExploreModule.from_dict(json.loads(r[0])) for r in rows]

    def delete_explore_module(self, module_id: str):
        self._conn.execute("DELETE FROM explore_modules WHERE id = ?", (module_id,))
        self._conn.commit()

    def delete_all_explore_modules(self):
        self._conn.execute("DELETE FROM explore_modules")
        self._conn.commit()

    # ── ExploreRun CRUD ─────────────────────────────────────────────────

    def save_explore_run(self, run: ExploreRun):
        self._conn.execute(
            "INSERT OR REPLACE INTO explore_runs (id, module_id, category, data) "
            "VALUES (?, ?, ?, ?)",
            (run.id, run.module_id, run.category, json.dumps(run.to_dict())),
        )
        self._conn.commit()

    def get_explore_run(self, run_id: str) -> Optional[ExploreRun]:
        row = self._conn.execute(
            "SELECT data FROM explore_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row:
            return ExploreRun.from_dict(json.loads(row[0]))
        return None

    def get_explore_runs_for_module(self, module_id: str) -> List[ExploreRun]:
        rows = self._conn.execute(
            "SELECT data FROM explore_runs WHERE module_id = ? ORDER BY rowid DESC",
            (module_id,),
        ).fetchall()
        return [ExploreRun.from_dict(json.loads(r[0])) for r in rows]

    def get_all_explore_runs(self) -> List[ExploreRun]:
        rows = self._conn.execute(
            "SELECT data FROM explore_runs ORDER BY rowid DESC"
        ).fetchall()
        return [ExploreRun.from_dict(json.loads(r[0])) for r in rows]

    def delete_all_explore_runs(self):
        self._conn.execute("DELETE FROM explore_runs")
        self._conn.commit()

    # ── Explore Queue Job Persistence ──────────────────────────────────

    def save_explore_queue_job(self, job: dict):
        state = str(job.get("state", "queued"))
        self._conn.execute(
            "INSERT OR REPLACE INTO explore_queue_jobs (id, state, data) VALUES (?, ?, ?)",
            (job["job_id"], state, json.dumps(job)),
        )
        self._conn.commit()

    def get_explore_queue_jobs(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT data FROM explore_queue_jobs ORDER BY rowid"
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete_explore_queue_job(self, job_id: str):
        self._conn.execute("DELETE FROM explore_queue_jobs WHERE id = ?", (job_id,))
        self._conn.commit()

    def delete_all_explore_queue_jobs(self):
        self._conn.execute("DELETE FROM explore_queue_jobs")
        self._conn.commit()

    # ── Orchestrator State KV ──────────────────────────────────────────

    def save_state(self, key: str, value: dict):
        self._conn.execute(
            "INSERT OR REPLACE INTO orchestrator_state (key, data) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
        self._conn.commit()

    def get_state(self, key: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT data FROM orchestrator_state WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def delete_state(self, key: str):
        self._conn.execute("DELETE FROM orchestrator_state WHERE key = ?", (key,))
        self._conn.commit()
