"""Tests for agents/coder.py: _resolve_file_path static method."""

import os
from unittest.mock import MagicMock

import pytest

from agents.coder import CoderAgent
from core.models import AgentRun, Task


class TestResolveFilePath:

    def test_relative_path_exists(self, tmp_path):
        (tmp_path / "src" / "main.py").mkdir(parents=True, exist_ok=True)
        # create actual file
        (tmp_path / "src" / "main.py").rmdir()
        (tmp_path / "src").mkdir(exist_ok=True)
        f = tmp_path / "src" / "main.py"
        f.write_text("code")
        result = CoderAgent._resolve_file_path("src/main.py", str(tmp_path))
        assert result == "src/main.py"

    def test_relative_path_not_exists(self, tmp_path):
        result = CoderAgent._resolve_file_path("nonexistent/file.py", str(tmp_path))
        assert result is None

    def test_empty_path(self, tmp_path):
        assert CoderAgent._resolve_file_path("", str(tmp_path)) is None

    def test_absolute_path_under_worktree(self, tmp_path):
        f = tmp_path / "module" / "util.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("code")
        abs_path = str(f)
        result = CoderAgent._resolve_file_path(abs_path, str(tmp_path))
        assert result == "module/util.py"

    def test_absolute_path_cross_root(self, tmp_path):
        """Absolute path from a different root (e.g. main repo) resolved
        by matching path suffix in the worktree."""
        # Simulate: main repo at /repo/src/main.py, worktree at tmp_path
        # which also has src/main.py
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "main.py").write_text("code")
        # file_path is absolute but from a *different* root
        fake_repo_path = "/completely/different/repo/src/main.py"
        result = CoderAgent._resolve_file_path(fake_repo_path, str(tmp_path))
        assert result == "src/main.py"

    def test_absolute_path_no_match(self, tmp_path):
        """Absolute path that doesn't exist anywhere in the worktree."""
        result = CoderAgent._resolve_file_path("/other/repo/foo.py", str(tmp_path))
        assert result is None


class TestCoderRunDefaults:
    def test_implement_task_uses_shared_default_max_continues(self):
        client = MagicMock()
        client.run.return_value = AgentRun(output="raw")
        client.extract_last_text_block_or_raw.return_value = "done"
        agent = CoderAgent(model="m", client=client)
        task = Task(title="T", description="D")

        agent.implement_task(task, "/repo")

        assert client.run.call_args.kwargs["max_continues"] == agent.default_max_continues
