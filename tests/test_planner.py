"""Tests for agents/planner.py: JSON parsing logic with mocked client."""

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.planner import PlannerAgent
from core.models import AgentRun, ModelOutputError, TodoItem


def _make_opencode_text_output(text: str) -> str:
    """Build fake opencode JSON-lines output containing a single text event."""
    import json as _json
    return _json.dumps({"type": "text", "part": {"text": text}}) + "\n"


def _make_planner(mock_output_text: str) -> PlannerAgent:
    """Create a PlannerAgent whose run() returns a fixed text response."""
    client = MagicMock()
    agent = PlannerAgent(model="test-model", client=client)
    fake_run = AgentRun(
        task_id="t1", agent_type="planner", model="test-model",
        output=_make_opencode_text_output(mock_output_text),
        exit_code=0, duration_sec=1.0,
    )
    client.run.return_value = fake_run
    client.extract_last_text_block_or_raw.return_value = mock_output_text
    return agent


class TestAnalyzeAndSplit:

    def test_single_task_no_split(self):
        model_output = json.dumps({
            "complexity": "medium",
            "split": False,
            "reason": "Simple change",
            "plan": "1. Edit file\n2. Test",
        })
        planner = _make_planner(model_output)
        run, is_split, plan_text, sub_tasks, complexity = planner.analyze_and_split(
            title="Fix bug", description="Fix the null pointer", repo_path="/repo",
        )
        assert is_split is False
        assert complexity == "medium"
        assert plan_text == "1. Edit file\n2. Test"
        assert sub_tasks == []

    def test_split_with_dependencies(self):
        model_output = json.dumps({
            "complexity": "complex",
            "split": True,
            "reason": "Multiple independent modules",
            "sub_tasks": [
                {"title": "Task A", "description": "Do A", "priority": "high", "depends_on": []},
                {"title": "Task B", "description": "Do B", "priority": "medium", "depends_on": [0]},
            ],
        })
        planner = _make_planner(model_output)
        run, is_split, plan_text, sub_tasks, complexity = planner.analyze_and_split(
            title="Big feature", description="Implement X", repo_path="/repo",
        )
        assert is_split is True
        assert complexity == "complex"
        assert len(sub_tasks) == 2
        assert sub_tasks[0]["title"] == "Task A"
        assert sub_tasks[1]["depends_on"] == [0]

    def test_no_json_raises(self):
        """When output has no JSON object at all, ModelOutputError is raised."""
        planner = _make_planner("This is not JSON at all, just text output.")
        with pytest.raises(ModelOutputError, match="no JSON object"):
            planner.analyze_and_split(
                title="Something", description="desc", repo_path="/repo",
            )

    def test_invalid_json_in_braces_raises(self):
        """When output contains {...} but it's not valid JSON, ModelOutputError is raised."""
        bad_output = "Here is my plan: {not: valid: json:}"
        planner = _make_planner(bad_output)
        with pytest.raises(ModelOutputError, match="invalid JSON"):
            planner.analyze_and_split(
                title="T", description="D", repo_path="/repo",
            )

    def test_json_embedded_in_text(self):
        """Model may wrap JSON in explanatory text."""
        model_output = (
            'Here is my analysis:\n'
            '{"complexity": "simple", "split": false, "reason": "trivial", "plan": "just do it"}\n'
            'Let me know if you need more.'
        )
        planner = _make_planner(model_output)
        run, is_split, plan_text, sub_tasks, complexity = planner.analyze_and_split(
            title="T", description="D", repo_path="/repo",
        )
        assert complexity == "simple"
        assert is_split is False
        assert plan_text == "just do it"


    def test_split_true_but_no_sub_tasks_key_raises(self):
        """Model says split=true but omits sub_tasks entirely → error."""
        model_output = json.dumps({
            "complexity": "complex",
            "split": True,
            "reason": "Should split",
        })
        planner = _make_planner(model_output)
        with pytest.raises(ModelOutputError, match="no sub_tasks"):
            planner.analyze_and_split(
                title="T", description="D", repo_path="/repo",
            )

    def test_split_true_but_empty_sub_tasks_raises(self):
        model_output = json.dumps({
            "complexity": "complex",
            "split": True,
            "reason": "Split into... nothing",
            "sub_tasks": [],
        })
        planner = _make_planner(model_output)
        with pytest.raises(ModelOutputError, match="no sub_tasks"):
            planner.analyze_and_split(
                title="T", description="D", repo_path="/repo",
            )

    def test_missing_complexity_field(self):
        model_output = json.dumps({
            "split": False,
            "reason": "Simple",
            "plan": "Do it",
        })
        planner = _make_planner(model_output)
        run, is_split, plan_text, sub_tasks, complexity = planner.analyze_and_split(
            title="T", description="D", repo_path="/repo",
        )
        assert complexity == ""
        assert plan_text == "Do it"

    def test_split_field_as_string_truthy(self):
        """Model outputs split as a string instead of bool."""
        model_output = json.dumps({
            "complexity": "medium",
            "split": "true",
            "reason": "Needs splitting",
            "sub_tasks": [{"title": "A", "description": "do A"}],
        })
        planner = _make_planner(model_output)
        run, is_split, plan_text, sub_tasks, complexity = planner.analyze_and_split(
            title="T", description="D", repo_path="/repo",
        )
        # bool("true") == True
        assert is_split is True
        assert len(sub_tasks) == 1

    def test_complexity_as_number(self):
        """Model outputs complexity as a number instead of string."""
        model_output = json.dumps({
            "complexity": 3,
            "split": False,
            "plan": "Steps",
        })
        planner = _make_planner(model_output)
        run, is_split, plan_text, sub_tasks, complexity = planner.analyze_and_split(
            title="T", description="D", repo_path="/repo",
        )
        assert complexity == "3"

    def test_force_no_split_keeps_single_task_even_if_model_requests_split(self):
        model_output = json.dumps({
            "complexity": "complex",
            "split": True,
            "reason": "Would normally split",
            "sub_tasks": [{"title": "A", "description": "Do A"}],
        })
        planner = _make_planner(model_output)
        run, is_split, plan_text, sub_tasks, complexity = planner.analyze_and_split(
            title="T",
            description="D",
            repo_path="/repo",
            force_no_split=True,
        )
        assert is_split is False
        assert complexity == "complex"
        assert sub_tasks == []
        assert "Do A" in plan_text

    def test_sub_tasks_missing_fields_use_defaults(self):
        """Sub-task dicts may lack optional fields like priority and depends_on."""
        model_output = json.dumps({
            "complexity": "complex",
            "split": True,
            "reason": "Big",
            "sub_tasks": [
                {"title": "Only title"},
                {"title": "With desc", "description": "Has description"},
            ],
        })
        planner = _make_planner(model_output)
        run, is_split, plan_text, sub_tasks, complexity = planner.analyze_and_split(
            title="T", description="D", repo_path="/repo",
        )
        assert is_split is True
        assert len(sub_tasks) == 2
        assert sub_tasks[0].get("depends_on", []) == []
        assert sub_tasks[0].get("description", "") == ""


class TestAnalyzeTodo:

    def test_parses_scores_and_note(self):
        model_output = json.dumps({
            "feasibility_score": 8.5,
            "difficulty_score": 3.0,
            "note": "Straightforward fix in a single file.",
        })
        planner = _make_planner(model_output)
        item = TodoItem(
            file_path="src/main.py", line_number=42,
            raw_text="# TODO: fix this", description="fix this",
        )
        run, feasibility, difficulty, note = planner.analyze_todo(item, "/repo")
        assert feasibility == 8.5
        assert difficulty == 3.0
        assert note == "Straightforward fix in a single file."

    def test_no_json_in_output(self):
        planner = _make_planner("I cannot analyze this TODO comment.")
        item = TodoItem(file_path="a.py", line_number=1, raw_text="# TODO: x", description="x")
        run, feasibility, difficulty, note = planner.analyze_todo(item, "/repo")
        assert feasibility == -1.0
        assert difficulty == -1.0
        assert "I cannot analyze" in note

    def test_partial_json_fields(self):
        model_output = json.dumps({"feasibility_score": 7.0})
        planner = _make_planner(model_output)
        item = TodoItem(file_path="b.py", line_number=5, raw_text="# TODO: y", description="y")
        run, feasibility, difficulty, note = planner.analyze_todo(item, "/repo")
        assert feasibility == 7.0
        assert difficulty == -1.0
        assert note == ""

    def test_malformed_json_raises(self):
        """Model output has braces but invalid JSON → raises ModelOutputError."""
        planner = _make_planner("Analysis: {not valid json here}")
        item = TodoItem(file_path="x.py", line_number=1, raw_text="# TODO: x", description="x")
        with pytest.raises(ModelOutputError, match="invalid JSON"):
            planner.analyze_todo(item, "/repo")

    def test_unconvertible_score_raises(self):
        """Model outputs a score that can't be converted to float."""
        model_output = json.dumps({
            "feasibility_score": "not_a_number",
            "difficulty_score": 3,
            "note": "ok",
        })
        planner = _make_planner(model_output)
        item = TodoItem(file_path="x.py", line_number=1, raw_text="# TODO: x", description="x")
        with pytest.raises(ModelOutputError, match="float"):
            planner.analyze_todo(item, "/repo")

    def test_scores_as_strings(self):
        """Model outputs scores as strings instead of numbers."""
        model_output = json.dumps({
            "feasibility_score": "9",
            "difficulty_score": "2",
            "note": "Easy fix",
        })
        planner = _make_planner(model_output)
        item = TodoItem(file_path="c.py", line_number=1, raw_text="# TODO: z", description="z")
        run, feasibility, difficulty, note = planner.analyze_todo(item, "/repo")
        assert feasibility == 9.0
        assert difficulty == 2.0

    def test_json_embedded_in_explanation(self):
        model_output = (
            'After analyzing the code:\n'
            '{"feasibility_score": 6, "difficulty_score": 4, "note": "Moderate effort"}\n'
            'Hope that helps!'
        )
        planner = _make_planner(model_output)
        item = TodoItem(file_path="d.py", line_number=10, raw_text="# TODO: refactor", description="refactor")
        run, feasibility, difficulty, note = planner.analyze_todo(item, "/repo")
        assert feasibility == 6.0
        assert difficulty == 4.0
        assert note == "Moderate effort"


class TestCreateTasksFromTodos:

    def _make_planner_instance(self):
        client = MagicMock()
        return PlannerAgent(model="test-model", client=client)

    def test_basic_conversion(self):
        planner = self._make_planner_instance()
        todos = [
            {"file": "/repo/src/main.py", "line": 42, "text": "// TODO: fix null pointer bug"},
            {"file": "/repo/src/util.py", "line": 10, "text": "// FIXME: optimize this loop"},
        ]
        tasks = planner.create_tasks_from_todos(todos)
        assert len(tasks) == 2
        assert "fix null pointer bug" in tasks[0].title
        assert tasks[0].file_path == "/repo/src/main.py"
        assert tasks[0].line_number == 42

    def test_short_description_skipped(self):
        """TODOs with descriptions shorter than 5 chars are filtered out."""
        planner = self._make_planner_instance()
        todos = [
            {"file": "a.py", "line": 1, "text": "// TODO: ab"},   # too short (2 chars)
            {"file": "b.py", "line": 2, "text": "// TODO: fix the broken parser logic"},
        ]
        tasks = planner.create_tasks_from_todos(todos)
        assert len(tasks) == 1
        assert tasks[0].file_path == "b.py"

    def test_max_tasks_limit(self):
        planner = self._make_planner_instance()
        todos = [
            {"file": f"f{i}.py", "line": i, "text": f"// TODO: task number {i} description here"}
            for i in range(30)
        ]
        tasks = planner.create_tasks_from_todos(todos, max_tasks=5)
        assert len(tasks) == 5

    def test_empty_input(self):
        planner = self._make_planner_instance()
        assert planner.create_tasks_from_todos([]) == []

    def test_various_prefixes(self):
        planner = self._make_planner_instance()
        todos = [
            {"file": "a.py", "line": 1, "text": "// FIXME: handle edge case properly"},
            {"file": "b.py", "line": 2, "text": "# HACK: temporary workaround for the issue"},
            {"file": "c.py", "line": 3, "text": "/* XXX: needs a proper implementation now */"},
        ]
        tasks = planner.create_tasks_from_todos(todos)
        assert len(tasks) == 3


class TestDecomposeComplexTask:

    def test_valid_json_array(self):
        model_output = json.dumps([
            {"title": "Sub A", "description": "Do A"},
            {"title": "Sub B", "description": "Do B"},
        ])
        planner = _make_planner(model_output)
        run, sub_tasks = planner.decompose_complex_task("Complex task", "/repo")
        assert len(sub_tasks) == 2
        assert sub_tasks[0]["title"] == "Sub A"

    def test_json_array_embedded_in_text(self):
        model_output = (
            'Here are the sub-tasks:\n'
            '[{"title": "A", "description": "Do A"}]\n'
            'Done.'
        )
        planner = _make_planner(model_output)
        run, sub_tasks = planner.decompose_complex_task("Complex", "/repo")
        assert len(sub_tasks) == 1

    def test_no_json_array_raises(self):
        planner = _make_planner("I'll just write a plan in text.")
        with pytest.raises(ModelOutputError, match="no JSON array"):
            planner.decompose_complex_task("Task", "/repo")

    def test_invalid_json_raises(self):
        planner = _make_planner("[not valid json array]")
        with pytest.raises(ModelOutputError, match="invalid JSON array"):
            planner.decompose_complex_task("Task", "/repo")
