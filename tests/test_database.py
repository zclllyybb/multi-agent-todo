"""Tests for core/database.py: CRUD operations with a temporary SQLite database."""

import pytest

from core.database import Database
from core.models import (
    AgentRun,
    Task,
    TaskPriority,
    TaskSource,
    TaskStatus,
    TodoItem,
    TodoItemStatus,
)


class TestTaskCRUD:
    def test_save_and_get(self, tmp_db, make_task):
        t = make_task(title="Save me")
        tmp_db.save_task(t)
        loaded = tmp_db.get_task(t.id)
        assert loaded.title == "Save me"
        assert loaded.status == TaskStatus.PENDING

    def test_get_nonexistent_returns_none(self, tmp_db):
        assert tmp_db.get_task("nonexistent") is None

    def test_update_preserves_id(self, tmp_db, make_task):
        t = make_task(title="v1")
        tmp_db.save_task(t)
        t.title = "v2"
        t.status = TaskStatus.CODING
        tmp_db.save_task(t)
        loaded = tmp_db.get_task(t.id)
        assert loaded.title == "v2"
        assert loaded.status == TaskStatus.CODING

    def test_get_all_tasks(self, tmp_db, make_task):
        for i in range(3):
            tmp_db.save_task(make_task(title=f"task-{i}"))
        assert len(tmp_db.get_all_tasks()) == 3

    def test_get_tasks_by_status(self, tmp_db, make_task):
        tmp_db.save_task(make_task(status=TaskStatus.PENDING))
        tmp_db.save_task(make_task(status=TaskStatus.CODING))
        tmp_db.save_task(make_task(status=TaskStatus.PENDING))
        assert len(tmp_db.get_tasks_by_status(TaskStatus.PENDING)) == 2
        assert len(tmp_db.get_tasks_by_status(TaskStatus.CODING)) == 1

    def test_get_active_tasks(self, tmp_db, make_task):
        tmp_db.save_task(make_task(status=TaskStatus.PENDING))
        tmp_db.save_task(make_task(status=TaskStatus.PLANNING))
        tmp_db.save_task(make_task(status=TaskStatus.CODING))
        tmp_db.save_task(make_task(status=TaskStatus.JIRA_ASSIGNING))
        tmp_db.save_task(make_task(status=TaskStatus.REVIEWING))
        tmp_db.save_task(make_task(status=TaskStatus.COMPLETED))
        active = tmp_db.get_active_tasks()
        assert len(active) == 4
        assert all(TaskStatus.is_active(t.status) for t in active)

    def test_delete_task(self, tmp_db, make_task):
        t = make_task()
        tmp_db.save_task(t)
        tmp_db.delete_task(t.id)
        assert tmp_db.get_task(t.id) is None

    def test_depends_on_persists(self, tmp_db, make_task):
        t = make_task(depends_on=["dep1", "dep2"])
        tmp_db.save_task(t)
        loaded = tmp_db.get_task(t.id)
        assert loaded.depends_on == ["dep1", "dep2"]

    def test_comments_persist(self, tmp_db, make_task):
        t = make_task(
            comments=[
                {
                    "id": "c1",
                    "username": "alice",
                    "content": "looks good",
                    "created_at": 123.0,
                }
            ]
        )
        tmp_db.save_task(t)
        loaded = tmp_db.get_task(t.id)
        assert len(loaded.comments) == 1
        assert loaded.comments[0]["username"] == "alice"
        assert loaded.comments[0]["content"] == "looks good"


class TestTodoItemCRUD:
    def test_save_and_get(self, tmp_db):
        item = TodoItem(file_path="a.py", line_number=10, description="fix bug")
        tmp_db.save_todo_item(item)
        loaded = tmp_db.get_todo_item(item.id)
        assert loaded.file_path == "a.py"
        assert loaded.description == "fix bug"

    def test_get_nonexistent_returns_none(self, tmp_db):
        assert tmp_db.get_todo_item("nonexistent") is None

    def test_get_all_todo_items(self, tmp_db):
        for i in range(3):
            tmp_db.save_todo_item(TodoItem(file_path=f"f{i}.py", line_number=i))
        assert len(tmp_db.get_all_todo_items()) == 3

    def test_get_by_status(self, tmp_db):
        tmp_db.save_todo_item(TodoItem(status=TodoItemStatus.PENDING_ANALYSIS))
        tmp_db.save_todo_item(TodoItem(status=TodoItemStatus.ANALYZED))
        tmp_db.save_todo_item(TodoItem(status=TodoItemStatus.PENDING_ANALYSIS))
        result = tmp_db.get_todo_items_by_status(TodoItemStatus.PENDING_ANALYSIS)
        assert len(result) == 2

    def test_delete_todo_item(self, tmp_db):
        item = TodoItem(file_path="x.py", line_number=1)
        tmp_db.save_todo_item(item)
        tmp_db.delete_todo_item(item.id)
        assert tmp_db.get_todo_item(item.id) is None


class TestAgentRunCRUD:
    def test_save_and_get_by_task(self, tmp_db):
        r1 = AgentRun(task_id="t1", agent_type="planner", output="plan")
        r2 = AgentRun(task_id="t1", agent_type="coder", output="code")
        r3 = AgentRun(task_id="t2", agent_type="reviewer", output="review")
        for r in [r1, r2, r3]:
            tmp_db.save_agent_run(r)
        runs = tmp_db.get_runs_for_task("t1")
        assert len(runs) == 2
        assert {r.agent_type for r in runs} == {"planner", "coder"}

    def test_get_runs_for_nonexistent_task(self, tmp_db):
        assert tmp_db.get_runs_for_task("no_such_task") == []

    def test_delete_agent_runs_for_task(self, tmp_db):
        run1 = AgentRun(task_id="t1", agent_type="planner", output="plan")
        run2 = AgentRun(task_id="t1", agent_type="coder", output="code")
        run3 = AgentRun(task_id="t2", agent_type="reviewer", output="review")
        for run in (run1, run2, run3):
            tmp_db.save_agent_run(run)

        tmp_db.delete_agent_runs_for_task("t1")

        assert tmp_db.get_runs_for_task("t1") == []
        remaining = tmp_db.get_runs_for_task("t2")
        assert len(remaining) == 1
        assert remaining[0].agent_type == "reviewer"
