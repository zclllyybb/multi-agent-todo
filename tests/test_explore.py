"""Comprehensive tests for the code exploration system.

Covers: models, database CRUD, explorer agent parsing, orchestrator exploration
methods (init_explore_map, start_exploration, _run_exploration, task creation),
and the full end-to-end flow with mocked model output.
"""

import json
import time
import threading
import asyncio
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import yaml

from core.models import (
    ExploreModule,
    ExploreRun,
    ExploreStatus,
    Task,
    TaskSource,
    TaskStatus,
    ModelOutputError,
    TodoItem,
    TodoItemStatus,
)
from core.database import Database
from core.opencode_client import OpenCodeClient
from agents.explorer import ExplorerAgent
from agents.prompts import (
    EXPLORER_PERSONALITIES,
    DEFAULT_EXPLORE_CATEGORIES,
    explorer_prompt,
    map_init_prompt,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "test_explore.db")
    return Database(db_path)


@pytest.fixture
def sample_module():
    return ExploreModule(
        id="mod_abc123",
        name="Query Engine",
        path="be/src/exec",
        parent_id="",
        depth=0,
        description="Vectorized execution engine",
        category_status={
            "performance": "todo",
            "concurrency": "todo",
        },
        category_notes={
            "performance": "",
            "concurrency": "",
        },
    )


@pytest.fixture
def sample_run():
    return ExploreRun(
        id="run_xyz789",
        module_id="mod_abc123",
        category="performance",
        personality="perf_hunter",
        model="test-model",
        prompt="test prompt",
        output="test output",
        session_id="ses_001",
        findings=[
            {
                "severity": "major",
                "title": "Unnecessary copy in hot path",
                "description": "A large vector is copied instead of moved",
                "file_path": "be/src/exec/scanner.cpp",
                "line_number": 42,
                "suggested_fix": "Use std::move",
            }
        ],
        summary="Found 1 performance issue",
        issue_count=1,
        exit_code=0,
        duration_sec=120.5,
    )


MOCK_MAP_OUTPUT = json.dumps(
    {
        "modules": [
            {
                "name": "Backend Engine",
                "path": "be/src",
                "description": "C++ backend execution engine",
                "children": [
                    {
                        "name": "Exec Module",
                        "path": "be/src/exec",
                        "description": "Query execution operators",
                        "children": [],
                    }
                ],
            },
            {
                "name": "Frontend",
                "path": "fe/src",
                "description": "Java frontend query planner",
                "children": [],
            },
        ]
    }
)

MOCK_EXPLORE_OUTPUT = json.dumps(
    {
        "summary": "Explored scanner.cpp and found a major performance issue with unnecessary copies.",
        "explored_scope": "scanner.cpp next_batch path and vector growth path",
        "completion_status": "complete",
        "findings": [
            {
                "severity": "major",
                "title": "Unnecessary vector copy in Scanner::next_batch()",
                "description": "The method copies a 1MB vector on every call instead of moving it.",
                "file_path": "be/src/exec/scanner.cpp",
                "line_number": 142,
                "suggested_fix": "Use std::move() to transfer ownership",
            },
            {
                "severity": "minor",
                "title": "Missing reserve() before push_back loop",
                "description": "A vector grows incrementally in a loop without pre-allocation.",
                "file_path": "be/src/exec/scanner.cpp",
                "line_number": 200,
                "suggested_fix": "Add results.reserve(expected_size) before the loop",
            },
        ],
    }
)

MOCK_EXPLORE_OUTPUT_EMPTY = json.dumps(
    {
        "summary": "Explored module, no issues found.",
        "explored_scope": "all primary entry points in the module",
        "completion_status": "complete",
        "findings": [],
    }
)


# ═══════════════════════════════════════════════════════════════════════
# 1. MODEL TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestExploreModels:
    def test_explore_status_values(self):
        assert ExploreStatus.TODO.value == "todo"
        assert ExploreStatus.IN_PROGRESS.value == "in_progress"
        assert ExploreStatus.DONE.value == "done"
        assert ExploreStatus.STALE.value == "stale"

    def test_task_source_explore(self):
        assert TaskSource.EXPLORE.value == "explore"

    def test_explore_module_defaults(self):
        m = ExploreModule()
        assert m.name == ""
        assert m.path == ""
        assert m.parent_id == ""
        assert m.depth == 0
        assert m.category_status == {}
        assert m.category_notes == {}
        assert m.file_count == 0
        assert m.loc == 0
        assert m.languages == []
        assert m.sort_order == 0
        assert m.id  # auto-generated

    def test_explore_module_to_dict_roundtrip(self, sample_module):
        d = sample_module.to_dict()
        assert d["name"] == "Query Engine"
        assert d["path"] == "be/src/exec"
        assert d["category_status"]["performance"] == "todo"

        restored = ExploreModule.from_dict(d)
        assert restored.id == sample_module.id
        assert restored.name == sample_module.name
        assert restored.category_status == sample_module.category_status

    def test_explore_module_from_dict_defaults(self):
        d = {
            "id": "x",
            "name": "foo",
            "path": "bar",
            "parent_id": "",
            "depth": 0,
            "description": "",
            "created_at": 1.0,
            "updated_at": 1.0,
        }
        m = ExploreModule.from_dict(d)
        assert m.category_status == {}
        assert m.category_notes == {}
        assert m.file_count == 0
        assert m.loc == 0
        assert m.languages == []
        assert m.sort_order == 0

    def test_explore_run_defaults(self):
        r = ExploreRun()
        assert r.module_id == ""
        assert r.category == ""
        assert r.findings == []
        assert r.summary == ""
        assert r.issue_count == 0
        assert r.exit_code == -1

    def test_explore_run_to_dict_roundtrip(self, sample_run):
        d = sample_run.to_dict()
        assert d["module_id"] == "mod_abc123"
        assert d["category"] == "performance"
        assert len(d["findings"]) == 1
        assert d["findings"][0]["severity"] == "major"

        restored = ExploreRun.from_dict(d)
        assert restored.id == sample_run.id
        assert restored.findings == sample_run.findings
        assert restored.summary == sample_run.summary

    def test_explore_run_from_dict_defaults(self):
        d = {
            "id": "r1",
            "module_id": "m1",
            "category": "perf",
            "personality": "p",
            "model": "m",
            "prompt": "",
            "output": "",
            "exit_code": 0,
            "duration_sec": 1.0,
            "created_at": 1.0,
        }
        r = ExploreRun.from_dict(d)
        assert r.session_id == ""
        assert r.findings == []
        assert r.summary == ""
        assert r.issue_count == 0

    def test_explore_run_from_dict_ignores_legacy_unknown_fields(self):
        d = {
            "id": "r1",
            "module_id": "m1",
            "category": "perf",
            "personality": "p",
            "model": "m",
            "prompt": "",
            "output": "",
            "completion_reason": "legacy field",
            "legacy_extra": {"x": 1},
            "exit_code": 0,
            "duration_sec": 1.0,
            "created_at": 1.0,
        }
        r = ExploreRun.from_dict(d)
        assert r.id == "r1"
        assert r.module_id == "m1"
        assert r.category == "perf"
        assert not hasattr(r, "completion_reason")


class TestExploreRunLegacyCompatibility:
    def test_database_loads_legacy_explore_run_with_removed_fields(self, tmp_path):
        db = Database(str(tmp_path / "legacy.db"))
        raw = {
            "id": "legacy-run",
            "module_id": "mod1",
            "category": "performance",
            "personality": "perf_hunter",
            "model": "test-explorer",
            "prompt": "explore",
            "output": "{}",
            "session_id": "ses-legacy",
            "summary": "legacy summary",
            "completion_status": "complete",
            "completion_reason": "legacy completion reason",
            "supplemental_note": "legacy note",
            "exit_code": 0,
            "duration_sec": 1.0,
            "created_at": 1.0,
        }
        db._conn.execute(
            "INSERT INTO explore_runs (id, module_id, category, data) VALUES (?, ?, ?, ?)",
            ("legacy-run", "mod1", "performance", json.dumps(raw)),
        )
        db._conn.commit()

        loaded = db.get_explore_run("legacy-run")
        assert loaded is not None
        assert loaded.id == "legacy-run"
        assert loaded.summary == "legacy summary"
        assert loaded.supplemental_note == "legacy note"

    def test_api_module_detail_handles_legacy_explore_run_payload(self, tmp_path):
        from web import app as web_app

        config = _make_orchestrator_config(tmp_path)
        with patch("core.orchestrator.OpenCodeClient"):
            from core.orchestrator import Orchestrator

            orch = Orchestrator(config)
        real_client = OpenCodeClient(timeout=10)
        orch.client.parse_readable_output = real_client.parse_readable_output

        mod = orch.add_explore_module(name="Exec", path="be/src/exec")
        legacy = {
            "id": "legacy-run",
            "module_id": mod["id"],
            "category": "performance",
            "personality": "perf_hunter",
            "model": "test-explorer",
            "prompt": "explore",
            "output": '{"sessionID":"ses-legacy","type":"init"}\n{"type":"step_start"}\n{"type":"text","part":{"text":"Legacy run"}}\n{"type":"step_finish","part":{"reason":"stop"}}\n',
            "session_id": "ses-legacy",
            "summary": "legacy summary",
            "completion_status": "complete",
            "completion_reason": "legacy completion reason",
            "supplemental_note": "legacy note",
            "exit_code": 0,
            "duration_sec": 1.0,
            "created_at": 1.0,
        }
        orch.db._conn.execute(
            "INSERT INTO explore_runs (id, module_id, category, data) VALUES (?, ?, ?, ?)",
            ("legacy-run", mod["id"], "performance", json.dumps(legacy)),
        )
        orch.db._conn.commit()

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            result = asyncio.run(web_app.api_explore_module_detail(mod["id"]))
        finally:
            web_app.set_orchestrator(original)

        assert result["module"]["id"] == mod["id"]
        assert len(result["runs"]) == 1
        assert result["runs"][0]["summary"] == "legacy summary"
        assert result["runs"][0]["supplemental_note"] == "legacy note"
        assert result["runs"][0]["parsed"]["session_id"] == "ses-legacy"


# ═══════════════════════════════════════════════════════════════════════
# 2. DATABASE TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestExploreDatabase:
    def test_save_and_get_module(self, tmp_db, sample_module):
        tmp_db.save_explore_module(sample_module)
        loaded = tmp_db.get_explore_module(sample_module.id)
        assert loaded is not None
        assert loaded.name == "Query Engine"
        assert loaded.path == "be/src/exec"

    def test_get_nonexistent_module(self, tmp_db):
        assert tmp_db.get_explore_module("nonexistent") is None

    def test_get_all_modules(self, tmp_db):
        m1 = ExploreModule(id="m1", name="A", path="a")
        m2 = ExploreModule(id="m2", name="B", path="b")
        tmp_db.save_explore_module(m1)
        tmp_db.save_explore_module(m2)
        all_mods = tmp_db.get_all_explore_modules()
        assert len(all_mods) == 2
        assert {m.id for m in all_mods} == {"m1", "m2"}

    def test_get_child_modules(self, tmp_db):
        parent = ExploreModule(id="p1", name="Parent", path="p")
        child1 = ExploreModule(id="c1", name="Child1", path="p/c1", parent_id="p1")
        child2 = ExploreModule(id="c2", name="Child2", path="p/c2", parent_id="p1")
        other = ExploreModule(id="o1", name="Other", path="o")
        for m in [parent, child1, child2, other]:
            tmp_db.save_explore_module(m)
        children = tmp_db.get_child_modules("p1")
        assert len(children) == 2
        assert {c.id for c in children} == {"c1", "c2"}

    def test_delete_module(self, tmp_db, sample_module):
        tmp_db.save_explore_module(sample_module)
        tmp_db.delete_explore_module(sample_module.id)
        assert tmp_db.get_explore_module(sample_module.id) is None

    def test_delete_all_modules(self, tmp_db):
        for i in range(5):
            tmp_db.save_explore_module(
                ExploreModule(id=f"m{i}", name=f"M{i}", path=f"p{i}")
            )
        assert len(tmp_db.get_all_explore_modules()) == 5
        tmp_db.delete_all_explore_modules()
        assert len(tmp_db.get_all_explore_modules()) == 0

    def test_save_and_get_run(self, tmp_db, sample_run):
        tmp_db.save_explore_run(sample_run)
        loaded = tmp_db.get_explore_run(sample_run.id)
        assert loaded is not None
        assert loaded.category == "performance"
        assert loaded.issue_count == 1

    def test_get_nonexistent_run(self, tmp_db):
        assert tmp_db.get_explore_run("nonexistent") is None

    def test_get_runs_for_module(self, tmp_db):
        r1 = ExploreRun(id="r1", module_id="m1", category="perf")
        r2 = ExploreRun(id="r2", module_id="m1", category="security")
        r3 = ExploreRun(id="r3", module_id="m2", category="perf")
        for r in [r1, r2, r3]:
            tmp_db.save_explore_run(r)
        runs = tmp_db.get_explore_runs_for_module("m1")
        assert len(runs) == 2
        assert {r.id for r in runs} == {"r1", "r2"}

    def test_get_all_runs(self, tmp_db):
        for i in range(3):
            tmp_db.save_explore_run(
                ExploreRun(id=f"r{i}", module_id="m1", category="c")
            )
        assert len(tmp_db.get_all_explore_runs()) == 3

    def test_module_update_persists(self, tmp_db, sample_module):
        tmp_db.save_explore_module(sample_module)
        sample_module.category_status["performance"] = "done"
        sample_module.category_notes["performance"] = "All good"
        tmp_db.save_explore_module(sample_module)
        loaded = tmp_db.get_explore_module(sample_module.id)
        assert loaded.category_status["performance"] == "done"
        assert loaded.category_notes["performance"] == "All good"


# ═══════════════════════════════════════════════════════════════════════
# 3. EXPLORER AGENT PARSING TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestExplorerAgentParsing:
    def test_parse_output_valid(self):
        findings, summary = ExplorerAgent._parse_output(MOCK_EXPLORE_OUTPUT)
        assert len(findings) == 2
        assert (
            summary
            == "Explored scanner.cpp and found a major performance issue with unnecessary copies."
        )
        assert findings[0]["severity"] == "major"
        assert (
            findings[0]["title"] == "Unnecessary vector copy in Scanner::next_batch()"
        )
        assert findings[0]["line_number"] == 142
        assert findings[1]["severity"] == "minor"

    def test_parse_output_empty_findings(self):
        findings, summary = ExplorerAgent._parse_output(MOCK_EXPLORE_OUTPUT_EMPTY)
        assert findings == []
        assert "no issues" in summary

    def test_parse_output_no_json(self):
        with pytest.raises(ModelOutputError, match="no JSON object found"):
            ExplorerAgent._parse_output("Just some random text without JSON")

    def test_parse_output_invalid_json(self):
        with pytest.raises(ModelOutputError, match="invalid JSON|no JSON object found"):
            ExplorerAgent._parse_output("{broken json here")

    def test_parse_output_metadata_with_scores_and_review_flags(self):
        text = json.dumps(
            {
                "summary": "checked lock paths",
                "focus_point": "lock contention in scanner",
                "actionability_score": 8.4,
                "reliability_score": 7.2,
                "explored_scope": "scanner lock acquisition and release paths",
                "completion_status": "partial",
                "supplemental_note": "Lock scope can be narrowed in two call paths.",
                "map_review_required": True,
                "map_review_reason": "scanner module should be split by responsibility",
                "findings": [],
            }
        )
        meta = ExplorerAgent.parse_output_metadata(text)
        assert meta["summary"] == "checked lock paths"
        assert meta["focus_point"] == "lock contention in scanner"
        assert meta["actionability_score"] == 8.4
        assert meta["reliability_score"] == 7.2
        assert meta["explored_scope"].startswith("scanner lock")
        assert meta["completion_status"] == "partial"
        assert meta["supplemental_note"].startswith("Lock scope")
        assert meta["map_review_required"] is True
        assert "split" in meta["map_review_reason"]

    def test_parse_output_metadata_clamps_scores(self):
        text = json.dumps(
            {
                "summary": "x",
                "actionability_score": 13,
                "reliability_score": -5,
                "findings": [],
            }
        )
        meta = ExplorerAgent.parse_output_metadata(text)
        assert meta["actionability_score"] == 10.0
        assert meta["reliability_score"] == 0.0

    def test_parse_output_with_surrounding_text(self):
        text = "Here is my analysis:\n" + MOCK_EXPLORE_OUTPUT + "\nDone."
        findings, summary = ExplorerAgent._parse_output(text)
        assert len(findings) == 2

    def test_parse_output_partial_finding_fields(self):
        text = json.dumps(
            {
                "summary": "test",
                "findings": [{"title": "A bug", "severity": "critical"}],
            }
        )
        findings, summary = ExplorerAgent._parse_output(text)
        assert len(findings) == 1
        assert findings[0]["title"] == "A bug"
        assert findings[0]["file_path"] == ""
        assert findings[0]["line_number"] == 0
        assert findings[0]["suggested_fix"] == ""

    def test_parse_map_output_valid(self):
        modules = ExplorerAgent._parse_map_output(MOCK_MAP_OUTPUT)
        assert len(modules) == 2
        assert modules[0]["name"] == "Backend Engine"
        assert len(modules[0]["children"]) == 1

    def test_parse_map_output_no_json(self):
        with pytest.raises(ModelOutputError, match="no JSON object found"):
            ExplorerAgent._parse_map_output("no json here")

    def test_parse_map_output_empty_modules(self):
        with pytest.raises(ModelOutputError, match="no modules found"):
            ExplorerAgent._parse_map_output(json.dumps({"modules": []}))

    def test_parse_map_output_invalid_json(self):
        with pytest.raises(ModelOutputError, match="invalid JSON|no JSON object found"):
            ExplorerAgent._parse_map_output("{bad")


# ═══════════════════════════════════════════════════════════════════════
# 4. PROMPT GENERATION TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestExplorePrompts:
    def test_personalities_structure(self):
        assert len(EXPLORER_PERSONALITIES) == 5
        for key, p in EXPLORER_PERSONALITIES.items():
            assert "name" in p
            assert "category" in p
            assert "focus" in p
            assert "model_preference" in p
            assert p["model_preference"] == "very_complex"

    def test_default_categories(self):
        assert len(DEFAULT_EXPLORE_CATEGORIES) == 5
        assert "performance" in DEFAULT_EXPLORE_CATEGORIES
        assert "concurrency" in DEFAULT_EXPLORE_CATEGORIES

    def test_explorer_prompt_contains_key_info(self):
        prompt = explorer_prompt(
            module_name="Exec",
            module_path="be/src/exec",
            module_description="Execution engine",
            category="performance",
            personality_name="Performance Hunter",
            personality_focus="bottlenecks, copies",
            repo_path="/repo",
        )
        assert "Performance Hunter" in prompt
        assert "be/src/exec" in prompt
        assert "performance" in prompt
        assert "bottlenecks, copies" in prompt
        assert "Execution engine" in prompt

    def test_map_init_prompt_contains_key_info(self):
        prompt = map_init_prompt(repo_path="/repo", max_depth=3)
        assert "/repo" in prompt
        assert "depth 3" in prompt
        assert "modules" in prompt

    def test_personality_category_mapping(self):
        """Each personality has a unique category mapping."""
        categories = [p["category"] for p in EXPLORER_PERSONALITIES.values()]
        assert len(categories) == len(set(categories)), "Duplicate category mappings"


# ═══════════════════════════════════════════════════════════════════════
# 5. ORCHESTRATOR EXPLORATION TESTS
# ═══════════════════════════════════════════════════════════════════════


def _make_orchestrator_config(tmp_path):
    """Build a minimal config dict for Orchestrator instantiation."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "repo:",
                "  path: /fake/repo",
                "  base_branch: master",
                f"  worktree_dir: {tmp_path}",
                "  worktree_hooks: []",
                "opencode:",
                "  planner:",
                "    model: test-planner",
                "    variant: planner-variant",
                "  planner_model: test-planner",
                "  coder_default:",
                "    model: test-coder",
                "    variant: coder-default-variant",
                "  coder_by_complexity:",
                "    simple:",
                "      model: test-coder",
                "      variant: coder-simple-variant",
                "  coder_model_by_complexity:",
                "    simple: test-coder",
                "  coder_model_default: test-coder",
                "  reviewers:",
                "    - model: test-reviewer",
                "      variant: reviewer-variant",
                "  reviewer_models:",
                "    - test-reviewer",
                "  timeout: 60",
                "orchestrator:",
                "  max_parallel_tasks: 2",
                "  max_retries: 1",
                "  poll_interval: 999",
                "explore:",
                "  explorer:",
                "    model: test-explorer",
                "    variant: explorer-variant",
                "  map:",
                "    model: test-map-model",
                "    variant: map-variant",
                "  explorer_model: test-explorer",
                "  map_model: test-map-model",
                "  categories:",
                "    - performance",
                "    - concurrency",
                "  auto_task_severity: major",
                "jira:",
                "  url: https://jira.example",
                "  token: secret-token",
                "  project_key: QA",
                "  epic: QA-100",
                "  issue_type:",
                "    - Bug",
                "    - Task",
                "    - Improvement",
                "  priority:",
                "    - Highest",
                "    - High",
                "    - Medium",
                "  routing_hints:",
                "    - about: planner failures and scheduling",
                "      assignee: planner-owner",
                "      component: query execution",
                "      labels:",
                "        - planner",
                "        - scheduler",
                "    - about: all unmatched items",
                "      assignee: fallback-user",
                "  timeout: 30",
                "  skill_path: skills/jira-issue",
                "database:",
                f"  path: {tmp_path / 'test.db'}",
                "hook_env: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "repo": {
            "path": "/fake/repo",
            "base_branch": "master",
            "worktree_dir": str(tmp_path),
            "worktree_hooks": [],
        },
        "opencode": {
            "config_path": "opencode.json",
            "planner": {"model": "test-planner", "variant": "planner-variant"},
            "planner_model": "test-planner",
            "coder_default": {
                "model": "test-coder",
                "variant": "coder-default-variant",
            },
            "coder_by_complexity": {
                "simple": {"model": "test-coder", "variant": "coder-simple-variant"}
            },
            "coder_model_by_complexity": {"simple": "test-coder"},
            "coder_model_default": "test-coder",
            "reviewers": [{"model": "test-reviewer", "variant": "reviewer-variant"}],
            "reviewer_models": ["test-reviewer"],
            "timeout": 60,
        },
        "orchestrator": {
            "max_parallel_tasks": 2,
            "max_retries": 1,
            "poll_interval": 999,
        },
        "explore": {
            "explorer": {"model": "test-explorer", "variant": "explorer-variant"},
            "map": {"model": "test-map-model", "variant": "map-variant"},
            "explorer_model": "test-explorer",
            "map_model": "test-map-model",
            "categories": ["performance", "concurrency"],
            "auto_task_severity": "major",
        },
        "jira": {
            "url": "https://jira.example",
            "token": "secret-token",
            "project_key": "QA",
            "epic": "QA-100",
            "issue_type": ["Bug", "Task", "Improvement"],
            "priority": ["Highest", "High", "Medium"],
            "routing_hints": [
                {
                    "about": "planner failures and scheduling",
                    "assignee": "planner-owner",
                    "component": "query execution",
                    "labels": ["planner", "scheduler"],
                },
                {
                    "about": "all unmatched items",
                    "assignee": "fallback-user",
                },
            ],
            "timeout": 30,
            "skill_path": "skills/jira-issue",
        },
        "database": {"path": str(tmp_path / "test.db")},
        "hook_env": {},
        "_meta": {"config_path": str(config_path)},
    }


def _mock_agent_run(prompt="", output="", exit_code=0, session_id="ses_test"):
    """Create a mock AgentRun-like object."""
    from core.models import AgentRun

    return AgentRun(
        prompt=prompt,
        output=output,
        exit_code=exit_code,
        session_id=session_id,
        duration_sec=1.0,
    )


class TestOrchestratorExplore:
    @pytest.fixture
    def orch(self, tmp_path):
        config = _make_orchestrator_config(tmp_path)
        with patch("core.orchestrator.OpenCodeClient"):
            from core.orchestrator import Orchestrator

            o = Orchestrator(config)
        return o

    @staticmethod
    def _mark_map_ready(orch):
        orch._explore_map_state["status"] = "done"
        orch._explore_map_state["finished_at"] = time.time()
        orch._persist_explore_map_state()

    def test_get_explore_categories(self, orch):
        cats = orch._get_explore_categories()
        assert cats == ["performance", "concurrency"]

    def test_get_explore_categories_default(self, orch):
        del orch.config["explore"]
        cats = orch._get_explore_categories()
        assert cats == DEFAULT_EXPLORE_CATEGORIES

    def test_get_explorer_model(self, orch):
        assert orch._get_explorer_model() == "test-explorer"

    def test_get_explorer_spec_variant(self, orch):
        spec = orch._explore_service().get_explorer_spec()
        assert spec.model == "test-explorer"
        assert spec.variant == "explorer-variant"

    def test_get_map_spec_variant(self, orch):
        spec = orch._explore_service().get_map_spec()
        assert spec.model == "test-map-model"
        assert spec.variant == "map-variant"

    def test_get_explorer_model_fallback(self, orch):
        del orch.config["explore"]
        assert orch._get_explorer_model() == "test-planner"

    def test_pick_personality_for_category(self, orch):
        key = orch._pick_personality_for_category("performance")
        assert key == "perf_hunter"
        key = orch._pick_personality_for_category("concurrency")
        assert key == "concurrency_auditor"

    def test_pick_personality_for_unknown_category(self, orch):
        key = orch._pick_personality_for_category("unknown_category")
        assert key in EXPLORER_PERSONALITIES

    def test_init_explore_map(self, orch):
        mock_run = _mock_agent_run(output=MOCK_MAP_OUTPUT)
        with (
            patch("core.explore_service.ExplorerAgent") as explorer_cls,
        ):
            explorer_cls.return_value.init_map.return_value = (
                mock_run,
                json.loads(MOCK_MAP_OUTPUT)["modules"],
            )
            result = orch.init_explore_map()

        explorer_cls.assert_called_once_with(
            model="test-map-model",
            variant="map-variant",
            client=orch.client,
        )
        assert "modules_created" in result
        assert result["modules_created"] == 3  # 2 top-level + 1 child

        modules = orch.db.get_all_explore_modules()
        assert len(modules) == 3
        names = {m.name for m in modules}
        assert "Backend Engine" in names
        assert "Exec Module" in names
        assert "Frontend" in names

        # Verify hierarchy
        be = [m for m in modules if m.name == "Backend Engine"][0]
        exec_mod = [m for m in modules if m.name == "Exec Module"][0]
        assert exec_mod.parent_id == be.id
        assert exec_mod.depth == 1

        # Verify category_status initialized
        for m in modules:
            assert set(m.category_status.keys()) == {"performance", "concurrency"}
            for v in m.category_status.values():
                assert v == ExploreStatus.TODO.value

    def test_init_explore_map_clears_old(self, orch):
        orch.db.save_explore_module(ExploreModule(id="old", name="Old", path="old"))
        mock_run = _mock_agent_run(output=MOCK_MAP_OUTPUT)
        with patch.object(
            ExplorerAgent,
            "init_map",
            return_value=(mock_run, json.loads(MOCK_MAP_OUTPUT)["modules"]),
        ):
            orch.init_explore_map()
        modules = orch.db.get_all_explore_modules()
        assert all(m.id != "old" for m in modules)

    def test_run_exploration_passes_variant_to_explorer(self, orch):
        mod = ExploreModule(id="m1", name="Exec", path="be/src/exec")
        orch.db.save_explore_module(mod)

        with patch("core.explore_service.ExplorerAgent") as explorer_cls:
            explorer_cls.return_value.explore_module.return_value = (
                _mock_agent_run(output=MOCK_EXPLORE_OUTPUT),
                json.loads(MOCK_EXPLORE_OUTPUT)["findings"],
                json.loads(MOCK_EXPLORE_OUTPUT)["summary"],
            )
            orch._run_exploration(mod.id, "performance", "perf_hunter", job=None)

        explorer_cls.assert_called_once_with(
            model="test-explorer",
            variant="explorer-variant",
            client=orch.client,
        )

    def test_run_exploration_streaming_passes_variant_to_explorer(self, orch):
        mod = ExploreModule(id="m2", name="Exec", path="be/src/exec")
        orch.db.save_explore_module(mod)
        orch.client = OpenCodeClient(timeout=10)
        job = {
            "job_id": "job1",
            "queue_id": 1,
            "module_id": mod.id,
            "category": "performance",
            "personality_key": "perf_hunter",
            "task_id": f"__explore__:{mod.id}:performance",
            "state": "running",
            "queued_at": time.time(),
            "started_at": time.time(),
            "session_id": "",
            "focus_point": "",
            "resume_with_continue": False,
        }

        with patch("core.explore_service.ExplorerAgent") as explorer_cls:
            explorer_cls.return_value.explore_module_streaming.return_value = (
                _mock_agent_run(output=MOCK_EXPLORE_OUTPUT),
                json.loads(MOCK_EXPLORE_OUTPUT)["findings"],
                json.loads(MOCK_EXPLORE_OUTPUT)["summary"],
            )
            orch._run_exploration(mod.id, "performance", "perf_hunter", job=job)

        explorer_cls.assert_called_once_with(
            model="test-explorer",
            variant="explorer-variant",
            client=orch.client,
        )

    def test_reinitialize_explore_map_resets_explore_metadata_but_keeps_tasks(
        self, orch
    ):
        task = Task(title="Keep me", description="persist")
        orch.db.save_task(task)
        orch.db.save_explore_module(ExploreModule(id="old", name="Old", path="old"))
        orch.db.save_explore_run(
            ExploreRun(
                module_id="old", category="performance", personality="perf", model="m"
            )
        )
        orch.db.save_explore_queue_job(
            {
                "job_id": "job-old",
                "module_id": "old",
                "category": "performance",
                "state": "queued",
            }
        )
        orch.db.save_state(
            orch._explore_map_state_key, {"status": "done", "session_id": "ses_old"}
        )

        mock_run = _mock_agent_run(output=MOCK_MAP_OUTPUT, session_id="ses_new")
        with patch("core.explore_service.ExplorerAgent") as explorer_cls:
            explorer_cls.return_value.init_map_streaming.return_value = (
                mock_run,
                json.loads(MOCK_MAP_OUTPUT)["modules"],
            )
            result = orch.reinitialize_explore_map()

        assert result["accepted"] is True
        assert result["reset"]["tasks_preserved"] is True
        assert orch.db.get_task(task.id) is not None
        assert orch.db.get_explore_module("old") is None
        assert not orch.db.get_all_explore_runs()
        assert not orch.db.get_explore_queue_jobs()
        state = orch.get_explore_init_state()
        assert state["status"] == "in_progress"
        assert state["session_id"] == ""

    def test_init_explore_map_error(self, orch):
        with patch("core.explore_service.ExplorerAgent") as explorer_cls:
            explorer_cls.return_value.init_map.side_effect = RuntimeError("fail")
            result = orch.init_explore_map()
        assert "error" in result

    def test_add_explore_module(self, orch):
        result = orch.add_explore_module(
            name="Test", path="test/path", description="A test"
        )
        assert "id" in result
        assert result["name"] == "Test"
        assert set(result["category_status"].keys()) == {"performance", "concurrency"}

    def test_add_explore_module_with_parent(self, orch):
        parent = orch.add_explore_module(name="Parent", path="p")
        child = orch.add_explore_module(
            name="Child", path="p/c", parent_id=parent["id"]
        )
        assert child["depth"] == 1
        assert child["parent_id"] == parent["id"]

    def test_add_explore_module_bad_parent(self, orch):
        result = orch.add_explore_module(name="X", path="x", parent_id="nonexistent")
        assert "error" in result

    def test_update_explore_module(self, orch):
        mod = orch.add_explore_module(name="A", path="a")
        result = orch.update_explore_module(
            mod["id"], {"name": "B", "description": "Updated"}
        )
        assert result["name"] == "B"
        assert result["description"] == "Updated"

    def test_update_explore_module_category_status(self, orch):
        mod = orch.add_explore_module(name="A", path="a")
        result = orch.update_explore_module(
            mod["id"], {"category_status": {"performance": "done"}}
        )
        assert result["category_status"]["performance"] == "done"

    def test_update_explore_module_not_found(self, orch):
        result = orch.update_explore_module("nonexistent", {"name": "X"})
        assert "error" in result

    def test_delete_explore_module(self, orch):
        mod = orch.add_explore_module(name="A", path="a")
        result = orch.delete_explore_module(mod["id"])
        assert result["deleted"] is True
        assert orch.db.get_explore_module(mod["id"]) is None

    def test_delete_explore_module_cascades(self, orch):
        parent = orch.add_explore_module(name="P", path="p")
        child = orch.add_explore_module(name="C", path="p/c", parent_id=parent["id"])
        orch.delete_explore_module(parent["id"])
        assert orch.db.get_explore_module(parent["id"]) is None
        assert orch.db.get_explore_module(child["id"]) is None

    def test_delete_explore_module_not_found(self, orch):
        result = orch.delete_explore_module("nonexistent")
        assert "error" in result

    def test_start_exploration_selects_leaf_todo_modules(self, orch):
        """start_exploration with no args picks leaf modules with TODO cells."""
        self._mark_map_ready(orch)
        # Create a parent and a child (leaf)
        parent = orch.add_explore_module(name="P", path="p")
        child = orch.add_explore_module(name="C", path="p/c", parent_id=parent["id"])

        # Mock the pool.submit to capture calls instead of actually running
        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))

        result = orch.start_exploration()
        # Only the leaf (child) should be explored, 2 categories
        assert result["started"] == 2
        assert len(submitted) == 2

    def test_start_exploration_with_specific_modules(self, orch):
        self._mark_map_ready(orch)
        mod = orch.add_explore_module(name="A", path="a")
        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))

        result = orch.start_exploration(module_ids=[mod["id"]])
        assert result["started"] == 2  # 2 categories

    def test_start_exploration_focus_point_propagated_to_jobs(self, orch):
        self._mark_map_ready(orch)
        mod = orch.add_explore_module(name="A", path="a")
        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))

        result = orch.start_exploration(
            module_ids=[mod["id"]],
            categories=["performance"],
            focus_point="hash map resize contention",
        )

        assert result["started"] == 1
        assert result["focus_point"] == "hash map resize contention"
        queue = orch.get_exploration_queue_state()
        assert queue["counts"]["running"] == 1
        assert queue["running"][0]["focus_point"] == "hash map resize contention"

    def test_start_exploration_skips_non_todo(self, orch):
        self._mark_map_ready(orch)
        mod_data = orch.add_explore_module(name="A", path="a")
        mod = orch.db.get_explore_module(mod_data["id"])
        mod.category_status["performance"] = ExploreStatus.DONE.value
        orch.db.save_explore_module(mod)

        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))

        result = orch.start_exploration(module_ids=[mod.id])
        assert result["started"] == 2
        assert result["skipped_non_todo"] == 0

    def test_start_exploration_allows_replaying_done_category(self, orch):
        self._mark_map_ready(orch)
        mod_data = orch.add_explore_module(name="A", path="a")
        mod = orch.db.get_explore_module(mod_data["id"])
        mod.category_status["performance"] = ExploreStatus.DONE.value
        mod.category_notes["performance"] = "[2026-01-01] summary: previous pass"
        orch.db.save_explore_module(mod)

        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))

        result = orch.start_exploration(
            module_ids=[mod.id],
            categories=["performance"],
        )

        assert result["started"] == 1
        assert result["skipped_non_todo"] == 0
        fresh = orch.db.get_explore_module(mod.id)
        assert fresh is not None
        assert fresh.category_status["performance"] == ExploreStatus.IN_PROGRESS.value
        assert len(submitted) == 1

    def test_start_exploration_specific_categories(self, orch):
        self._mark_map_ready(orch)
        mod = orch.add_explore_module(name="A", path="a")
        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))

        result = orch.start_exploration(
            module_ids=[mod["id"]], categories=["performance"]
        )
        assert result["started"] == 1

    def test_start_exploration_rejects_duplicate_in_progress(self, orch):
        self._mark_map_ready(orch)
        mod_data = orch.add_explore_module(name="A", path="a")
        mod = orch.db.get_explore_module(mod_data["id"])
        mod.category_status["performance"] = ExploreStatus.IN_PROGRESS.value
        orch.db.save_explore_module(mod)

        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))

        result = orch.start_exploration(
            module_ids=[mod_data["id"]],
            categories=["performance", "concurrency"],
        )
        assert result["started"] == 1
        assert result["rejected_in_progress"] == 1
        assert result["skipped_non_todo"] == 0
        assert len(submitted) == 1

    def test_start_exploration_queues_when_parallel_limit_reached(self, orch):
        self._mark_map_ready(orch)
        orch._explore_parallel_limit = 1
        m1 = orch.add_explore_module(name="A", path="a")
        m2 = orch.add_explore_module(name="B", path="b")

        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))

        result = orch.start_exploration(
            module_ids=[m1["id"], m2["id"]],
            categories=["performance"],
        )
        assert result["started"] == 2
        assert result["running"] == 1
        assert result["queue"]["counts"]["queued"] == 1
        assert len(submitted) == 1

    def test_cancel_exploration_resets_stale_in_progress_cells(self, orch):
        self._mark_map_ready(orch)
        mod_data = orch.add_explore_module(name="A", path="a")
        mod = orch.db.get_explore_module(mod_data["id"])
        mod.category_status["performance"] = ExploreStatus.IN_PROGRESS.value
        orch.db.save_explore_module(mod)

        result = orch.cancel_exploration(
            module_ids=[mod_data["id"]],
            categories=["performance"],
        )

        assert result["cancelled"] == 1
        assert result["reset_stale"] == 1
        updated = orch.db.get_explore_module(mod_data["id"])
        assert updated.category_status["performance"] == ExploreStatus.TODO.value

    def test_recover_stuck_exploration_on_startup(self, tmp_path):
        config = _make_orchestrator_config(tmp_path)
        with patch("core.orchestrator.OpenCodeClient"):
            from core.orchestrator import Orchestrator

            orch = Orchestrator(config)

        mod_data = orch.add_explore_module(name="A", path="a")
        mod = orch.db.get_explore_module(mod_data["id"])
        mod.category_status["performance"] = ExploreStatus.IN_PROGRESS.value
        orch.db.save_explore_module(mod)

        with patch("core.orchestrator.OpenCodeClient"):
            from core.orchestrator import Orchestrator

            restarted = Orchestrator(config)

        recovered = restarted.db.get_explore_module(mod_data["id"])
        assert recovered.category_status["performance"] == ExploreStatus.TODO.value

    def test_start_exploration_rejected_before_map_ready(self, orch):
        result = orch.start_exploration(categories=["performance"])
        assert result["started"] == 0
        assert result["map_ready"] is False
        assert "error" in result

    def test_manual_module_map_is_ready_without_map_init(self, orch):
        mod = orch.add_explore_module(name="A", path="a")
        result = orch.start_exploration(
            module_ids=[mod["id"]], categories=["performance"]
        )
        assert result["started"] == 1
        assert orch.get_explore_status()["map_ready"] is True

    def test_init_map_non_reentrant_and_cancellable(self, orch):
        def _fake_init_map_streaming(
            self,
            repo_path,
            max_depth=2,
            task_id="",
            session_id="",
            message_override=None,
            on_output=None,
            should_cancel=None,
        ):
            if on_output:
                on_output('{"sessionID":"ses_init"}\n', "ses_init")
            for _ in range(30):
                if should_cancel and should_cancel():
                    raise RuntimeError("map init cancelled")
                time.sleep(0.01)
            run = _mock_agent_run(output=MOCK_MAP_OUTPUT, session_id="ses_init")
            return run, json.loads(MOCK_MAP_OUTPUT)["modules"]

        with patch.object(
            ExplorerAgent, "init_map_streaming", new=_fake_init_map_streaming
        ):
            first = orch.start_init_explore_map()
            assert first["accepted"] is True

            second = orch.start_init_explore_map()
            assert second["accepted"] is False

            cancel = orch.cancel_init_explore_map()
            assert cancel["cancel_requested"] is True

            for _ in range(100):
                state = orch.get_explore_init_state()
                if state["status"] != "in_progress":
                    break
                time.sleep(0.01)

            state = orch.get_explore_init_state()
            assert state["status"] == "cancelled"

    def test_recover_exploration_queue_job_with_continue(self, tmp_path):
        from core.orchestrator import Orchestrator

        class _DummyPool:
            def __init__(self):
                self.submitted = []

            def submit(self, fn, *args):
                self.submitted.append((fn, args))
                return MagicMock()

            def shutdown(self, wait=False):
                return None

        config = _make_orchestrator_config(tmp_path)
        with (
            patch("core.orchestrator.OpenCodeClient"),
            patch(
                "core.orchestrator.ThreadPoolExecutor",
                side_effect=lambda max_workers: _DummyPool(),
            ),
        ):
            orch = Orchestrator(config)

        mod_data = orch.add_explore_module(name="A", path="a")
        mod = orch.db.get_explore_module(mod_data["id"])
        mod.category_status["performance"] = ExploreStatus.IN_PROGRESS.value
        orch.db.save_explore_module(mod)
        orch.db.save_explore_queue_job(
            {
                "job_id": "job_resume_1",
                "queue_id": 1,
                "module_id": mod_data["id"],
                "category": "performance",
                "personality_key": "perf_hunter",
                "task_id": f"__explore__:{mod_data['id']}:performance",
                "state": "running",
                "queued_at": time.time() - 10,
                "started_at": time.time() - 9,
                "session_id": "ses_resume",
            }
        )

        with (
            patch("core.orchestrator.OpenCodeClient"),
            patch(
                "core.orchestrator.ThreadPoolExecutor",
                side_effect=lambda max_workers: _DummyPool(),
            ),
        ):
            restarted = Orchestrator(config)

        queue_state = restarted.get_exploration_queue_state()
        assert queue_state["counts"]["running"] == 1
        running_job = next(iter(restarted._explore_running.values()))
        assert running_job["session_id"] == "ses_resume"
        assert running_job["resume_with_continue"] is True


# ═══════════════════════════════════════════════════════════════════════
# 6. FULL END-TO-END EXPLORATION FLOW (mocked model output)
# ═══════════════════════════════════════════════════════════════════════


class TestExplorationFullFlow:
    """Tests the complete exploration pipeline with mocked model I/O."""

    @pytest.fixture
    def orch(self, tmp_path):
        config = _make_orchestrator_config(tmp_path)
        with patch("core.orchestrator.OpenCodeClient"):
            from core.orchestrator import Orchestrator

            o = Orchestrator(config)
        return o

    def test_run_exploration_success_with_findings(self, orch):
        """Full _run_exploration: module → agent → parse → save run → update module → create task."""
        mod_data = orch.add_explore_module(
            name="Exec", path="be/src/exec", description="Execution engine"
        )
        mod_id = mod_data["id"]
        mod = orch.db.get_explore_module(mod_id)
        mod.category_status["performance"] = ExploreStatus.IN_PROGRESS.value
        orch.db.save_explore_module(mod)

        mock_run = _mock_agent_run(
            prompt="test prompt", output=MOCK_EXPLORE_OUTPUT, exit_code=0
        )
        findings = json.loads(MOCK_EXPLORE_OUTPUT)["findings"]
        summary = json.loads(MOCK_EXPLORE_OUTPUT)["summary"]

        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(mock_run, findings, summary),
        ):
            orch._run_exploration(mod_id, "performance", "perf_hunter")

        # Verify module updated
        updated = orch.db.get_explore_module(mod_id)
        assert updated.category_status["performance"] == ExploreStatus.DONE.value
        assert "performance issue" in updated.category_notes["performance"]

        # Verify ExploreRun saved
        runs = orch.db.get_explore_runs_for_module(mod_id)
        assert len(runs) == 1
        assert runs[0].category == "performance"
        assert runs[0].personality == "perf_hunter"
        assert runs[0].issue_count == 2
        assert len(runs[0].findings) == 2

        # Verify auto-task created for major finding (auto_task_severity = major)
        tasks = orch.db.get_all_tasks()
        major_tasks = [t for t in tasks if t.source == TaskSource.EXPLORE]
        assert len(major_tasks) == 1  # only major, not minor
        assert "Unnecessary vector copy" in major_tasks[0].title
        assert major_tasks[0].file_path == "be/src/exec/scanner.cpp"

    def test_run_exploration_no_findings(self, orch):
        """Exploration with no findings: module marked DONE, no tasks created."""
        mod_data = orch.add_explore_module(name="Clean", path="clean")
        mod_id = mod_data["id"]

        mock_run = _mock_agent_run(output=MOCK_EXPLORE_OUTPUT_EMPTY)
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(mock_run, [], "Explored module, no issues found."),
        ):
            orch._run_exploration(mod_id, "performance", "perf_hunter")

        updated = orch.db.get_explore_module(mod_id)
        assert updated.category_status["performance"] == ExploreStatus.DONE.value
        tasks = [t for t in orch.db.get_all_tasks() if t.source == TaskSource.EXPLORE]
        assert len(tasks) == 0

    def test_run_exploration_error_resets_status(self, orch):
        """When exploration fails, module status reverts to TODO."""
        mod_data = orch.add_explore_module(name="Err", path="err")
        mod_id = mod_data["id"]

        with patch.object(
            ExplorerAgent,
            "explore_module",
            side_effect=RuntimeError("model timeout"),
        ):
            orch._run_exploration(mod_id, "performance", "perf_hunter")

        updated = orch.db.get_explore_module(mod_id)
        assert updated.category_status["performance"] == ExploreStatus.TODO.value

    def test_run_exploration_critical_finding_creates_high_priority_task(self, orch):
        """Critical findings create HIGH priority tasks."""
        mod_data = orch.add_explore_module(name="Sec", path="sec")
        mod_id = mod_data["id"]

        critical_findings = [
            {
                "severity": "critical",
                "title": "SQL injection",
                "description": "User input directly interpolated into query",
                "file_path": "sec/query.py",
                "line_number": 10,
                "suggested_fix": "Use parameterized queries",
            }
        ]
        mock_run = _mock_agent_run()
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(mock_run, critical_findings, "Found critical issue"),
        ):
            orch._run_exploration(mod_id, "security", "security_scout")

        tasks = [t for t in orch.db.get_all_tasks() if t.source == TaskSource.EXPLORE]
        assert len(tasks) == 1
        assert tasks[0].priority.value == "high"

    def test_create_task_from_finding(self, orch):
        """Manual task creation from a specific finding in an ExploreRun."""
        mod_data = orch.add_explore_module(name="M", path="m")
        run = ExploreRun(
            module_id=mod_data["id"],
            category="concurrency",
            personality="concurrency_auditor",
            findings=[
                {
                    "severity": "major",
                    "title": "Race condition",
                    "description": "Unsafe read",
                    "file_path": "m/foo.cpp",
                    "line_number": 50,
                    "suggested_fix": "Add lock",
                },
                {
                    "severity": "minor",
                    "title": "Lock granularity",
                    "description": "Too coarse",
                    "file_path": "m/bar.cpp",
                    "line_number": 100,
                    "suggested_fix": "Split lock",
                },
            ],
        )
        orch.db.save_explore_run(run)

        result = orch.create_task_from_finding(run.id, 0)
        assert "id" in result
        assert "Race condition" in result["title"]
        assert result["source"] == "explore"

        result2 = orch.create_task_from_finding(run.id, 1)
        assert "Lock granularity" in result2["title"]

    def test_create_task_from_finding_invalid_index(self, orch):
        run = ExploreRun(id="r1", module_id="m1", category="c", findings=[])
        orch.db.save_explore_run(run)
        result = orch.create_task_from_finding("r1", 0)
        assert "error" in result

    def test_create_task_from_finding_bad_run_id(self, orch):
        result = orch.create_task_from_finding("nonexistent", 0)
        assert "error" in result

    def test_full_pipeline_init_and_explore(self, orch):
        """End-to-end: init map → start exploration → verify results."""
        # 1. Init map
        map_modules = json.loads(MOCK_MAP_OUTPUT)["modules"]
        mock_map_run = _mock_agent_run(output=MOCK_MAP_OUTPUT)
        with patch.object(
            ExplorerAgent, "init_map", return_value=(mock_map_run, map_modules)
        ):
            init_result = orch.init_explore_map()
        assert init_result["modules_created"] == 3

        # 2. Start exploration (synchronous execution for testing)
        explore_findings = json.loads(MOCK_EXPLORE_OUTPUT)["findings"]
        explore_summary = json.loads(MOCK_EXPLORE_OUTPUT)["summary"]
        mock_explore_run = _mock_agent_run(output=MOCK_EXPLORE_OUTPUT)

        # Replace pool.submit with direct execution
        def sync_submit(fn, *args):
            fn(*args)

        orch._pool.submit = sync_submit

        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(mock_explore_run, explore_findings, explore_summary),
        ):
            start_result = orch.start_exploration()

        # Only leaf modules explored: "Exec Module" and "Frontend" (2 leaves × 2 cats = 4)
        assert start_result["started"] == 4

        # Verify all leaf modules are DONE for both categories
        modules = orch.db.get_all_explore_modules()
        leaves = [
            m
            for m in modules
            if m.depth > 0 or not any(c.parent_id == m.id for c in modules)
        ]
        for leaf in leaves:
            for cat in ["performance", "concurrency"]:
                assert leaf.category_status[cat] == ExploreStatus.DONE.value

        # Verify runs created
        all_runs = orch.db.get_all_explore_runs()
        assert len(all_runs) == 4

        # Verify tasks created (each run has 1 major finding → 4 tasks)
        tasks = [t for t in orch.db.get_all_tasks() if t.source == TaskSource.EXPLORE]
        assert len(tasks) == 4

    def test_auto_task_severity_threshold(self, orch):
        """Only findings >= configured severity create auto-tasks."""
        orch.config["explore"]["auto_task_severity"] = "critical"
        mod_data = orch.add_explore_module(name="X", path="x")

        findings = [
            {
                "severity": "major",
                "title": "Major issue",
                "description": "d",
                "file_path": "f",
                "line_number": 1,
                "suggested_fix": "s",
            },
            {
                "severity": "critical",
                "title": "Critical issue",
                "description": "d",
                "file_path": "f",
                "line_number": 2,
                "suggested_fix": "s",
            },
        ]
        mock_run = _mock_agent_run()
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(mock_run, findings, "summary"),
        ):
            orch._run_exploration(mod_data["id"], "performance", "perf_hunter")

        tasks = [t for t in orch.db.get_all_tasks() if t.source == TaskSource.EXPLORE]
        assert len(tasks) == 1
        assert "Critical issue" in tasks[0].title

    def test_auto_task_severity_info_creates_all(self, orch):
        """auto_task_severity=info means all findings create tasks."""
        orch.config["explore"]["auto_task_severity"] = "info"
        mod_data = orch.add_explore_module(name="X", path="x")

        findings = [
            {
                "severity": "info",
                "title": "Info",
                "description": "d",
                "file_path": "f",
                "line_number": 1,
                "suggested_fix": "s",
            },
            {
                "severity": "minor",
                "title": "Minor",
                "description": "d",
                "file_path": "f",
                "line_number": 2,
                "suggested_fix": "s",
            },
        ]
        mock_run = _mock_agent_run()
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(mock_run, findings, "summary"),
        ):
            orch._run_exploration(mod_data["id"], "performance", "perf_hunter")

        tasks = [t for t in orch.db.get_all_tasks() if t.source == TaskSource.EXPLORE]
        assert len(tasks) == 2

    def test_run_exploration_persists_scores_and_adds_visible_note(self, orch):
        mod_data = orch.add_explore_module(name="Exec", path="be/src/exec")
        mod_id = mod_data["id"]

        output = json.dumps(
            {
                "summary": "Checked scanner hot paths",
                "focus_point": "allocator pressure",
                "actionability_score": 8.5,
                "reliability_score": 7.0,
                "explored_scope": "scanner allocator-heavy loops",
                "completion_status": "partial",
                "supplemental_note": "Previous copy issue remains after reset.",
                "map_review_required": False,
                "map_review_reason": "",
                "findings": [],
            }
        )
        mock_run = _mock_agent_run(output=output)
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(mock_run, [], "Checked scanner hot paths"),
        ):
            orch._run_exploration(mod_id, "performance", "perf_hunter")

        runs = orch.db.get_explore_runs_for_module(mod_id)
        assert len(runs) == 1
        assert runs[0].focus_point == "allocator pressure"
        assert runs[0].actionability_score == 8.5
        assert runs[0].reliability_score == 7.0
        assert runs[0].explored_scope == "scanner allocator-heavy loops"
        assert runs[0].completion_status == "partial"
        assert runs[0].supplemental_note.startswith("Previous copy issue")

        updated = orch.db.get_explore_module(mod_id)
        assert updated.category_status["performance"] == ExploreStatus.STALE.value
        note = updated.category_notes["performance"]
        assert "focus: allocator pressure" in note
        assert "actionability: 8.5/10" in note
        assert "reliability: 7.0/10" in note
        assert "explored: scanner allocator-heavy loops" in note
        assert "completion: partial" in note
        assert "summary: Checked scanner hot paths" in note

    def test_run_exploration_map_review_request_triggers_map_init_review(self, orch):
        mod_data = orch.add_explore_module(name="Exec", path="be/src/exec")
        mod_id = mod_data["id"]

        output = json.dumps(
            {
                "summary": "layout mismatch found",
                "focus_point": "module boundary",
                "actionability_score": 6.0,
                "reliability_score": 6.5,
                "supplemental_note": "Consider splitting planner and executor.",
                "map_review_required": True,
                "map_review_reason": "split exec module into planner/executor",
                "findings": [],
            }
        )
        mock_run = _mock_agent_run(output=output)
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(mock_run, [], "layout mismatch found"),
        ):
            with patch.object(
                orch, "start_init_explore_map", return_value={"accepted": True}
            ) as mock_review:
                orch._run_exploration(
                    mod_id, "maintainability", "maintainability_critic"
                )

        mock_review.assert_called_once()
        kwargs = mock_review.call_args.kwargs
        assert "review_reason" in kwargs
        assert "split exec module" in kwargs["review_reason"]

        state = orch.get_explore_init_state()
        assert state["status"] in ("review_required", "done", "in_progress")

    def test_run_exploration_progression_from_todo_to_partial_to_done(self, orch):
        mod_data = orch.add_explore_module(name="Exec", path="be/src/exec")
        mod_id = mod_data["id"]

        partial_output = json.dumps(
            {
                "summary": "Checked scanner hot path only",
                "focus_point": "scanner loop",
                "actionability_score": 7.0,
                "reliability_score": 7.5,
                "explored_scope": "scanner.cpp next_batch and buffer reuse",
                "completion_status": "partial",
                "supplemental_note": "Continue with scheduler interactions next.",
                "map_review_required": False,
                "map_review_reason": "",
                "findings": [],
            }
        )
        partial_run = _mock_agent_run(output=partial_output, session_id="ses_partial")
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(partial_run, [], "Checked scanner hot path only"),
        ):
            orch._run_exploration(mod_id, "performance", "perf_hunter")

        after_partial = orch.db.get_explore_module(mod_id)
        assert after_partial.category_status["performance"] == ExploreStatus.STALE.value
        partial_runs = orch.db.get_explore_runs_for_module(mod_id)
        assert len(partial_runs) == 1
        assert partial_runs[0].completion_status == "partial"
        assert partial_runs[0].session_id == "ses_partial"
        assert (
            partial_runs[0].explored_scope == "scanner.cpp next_batch and buffer reuse"
        )
        assert "completion: partial" in after_partial.category_notes["performance"]

        complete_output = json.dumps(
            {
                "summary": "Covered remaining performance-sensitive paths",
                "focus_point": "scheduler and merge paths",
                "actionability_score": 4.5,
                "reliability_score": 8.0,
                "explored_scope": "scheduler.cpp dispatch flow and merge path buffering",
                "completion_status": "complete",
                "supplemental_note": "Module performance coverage is now complete.",
                "map_review_required": False,
                "map_review_reason": "",
                "findings": [],
            }
        )
        complete_run = _mock_agent_run(
            output=complete_output, session_id="ses_complete"
        )
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(
                complete_run,
                [],
                "Covered remaining performance-sensitive paths",
            ),
        ):
            orch._run_exploration(mod_id, "performance", "perf_hunter")

        after_complete = orch.db.get_explore_module(mod_id)
        assert after_complete.category_status["performance"] == ExploreStatus.DONE.value
        assert (
            after_complete.category_notes["performance"].count("completion: partial")
            == 1
        )
        assert (
            after_complete.category_notes["performance"].count("completion: complete")
            == 1
        )

        all_runs = orch.db.get_explore_runs_for_module(mod_id)
        assert len(all_runs) == 2
        assert all_runs[0].completion_status == "complete"
        assert all_runs[0].session_id == "ses_complete"
        assert all_runs[1].completion_status == "partial"

    def test_run_exploration_done_category_can_be_replayed_and_relogged(self, orch):
        mod_data = orch.add_explore_module(name="Exec", path="be/src/exec")
        mod_id = mod_data["id"]
        orch._explore_map_state["status"] = "done"
        orch._explore_map_state["finished_at"] = time.time()
        orch._persist_explore_map_state()

        first_output = json.dumps(
            {
                "summary": "Covered scanner hot paths",
                "focus_point": "scanner",
                "actionability_score": 3.0,
                "reliability_score": 8.0,
                "explored_scope": "scanner.cpp hot loop",
                "completion_status": "complete",
                "supplemental_note": "Look at merge path only if workload changes.",
                "map_review_required": False,
                "map_review_reason": "",
                "findings": [],
            }
        )
        first_run = _mock_agent_run(output=first_output, session_id="ses_done_1")
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(first_run, [], "Covered scanner hot paths"),
        ):
            orch._run_exploration(mod_id, "performance", "perf_hunter")

        submitted = []
        orch._pool.submit = lambda fn, *a: submitted.append((fn, a))
        replay = orch.start_exploration(module_ids=[mod_id], categories=["performance"])
        assert replay["started"] == 1
        assert replay["skipped_non_todo"] == 0

        second_output = json.dumps(
            {
                "summary": "Rechecked merge path with previous notes in context",
                "focus_point": "merge path",
                "actionability_score": 5.0,
                "reliability_score": 8.5,
                "explored_scope": "merge path buffering",
                "completion_status": "partial",
                "supplemental_note": "Previous scanner pass still valid; spill path remains.",
                "map_review_required": False,
                "map_review_reason": "",
                "findings": [],
            }
        )
        second_run = _mock_agent_run(output=second_output, session_id="ses_done_2")
        with patch.object(
            ExplorerAgent,
            "explore_module",
            return_value=(
                second_run,
                [],
                "Rechecked merge path with previous notes in context",
            ),
        ):
            orch._run_exploration(mod_id, "performance", "perf_hunter")

        saved = orch.db.get_explore_module(mod_id)
        assert saved is not None
        assert saved.category_status["performance"] == ExploreStatus.STALE.value
        note = saved.category_notes["performance"]
        assert "summary: Covered scanner hot paths" in note
        assert "summary: Rechecked merge path with previous notes in context" in note
        runs = orch.db.get_explore_runs_for_module(mod_id)
        assert len(runs) == 2
        assert runs[0].session_id == "ses_done_2"
        assert runs[0].completion_status == "partial"
        assert runs[1].session_id == "ses_done_1"


class TestExploreModuleDetailApi:
    @pytest.fixture
    def orch(self, tmp_path):
        config = _make_orchestrator_config(tmp_path)
        with patch("core.orchestrator.OpenCodeClient"):
            from core.orchestrator import Orchestrator

            o = Orchestrator(config)
        return o

    def test_api_explore_module_detail_includes_parsed_run_metadata(self, orch):
        from web import app as web_app

        mod = orch.add_explore_module(name="Exec", path="be/src/exec")
        real_client = OpenCodeClient(timeout=10)
        orch.client.parse_readable_output = real_client.parse_readable_output
        run_output = (
            '{"sessionID":"ses_api","type":"init"}\n'
            '{"type":"step_start"}\n'
            '{"type":"text","part":{"text":"Exploring scanner and scheduler"}}\n'
            '{"type":"step_finish","part":{"reason":"stop"}}\n'
        )
        run = ExploreRun(
            module_id=mod["id"],
            category="performance",
            personality="perf_hunter",
            model="test-explorer",
            prompt="explore this module carefully",
            output=run_output,
            session_id="ses_api",
            focus_point="scanner and scheduler",
            actionability_score=6.5,
            reliability_score=8.0,
            explored_scope="scanner.cpp and scheduler.cpp core paths",
            completion_status="partial",
            supplemental_note="Continue with merge path.",
            findings=[],
            summary="Explored scanner and scheduler",
            issue_count=0,
            exit_code=0,
            duration_sec=12.3,
        )
        orch.db.save_explore_run(run)

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            result = asyncio.run(web_app.api_explore_module_detail(mod["id"]))
        finally:
            web_app.set_orchestrator(original)

        assert result["module"]["id"] == mod["id"]
        assert len(result["runs"]) == 1
        returned = result["runs"][0]
        assert returned["session_id"] == "ses_api"
        assert returned["prompt"] == "explore this module carefully"
        assert returned["completion_status"] == "partial"
        assert returned["explored_scope"] == "scanner.cpp and scheduler.cpp core paths"
        assert "output" not in returned
        assert returned["parsed"]["session_id"] == "ses_api"
        assert returned["parsed"]["summary"]["total_steps"] == 1
        assert (
            returned["parsed"]["steps"][0]["events"][0]["content"]
            == "Exploring scanner and scheduler"
        )


class TestModelConfigUpdates:
    @pytest.fixture
    def orch(self, tmp_path):
        config = _make_orchestrator_config(tmp_path)
        with patch("core.orchestrator.OpenCodeClient"):
            from core.orchestrator import Orchestrator

            o = Orchestrator(config)
        return o

    def test_update_models_updates_and_persists_explore_models(self, orch):
        with patch.object(orch, "_save_model_config") as save_mock:
            orch.update_models(
                {
                    "planner": {"model": "planner-new", "variant": "planner-v2"},
                    "explorer": {"model": "explorer-new", "variant": "explorer-v2"},
                    "map": {"model": "map-new", "variant": "map-v2"},
                }
            )

        assert orch.config["opencode"]["planner_model"] == "planner-new"
        assert orch.config["opencode"]["planner"]["variant"] == "planner-v2"
        assert orch.config["explore"]["explorer_model"] == "explorer-new"
        assert orch.config["explore"]["explorer"]["variant"] == "explorer-v2"
        assert orch.config["explore"]["map_model"] == "map-new"
        assert orch.config["explore"]["map"]["variant"] == "map-v2"
        assert orch.planner.model == "planner-new"
        assert orch.planner.variant == "planner-v2"
        save_mock.assert_called_once()

    def test_patch_yaml_lines_updates_opencode_and_explore_model_fields(self):
        from core.orchestrator import Orchestrator

        lines = [
            "opencode:\n",
            "  planner: old-planner-spec\n",
            "  planner_model: old-planner\n",
            "  coder_default: old-coder-spec\n",
            "  coder_model_by_complexity:\n",
            "    simple: old-simple\n",
            "  coder_model_default: old-coder\n",
            "  reviewers:\n",
            "  - old-reviewer-spec\n",
            "  reviewer_models:\n",
            "  - old-reviewer\n",
            "explore:\n",
            "  explorer: old-explorer-spec\n",
            "  explorer_model: old-explorer\n",
            "  map: old-map-spec\n",
            "  map_model: old-map\n",
        ]

        patched = Orchestrator._patch_yaml_lines(
            lines,
            {
                "planner": {"model": "new-planner", "variant": "planner-v"},
                "coder_by_complexity": {
                    "simple": {"model": "new-simple", "variant": "simple-v"}
                },
                "coder_default": {"model": "new-coder", "variant": "coder-v"},
                "reviewers": [
                    {"model": "new-reviewer-a", "variant": "reviewer-a-v"},
                    {"model": "new-reviewer-b", "variant": ""},
                ],
            },
            {
                "explorer": {"model": "new-explorer", "variant": "explorer-v"},
                "map": {"model": "new-map", "variant": "map-v"},
            },
        )

        text = "".join(patched)
        assert "planner_model: new-planner" in text
        assert "planner: {model: new-planner, variant: planner-v}" in text
        assert "simple: new-simple" in text
        assert "simple: {model: new-simple, variant: simple-v}" in text
        assert "coder_model_default: new-coder" in text
        assert "coder_default: {model: new-coder, variant: coder-v}" in text
        assert "- {model: new-reviewer-a, variant: reviewer-a-v}" in text
        assert "- new-reviewer-a" in text
        assert "- new-reviewer-b" in text
        assert '- {model: new-reviewer-b, variant: ""}' not in text
        assert "explorer_model: new-explorer" in text
        assert "explorer: {model: new-explorer, variant: explorer-v}" in text
        assert "map_model: new-map" in text
        assert "map: {model: new-map, variant: map-v}" in text

    def test_patch_yaml_lines_does_not_modify_regression_model_profiles(self):
        from core.orchestrator import Orchestrator

        lines = [
            "opencode:\n",
            "  planner: old-planner-spec\n",
            "  planner_model: old-planner\n",
            "  coder_default: old-coder-spec\n",
            "  coder_model_by_complexity:\n",
            "    simple: old-simple\n",
            "  coder_model_default: old-coder\n",
            "  reviewers:\n",
            "  - old-reviewer-spec\n",
            "  reviewer_models:\n",
            "  - old-reviewer\n",
            "explore:\n",
            "  explorer: old-explorer-spec\n",
            "  explorer_model: old-explorer\n",
            "  map: old-map-spec\n",
            "  map_model: old-map\n",
            "regression:\n",
            "  model_profiles:\n",
            "    stable:\n",
            "      planner_model: keep-planner\n",
            "      coder_model_default: keep-coder\n",
            "      coder_model_by_complexity: {}\n",
            "      reviewer_models:\n",
            "        - keep-reviewer\n",
            "      explorer_model: keep-explorer\n",
            "      map_model: keep-map\n",
        ]

        patched = Orchestrator._patch_yaml_lines(
            lines,
            {
                "planner": {"model": "new-planner", "variant": "planner-v"},
                "coder_by_complexity": {
                    "simple": {"model": "new-simple", "variant": "simple-v"}
                },
                "coder_default": {"model": "new-coder", "variant": "coder-v"},
                "reviewers": [
                    {"model": "new-reviewer-a", "variant": "reviewer-a-v"},
                    {"model": "new-reviewer-b", "variant": ""},
                ],
            },
            {
                "explorer": {"model": "new-explorer", "variant": "explorer-v"},
                "map": {"model": "new-map", "variant": "map-v"},
            },
        )

        parsed = yaml.safe_load("".join(patched))
        assert parsed["opencode"]["planner_model"] == "new-planner"
        assert parsed["opencode"]["planner"] == {
            "model": "new-planner",
            "variant": "planner-v",
        }
        assert parsed["opencode"]["coder_model_default"] == "new-coder"
        assert parsed["opencode"]["coder_default"] == {
            "model": "new-coder",
            "variant": "coder-v",
        }
        assert parsed["opencode"]["coder_model_by_complexity"]["simple"] == "new-simple"
        assert parsed["opencode"]["coder_by_complexity"]["simple"] == {
            "model": "new-simple",
            "variant": "simple-v",
        }
        assert parsed["opencode"]["reviewer_models"] == [
            "new-reviewer-a",
            "new-reviewer-b",
        ]
        assert parsed["opencode"]["reviewers"] == [
            {"model": "new-reviewer-a", "variant": "reviewer-a-v"},
            "new-reviewer-b",
        ]
        assert parsed["explore"]["explorer_model"] == "new-explorer"
        assert parsed["explore"]["explorer"] == {
            "model": "new-explorer",
            "variant": "explorer-v",
        }
        assert parsed["explore"]["map_model"] == "new-map"
        assert parsed["explore"]["map"] == {"model": "new-map", "variant": "map-v"}
        assert parsed["regression"]["model_profiles"]["stable"]["planner_model"] == (
            "keep-planner"
        )
        assert (
            parsed["regression"]["model_profiles"]["stable"]["coder_model_default"]
            == "keep-coder"
        )
        assert parsed["regression"]["model_profiles"]["stable"]["reviewer_models"] == [
            "keep-reviewer"
        ]
        assert (
            parsed["regression"]["model_profiles"]["stable"]["explorer_model"]
            == "keep-explorer"
        )
        assert parsed["regression"]["model_profiles"]["stable"]["map_model"] == (
            "keep-map"
        )

    def test_api_config_get_and_post_include_explore_models(self, orch):
        from web import app as web_app

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            before = asyncio.run(web_app.api_config())
            assert before["planner_model"] == "test-planner"
            assert before["planner"]["variant"] == "planner-variant"
            assert before["explorer_model"] == "test-explorer"
            assert before["explorer"]["variant"] == "explorer-variant"
            assert before["map_model"] == "test-map-model"
            assert before["map"]["variant"] == "map-variant"

            request = MagicMock()

            async def _json():
                return {
                    "planner": {"model": "planner-api", "variant": "planner-api-v"},
                    "explorer": {"model": "explorer-api", "variant": "explorer-api-v"},
                    "map": {"model": "map-api", "variant": "map-api-v"},
                }

            request.json = _json
            response = asyncio.run(web_app.api_update_config(request))
            assert response["ok"] is True

            after = asyncio.run(web_app.api_config())
            assert after["planner_model"] == "planner-api"
            assert after["planner"]["variant"] == "planner-api-v"
            assert after["explorer_model"] == "explorer-api"
            assert after["explorer"]["variant"] == "explorer-api-v"
            assert after["map_model"] == "map-api"
            assert after["map"]["variant"] == "map-api-v"
        finally:
            web_app.set_orchestrator(original)

    def test_api_submit_jira_task_validates_and_dispatches(self, orch):
        from web import app as web_app

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            request = MagicMock()

            async def _json():
                return {
                    "title": "Create issue for planner bug",
                    "description": "Planner crashes on malformed task graphs.",
                    "priority": "high",
                    "source_task_id": "task_src_123",
                }

            request.json = _json
            with patch.object(
                orch, "_dispatch_jira_task", return_value=True
            ) as dispatch_mock:
                response = asyncio.run(web_app.api_add_jira_task(request))

            assert response["task_mode"] == "jira"
            saved = orch.db.get_task(response["id"])
            assert saved is not None
            assert saved.task_mode == "jira"
            assert saved.jira_source_task_id == "task_src_123"
            dispatch_mock.assert_called_once_with(saved.id)
        finally:
            web_app.set_orchestrator(original)

    def test_api_submit_jira_task_requires_description(self, orch):
        from web import app as web_app

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            request = MagicMock()

            async def _json():
                return {"title": "Create issue", "description": "   "}

            request.json = _json
            response = asyncio.run(web_app.api_add_jira_task(request))
            assert response.status_code == 400
            assert b"description required" in response.body
        finally:
            web_app.set_orchestrator(original)

    def test_api_dispatch_task_reports_queued_when_parallel_limit_is_full(self, orch):
        from web import app as web_app

        task = Task(title="Queued manual task", description="wait for slot")
        orch.db.save_task(task)
        orch._pending_dispatch = [task.id]

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            with patch.object(
                orch, "dispatch_task", return_value=False
            ) as dispatch_mock:
                response = asyncio.run(web_app.api_dispatch_task(task.id))

            assert response == {"dispatched": False, "queued": True}
            dispatch_mock.assert_called_once_with(task.id)
        finally:
            web_app.set_orchestrator(original)

    def test_api_delete_tasks_requires_ids(self, orch):
        from web import app as web_app

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            request = MagicMock()

            async def _json():
                return {"ids": []}

            request.json = _json
            response = asyncio.run(web_app.api_delete_tasks(request))
            assert response.status_code == 400
            assert b"ids required" in response.body
        finally:
            web_app.set_orchestrator(original)

    def test_api_delete_tasks_returns_400_when_all_deletes_fail(self, orch):
        from web import app as web_app

        task = Task(title="Keep me", description="still referenced")
        child = Task(title="Child", description="x", parent_id=task.id)
        blocked = Task(title="Blocked", description="y", depends_on=[child.id])
        orch.db.save_task(task)
        orch.db.save_task(child)
        orch.db.save_task(blocked)
        orch.client._task_procs = {}

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            request = MagicMock()

            async def _json():
                return {"ids": [task.id]}

            request.json = _json
            with patch.object(
                orch, "_collect_resource_snapshot", return_value=(set(), {})
            ):
                response = asyncio.run(web_app.api_delete_tasks(request))
            assert response.status_code == 400
            assert b"Task is referenced by dependent task" in response.body
        finally:
            web_app.set_orchestrator(original)

    def test_api_delete_tasks_returns_partial_success_payload(self, orch):
        from web import app as web_app

        deletable = Task(
            title="Delete me", description="isolated", status=TaskStatus.FAILED
        )
        blocked = Task(
            title="Keep me", description="still referenced", status=TaskStatus.FAILED
        )
        child = Task(title="Child", description="x", parent_id=blocked.id)
        external = Task(title="External", description="z", depends_on=[child.id])
        orch.db.save_task(deletable)
        orch.db.save_task(blocked)
        orch.db.save_task(child)
        orch.db.save_task(external)
        orch.client._task_procs = {}

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            request = MagicMock()

            async def _json():
                return {"ids": [deletable.id, blocked.id]}

            request.json = _json
            with patch.object(
                orch, "_collect_resource_snapshot", return_value=(set(), {})
            ):
                response = asyncio.run(web_app.api_delete_tasks(request))
            assert response["deleted"] == 1
            assert response["deleted_ids"] == [deletable.id]
            assert (
                "Task is referenced by dependent task" in response["errors"][blocked.id]
            )
        finally:
            web_app.set_orchestrator(original)

    def test_api_delete_tasks_deletes_safe_task_and_related_records(self, orch):
        from web import app as web_app

        task = Task(title="Delete me", description="isolated", status=TaskStatus.FAILED)
        orch.db.save_task(task)
        orch.db.save_agent_run(
            _mock_agent_run(prompt="p", output="o", session_id="ses_delete")
        )
        todo = TodoItem(
            file_path="a.py",
            line_number=1,
            status=TodoItemStatus.DISPATCHED,
            task_id=task.id,
        )
        orch.db.save_todo_item(todo)
        orch.client._task_procs = {}

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            request = MagicMock()

            async def _json():
                return {"ids": [task.id]}

            request.json = _json
            with patch.object(
                orch, "_collect_resource_snapshot", return_value=(set(), {})
            ):
                response = asyncio.run(web_app.api_delete_tasks(request))
            assert response["deleted"] == 1
            assert response["deleted_ids"] == [task.id]
            assert response["errors"] == {}
            assert orch.db.get_task(task.id) is None
            reverted = orch.db.get_todo_item(todo.id)
            assert reverted.status == TodoItemStatus.ANALYZED
            assert reverted.task_id == ""
        finally:
            web_app.set_orchestrator(original)

    def test_assign_jira_for_task_dispatches_in_place_without_creating_new_task(
        self, orch
    ):
        source_task = Task(
            title="Resolve planner bug",
            description="Planner crashes on malformed task graphs.",
        )
        orch.db.save_task(source_task)

        before_ids = {t.id for t in orch.db.get_all_tasks()}
        with patch.object(
            orch, "_dispatch_jira_task", return_value=True
        ) as dispatch_mock:
            result = orch.assign_jira_for_task(source_task.id)

        assert result["ok"] is True
        assert result["task"]["id"] == source_task.id
        after_ids = {t.id for t in orch.db.get_all_tasks()}
        assert after_ids == before_ids
        dispatch_mock.assert_called_once_with(source_task.id)
        saved = orch.db.get_task(source_task.id)
        assert saved is not None
        assert saved.task_mode == "develop"
        assert saved.jira_status == "pending"

    def test_assign_jira_for_task_rolls_back_pending_status_when_dispatch_fails(
        self, orch
    ):
        source_task = Task(
            title="Resolve planner bug",
            description="Planner crashes on malformed task graphs.",
        )
        orch.db.save_task(source_task)

        with patch.object(orch, "_dispatch_jira_task", return_value=False):
            result = orch.assign_jira_for_task(source_task.id)

        assert result == {"error": "Failed to dispatch Jira assignment"}
        saved = orch.db.get_task(source_task.id)
        assert saved is not None
        assert saved.jira_status == ""

    def test_api_assign_jira_for_existing_task_dispatches_in_place(self, orch):
        from web import app as web_app

        source_task = Task(
            title="Resolve planner bug",
            description="Planner crashes on malformed task graphs.",
        )
        orch.db.save_task(source_task)

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            with patch.object(orch, "_dispatch_jira_task", return_value=True):
                response = asyncio.run(web_app.api_assign_jira_for_task(source_task.id))

            assert response["ok"] is True
            assert response["task"]["id"] == source_task.id
            assert len(orch.db.get_all_tasks()) == 1
        finally:
            web_app.set_orchestrator(original)

    def test_assign_jira_for_task_rejects_when_already_assigning(self, orch):
        source_task = Task(
            title="Resolve planner bug",
            description="Planner crashes on malformed task graphs.",
            status=TaskStatus.JIRA_ASSIGNING,
        )
        orch.db.save_task(source_task)

        result = orch.assign_jira_for_task(source_task.id)

        assert result == {"error": "Jira assignment already in progress for this task"}

    def test_get_status_includes_jira_assigning_in_active_tasks(self, orch):
        pending = Task(title="Pending task", description="x", status=TaskStatus.PENDING)
        jira = Task(
            title="Assign jira",
            description="x",
            status=TaskStatus.JIRA_ASSIGNING,
        )
        orch.db.save_task(pending)
        orch.db.save_task(jira)

        status = orch.get_status()

        active_ids = {t["id"] for t in status["active_tasks"]}
        assert jira.id in active_ids
        assert pending.id not in active_ids
        assert status["active_task_count"] == 1

    def test_jira_task_pipeline_syncs_result_back_to_source_task(self, orch):
        source_task = Task(title="Planner issue", description="Planner stalls")
        orch.db.save_task(source_task)
        jira_task = Task(
            title="Planner issue",
            description="Planner stalls",
            task_mode="jira",
            jira_source_task_id=source_task.id,
        )
        orch.db.save_task(jira_task)

        run = _mock_agent_run(
            prompt="jira prompt",
            output="key=QA-321\nself=https://jira.example/rest/api/2/issue/321\ncreated body",
            session_id="ses_jira",
        )
        with patch.object(orch, "_run_jira_agent", return_value=(run, run.output)):
            orch._jira_task_pipeline(jira_task.id)

        saved_source = orch.db.get_task(source_task.id)
        assert saved_source is not None
        assert saved_source.jira_issue_key == "QA-321"
        assert saved_source.jira_issue_url == "https://jira.example/browse/QA-321"
        assert saved_source.jira_status == "created"

    def test_in_place_jira_assignment_success_restores_task_to_pending(self, orch):
        task = Task(
            title="Planner issue",
            description="Planner stalls",
            status=TaskStatus.REVIEW_FAILED,
            review_pass=False,
            review_output="REQUEST_CHANGES\nFix null handling",
            reviewer_results=[
                {
                    "model": "reviewer-a",
                    "passed": False,
                    "output": "Fix null handling",
                }
            ],
            error="Review failed",
            completed_at=123.0,
        )
        orch.db.save_task(task)

        run = _mock_agent_run(
            prompt="jira prompt",
            output="key=QA-321\nself=https://jira.example/rest/api/2/issue/321\ncreated body",
            session_id="ses_jira",
        )
        with patch.object(orch, "_run_jira_agent", return_value=(run, run.output)):
            orch._jira_task_pipeline(task.id)

        saved = orch.db.get_task(task.id)
        assert saved.status == TaskStatus.PENDING
        assert saved.jira_status == "created"
        assert saved.jira_issue_key == "QA-321"
        assert saved.review_pass is False
        assert saved.review_output == "REQUEST_CHANGES\nFix null handling"
        assert saved.reviewer_results == [
            {
                "model": "reviewer-a",
                "passed": False,
                "output": "Fix null handling",
            }
        ]
        assert saved.completed_at == 0.0

    def test_in_place_jira_assignment_failure_restores_prior_status(self, orch):
        task = Task(
            title="Planner issue",
            description="Planner stalls",
            status=TaskStatus.NEEDS_ARBITRATION,
            review_pass=False,
            review_output="REQUEST_CHANGES\nStill broken",
            reviewer_results=[
                {"model": "reviewer-a", "passed": False, "output": "Still broken"}
            ],
            error="Awaiting arbitration",
            completed_at=456.0,
        )
        orch.db.save_task(task)

        run = _mock_agent_run(
            prompt="jira prompt",
            output="unexpected output without issue key",
            session_id="ses_jira",
        )
        with patch.object(orch, "_run_jira_agent", return_value=(run, run.output)):
            orch._jira_task_pipeline(task.id)

        saved = orch.db.get_task(task.id)
        assert saved.status == TaskStatus.FAILED
        assert saved.jira_status == "failed"
        assert saved.review_pass is False
        assert saved.review_output == "REQUEST_CHANGES\nStill broken"
        assert saved.reviewer_results == [
            {"model": "reviewer-a", "passed": False, "output": "Still broken"}
        ]
        assert saved.completed_at == 456.0

    def test_run_jira_agent_uses_simple_coder_model_and_prompt_rules(self, orch):
        task = Task(
            title="Planner issue",
            description="Planner stalls during dispatch.",
            task_mode="jira",
            jira_source_task_id="src-task-777",
        )
        orch.db.save_task(task)

        run = _mock_agent_run(
            prompt="jira prompt",
            output="key=QA-123\nself=https://jira.example/rest/api/2/issue/123",
            session_id="ses_jira",
        )
        orch.client.run.return_value = run
        orch.client.extract_last_text_block_or_raw.return_value = run.output

        agent_run, text = orch._run_jira_agent(task)

        assert agent_run is run
        assert text.startswith("key=QA-123")
        kwargs = orch.client.run.call_args.kwargs
        assert kwargs["model"] == "test-coder"
        assert "planner failures and scheduling" in kwargs["message"]
        assert "all unmatched items" in kwargs["message"]
        assert "required_epic: QA-100" in kwargs["message"]
        assert "Improvement" in kwargs["message"]
        assert "fixed_label:" in kwargs["message"]
        assert "DorisExplorer" in kwargs["message"]
        assert (
            "Choose assignee, extra labels, and optional component strictly from the routing hints."
            in kwargs["message"]
        )
        assert (
            "Every created issue must include the fixed label `DorisExplorer` by passing it explicitly with `--label`."
            in kwargs["message"]
        )
        assert (
            "If the selected routing hint has labels, pass them with `--label`. If it has no labels, do not add any extra routing labels."
            in kwargs["message"]
        )
        assert (
            "If the selected routing hint has a component, pass it via `--component`. If the hint has no component, omit `--component`."
            in kwargs["message"]
        )
        assert (
            "You MUST pass the configured epic with `--epic QA-100`"
            in kwargs["message"]
        )
        assert "Do not omit or change the configured epic." in kwargs["message"]
        assert f"[Doris Agent src-task-777]" in kwargs["message"]
        assert (
            "此jira由赵长乐的agent创建，AI 有误判可能，如有疑问可飞书联系。"
            "如果确认jira问题不存在/无需处理，或者处理完成，请在 http://10.26.20.3:8778 评论对应task。"
            in kwargs["message"]
        )
        assert "skills/jira-issue/SKILL.md" in kwargs["message"]

    def test_parse_jira_agent_result_extracts_issue_key_and_url(self, orch):
        task = Task(title="Planner issue", description="desc", task_mode="jira")
        result = orch._parse_jira_agent_result(
            task,
            "key=QA-123\nself=https://jira.example/rest/api/2/issue/123",
        )
        assert result["key"] == "QA-123"
        assert result["self"].endswith("/123")

    def test_build_jira_browse_url_uses_base_url_and_issue_key(self, orch):
        assert (
            orch._build_jira_browse_url("https://jira.example/", "QA-123")
            == "https://jira.example/browse/QA-123"
        )

    def test_parse_jira_agent_result_rejects_missing_issue_key(self, orch):
        task = Task(title="Planner issue", description="desc", task_mode="jira")
        with pytest.raises(RuntimeError, match="did not return issue key"):
            orch._parse_jira_agent_result(
                task, "self=https://jira.example/rest/api/2/issue/123"
            )

    def test_jira_task_pipeline_completes_and_records_issue(self, orch):
        task = Task(
            title="Planner issue", description="Planner stalls", task_mode="jira"
        )
        orch.db.save_task(task)

        run = _mock_agent_run(
            prompt="jira prompt",
            output="key=QA-321\nself=https://jira.example/rest/api/2/issue/321\ncreated body",
            session_id="ses_jira",
        )
        with patch.object(orch, "_run_jira_agent", return_value=(run, run.output)):
            orch._jira_task_pipeline(task.id)

        saved = orch.db.get_task(task.id)
        assert saved.status == TaskStatus.COMPLETED
        assert saved.jira_issue_key == "QA-321"
        assert saved.jira_issue_url == "https://jira.example/browse/QA-321"
        assert saved.jira_status == "created"
        assert saved.jira_agent_output.startswith("key=QA-321")
        assert saved.jira_payload_preview == ""
        assert saved.code_output == ""
        assert saved.review_output == ""

    def test_jira_task_pipeline_marks_running_with_jira_assigning_status(self, orch):
        task = Task(
            title="Planner issue", description="Planner stalls", task_mode="jira"
        )
        orch.db.save_task(task)

        def _run_agent(_task):
            current = orch.db.get_task(task.id)
            assert current is not None
            assert current.status == TaskStatus.JIRA_ASSIGNING
            assert current.jira_status == "assigning"
            return (
                _mock_agent_run(
                    prompt="jira prompt",
                    output="key=QA-111\nself=https://jira.example/rest/api/2/issue/111",
                    session_id="ses_jira",
                ),
                "key=QA-111\nself=https://jira.example/rest/api/2/issue/111",
            )

        with patch.object(orch, "_run_jira_agent", side_effect=_run_agent):
            orch._jira_task_pipeline(task.id)

        saved = orch.db.get_task(task.id)
        assert saved is not None
        assert saved.status == TaskStatus.COMPLETED
        assert saved.jira_status == "created"

    def test_jira_task_pipeline_failure_marks_task_failed(self, orch):
        task = Task(
            title="Planner issue", description="Planner stalls", task_mode="jira"
        )
        orch.db.save_task(task)

        run = _mock_agent_run(
            prompt="jira prompt",
            output="unexpected output without issue key",
            session_id="ses_jira",
        )
        with patch.object(orch, "_run_jira_agent", return_value=(run, run.output)):
            orch._jira_task_pipeline(task.id)

        saved = orch.db.get_task(task.id)
        assert saved.status == TaskStatus.FAILED
        assert saved.jira_status == "failed"
        assert "did not return issue key" in saved.error
        assert saved.jira_agent_output == "unexpected output without issue key"

    def test_submit_jira_task_initializes_prefix_related_state(self, orch):
        task = orch.submit_jira_task(
            title="Planner issue",
            description="Planner stalls",
            priority="medium",
            source_task_id="source-456",
        )
        saved = orch.db.get_task(task.id)
        assert saved is not None
        assert saved.task_mode == "jira"
        assert saved.jira_source_task_id == "source-456"
        assert saved.jira_status in ("pending", "assigning", "created", "failed")
        assert "epic=QA-100" in saved.plan_output
        assert f"issue types=" in saved.plan_output
        assert "routing hints=" in saved.plan_output

    def test_add_task_comment_persists_and_updates_task(self, orch):
        from web import app as web_app

        task = Task(title="Comment me", description="task")
        orch.db.save_task(task)

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            request = MagicMock()

            async def _json():
                return {"username": "alice", "content": "Please check the edge case."}

            request.json = _json
            with (
                patch.object(
                    orch, "_collect_resource_snapshot", return_value=(set(), {})
                ),
                patch("core.orchestrator.os.path.isdir", return_value=False),
            ):
                response = asyncio.run(web_app.api_add_task_comment(task.id, request))
            assert response["ok"] is True
            assert response["task"]["has_comments"] is True
            assert response["task"]["comment_count"] == 1
            assert response["comments"][0]["username"] == "alice"

            saved = orch.db.get_task(task.id)
            assert saved is not None
            assert len(saved.comments) == 1
            assert saved.comments[0]["content"] == "Please check the edge case."
        finally:
            web_app.set_orchestrator(original)

    def test_api_task_detail_uses_backend_review_verdict_parsing(self, orch):
        from web import app as web_app

        task = Task(title="Review me", description="desc")
        orch.db.save_task(task)
        reviewer_run = _mock_agent_run(
            prompt="review prompt",
            output="APPROVE\nPreviously I would have said REQUEST_CHANGES but it's fixed now.",
            session_id="ses_review_verdict",
        )
        reviewer_run.task_id = task.id
        reviewer_run.agent_type = "reviewer"
        orch.db.save_agent_run(reviewer_run)

        original = web_app.orchestrator
        web_app.set_orchestrator(orch)
        try:
            with patch.object(
                orch, "_collect_resource_snapshot", return_value=(set(), {})
            ):
                orch.client.extract_text_response.return_value = reviewer_run.output
                response = asyncio.run(web_app.api_task_detail(task.id))
            assert response["runs"][0]["review_verdict"] == "approve"
        finally:
            web_app.set_orchestrator(original)
