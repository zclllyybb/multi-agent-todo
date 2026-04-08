"""Tests for core/models.py: serialization roundtrips, backward compat, enum handling."""

from core.models import (
    AgentRun,
    Task,
    TaskPriority,
    TaskStatus,
    TodoItem,
    TodoItemStatus,
)


class TestTaskRoundtrip:
    """Task.to_dict / Task.from_dict must be lossless for all fields."""

    def test_basic_roundtrip(self, make_task):
        t = make_task(title="Fix bug", depends_on=["abc", "def"])
        d = t.to_dict()
        t2 = Task.from_dict(d)
        assert t2.title == t.title
        assert t2.status == t.status
        assert t2.priority == t.priority
        assert t2.source == t.source
        assert t2.depends_on == ["abc", "def"]

    def test_enum_values_serialized_as_strings(self, make_task):
        t = make_task(status=TaskStatus.CODING, priority=TaskPriority.HIGH)
        d = t.to_dict()
        assert d["status"] == "coding"
        assert d["priority"] == "high"

    def test_jira_assigning_status_serialized_as_strings(self, make_task):
        t = make_task(status=TaskStatus.JIRA_ASSIGNING)
        d = t.to_dict()
        assert d["status"] == "jira_assigning"

    def test_active_statuses_centralized(self):
        assert TaskStatus.active_statuses() == (
            TaskStatus.PLANNING,
            TaskStatus.CODING,
            TaskStatus.JIRA_ASSIGNING,
            TaskStatus.REVIEWING,
        )
        assert TaskStatus.is_active(TaskStatus.JIRA_ASSIGNING) is True
        assert TaskStatus.is_active(TaskStatus.PENDING) is False

    def test_complex_fields_roundtrip(self, make_task):
        t = make_task(
            session_ids={"planner": ["ses1"], "coder": ["ses2", "ses3"]},
            reviewer_results=[{"model": "gpt-4", "passed": True, "output": "ok"}],
            copy_files=["a.py", "b.py"],
        )
        t2 = Task.from_dict(t.to_dict())
        assert t2.session_ids == t.session_ids
        assert t2.reviewer_results == t.reviewer_results
        assert t2.copy_files == t.copy_files


class TestTaskFromDictBackwardCompat:
    """from_dict must tolerate missing keys added in later versions."""

    def test_missing_depends_on(self):
        d = Task(title="old task").to_dict()
        del d["depends_on"]
        t = Task.from_dict(d)
        assert t.depends_on == []

    def test_missing_complexity(self):
        d = Task(title="old task").to_dict()
        del d["complexity"]
        t = Task.from_dict(d)
        assert t.complexity == ""

    def test_missing_copy_files(self):
        d = Task(title="old task").to_dict()
        del d["copy_files"]
        t = Task.from_dict(d)
        assert t.copy_files == []

    def test_missing_task_mode(self):
        d = Task(title="old task").to_dict()
        del d["task_mode"]
        t = Task.from_dict(d)
        assert t.task_mode == "develop"

    def test_missing_user_feedback(self):
        d = Task(title="old task").to_dict()
        del d["user_feedback"]
        t = Task.from_dict(d)
        assert t.user_feedback == ""

    def test_missing_published_at(self):
        d = Task(title="old task").to_dict()
        del d["published_at"]
        t = Task.from_dict(d)
        assert t.published_at == 0.0

    def test_missing_jira_fields(self):
        d = Task(title="old task").to_dict()
        del d["jira_issue_key"]
        del d["jira_issue_url"]
        del d["jira_payload_preview"]
        t = Task.from_dict(d)
        assert t.jira_issue_key == ""
        assert t.jira_issue_url == ""
        assert t.jira_payload_preview == ""

    def test_unknown_future_fields_are_ignored(self):
        d = Task(title="old task").to_dict()
        d["future_field"] = "unexpected"
        d["jira_issue_key"] = "QA-1"
        t = Task.from_dict(d)
        assert t.title == "old task"
        assert t.jira_issue_key == "QA-1"


class TestTodoItemRoundtrip:
    def test_basic_roundtrip(self):
        item = TodoItem(
            file_path="src/main.py",
            line_number=42,
            raw_text="# TODO: fix this",
            description="fix this",
            feasibility_score=8.0,
            difficulty_score=3.0,
        )
        d = item.to_dict()
        item2 = TodoItem.from_dict(d)
        assert item2.file_path == item.file_path
        assert item2.line_number == item.line_number
        assert item2.feasibility_score == 8.0
        assert item2.difficulty_score == 3.0

    def test_backward_compat_relevance_score(self):
        """Old records had 'relevance_score' instead of 'difficulty_score'."""
        d = TodoItem(file_path="a.py", line_number=1).to_dict()
        d["relevance_score"] = 5.0
        del d["difficulty_score"]
        item = TodoItem.from_dict(d)
        assert item.difficulty_score == 5.0

    def test_enum_serialization(self):
        item = TodoItem(status=TodoItemStatus.ANALYZED)
        d = item.to_dict()
        assert d["status"] == "analyzed"
        item2 = TodoItem.from_dict(d)
        assert item2.status == TodoItemStatus.ANALYZED


class TestAgentRunRoundtrip:
    def test_basic_roundtrip(self):
        run = AgentRun(
            task_id="task1",
            agent_type="planner",
            model="gpt-4",
            prompt="do something",
            output="done",
            exit_code=0,
            duration_sec=1.5,
            session_id="ses_abc",
        )
        d = run.to_dict()
        run2 = AgentRun.from_dict(d)
        assert run2.task_id == run.task_id
        assert run2.agent_type == run.agent_type
        assert run2.session_id == "ses_abc"

    def test_missing_session_id(self):
        d = AgentRun(task_id="t1").to_dict()
        del d["session_id"]
        run = AgentRun.from_dict(d)
        assert run.session_id == ""
