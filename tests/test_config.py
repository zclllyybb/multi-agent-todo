"""Tests for core/config.py: _deep_merge and load_config."""

import os
from pathlib import Path
import yaml
import pytest

from core.config import DEFAULT_CONFIG, _deep_merge, load_config
from regression.helpers.configuration import RegressionConfigFactory
from regression.helpers.models import RegressionPaths, RegressionWorkspace


def _required_config(tmp_path):
    repo = tmp_path / "repo"
    return {
        "repo": {
            "path": str(repo),
            "worktree_dir": str(tmp_path / "worktrees"),
        },
        "hook_env": {"ROOT_WORKSPACE_PATH": str(repo)},
        "logging": {"file": str(tmp_path / "logs" / "agent.log")},
        "database": {"path": str(tmp_path / "data" / "tasks.db")},
    }


class TestDeepMerge:
    def test_flat_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 99, "c": 3}
        _deep_merge(base, override)
        assert base == {"a": 1, "b": 99, "c": 3}

    def test_nested_merge(self):
        base = {"x": {"a": 1, "b": 2}, "y": 10}
        override = {"x": {"b": 99, "c": 3}}
        _deep_merge(base, override)
        assert base == {"x": {"a": 1, "b": 99, "c": 3}, "y": 10}

    def test_override_replaces_non_dict_with_dict(self):
        base = {"x": 42}
        override = {"x": {"nested": True}}
        _deep_merge(base, override)
        assert base == {"x": {"nested": True}}

    def test_override_replaces_dict_with_non_dict(self):
        base = {"x": {"nested": True}}
        override = {"x": "flat_now"}
        _deep_merge(base, override)
        assert base == {"x": "flat_now"}

    def test_empty_override(self):
        base = {"a": 1}
        _deep_merge(base, {})
        assert base == {"a": 1}

    def test_empty_base(self):
        base = {}
        _deep_merge(base, {"a": 1})
        assert base == {"a": 1}

    def test_deeply_nested(self):
        base = {"a": {"b": {"c": {"d": 1}}}}
        override = {"a": {"b": {"c": {"d": 2, "e": 3}}}}
        _deep_merge(base, override)
        assert base == {"a": {"b": {"c": {"d": 2, "e": 3}}}}


class TestLoadConfig:
    def test_default_config_includes_opencode_config_path(self):
        assert DEFAULT_CONFIG["opencode"]["config_path"] == "opencode.json"

    def test_default_config_does_not_include_machine_local_paths(self):
        dumped = yaml.safe_dump(DEFAULT_CONFIG)
        assert "/mnt/disk3/zhaochangle" not in dumped

    def test_loads_yaml_file(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        raw = _required_config(tmp_path)
        _deep_merge(
            raw,
            {
                "web": {"port": 9999},
                "orchestrator": {"max_parallel_tasks": 10},
                "explore": {"variant": "deep-explorer"},
            },
        )
        cfg_file.write_text(yaml.dump(raw))
        config = load_config(str(cfg_file))
        assert config["web"]["port"] == 9999
        assert config["orchestrator"]["max_parallel_tasks"] == 10
        assert config["explore"]["explorer"]["variant"] == "deep-explorer"
        assert config["_meta"]["config_path"] == str(cfg_file)
        # Default keys still present
        assert "repo" in config
        assert "database" in config

    def test_missing_file_uses_defaults(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        config = load_config(str(missing), validate_required=False)
        assert config["orchestrator"]["max_parallel_tasks"] == 3
        assert config["web"]["port"] == 8778
        assert config["explore"]["explorer"]["variant"] == ""
        assert config["opencode"]["config_path"] == "opencode.json"
        assert config["_meta"]["config_path"] == str(missing)

    def test_missing_required_paths_raise_by_default(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"web": {"port": 1234}}))

        with pytest.raises(ValueError, match="repo.path"):
            load_config(str(cfg_file))

    def test_partial_override_keeps_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        raw = _required_config(tmp_path)
        _deep_merge(raw, {"web": {"port": 1234}})
        cfg_file.write_text(yaml.dump(raw))
        config = load_config(str(cfg_file))
        assert config["web"]["port"] == 1234
        assert config["web"]["host"] == "0.0.0.0"  # default preserved

    def test_load_config_discards_explore_categories_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        raw = _required_config(tmp_path)
        _deep_merge(
            raw,
            {
                "explore": {
                    "categories": [
                        "performance",
                        "concurrency",
                        "error_handling",
                        "maintainability",
                    ]
                }
            },
        )
        cfg_file.write_text(yaml.dump(raw))

        config = load_config(str(cfg_file))

        assert "categories" not in config["explore"]

    def test_jira_defaults_and_partial_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        raw = _required_config(tmp_path)
        _deep_merge(
            raw,
            {
                "jira": {
                    "project_key": "QA",
                    "epic": "QA-100",
                    "routing_hints": [
                        {
                            "about": "planner issues",
                            "assignee": "alice",
                            "component": "query execution",
                            "labels": ["planner"],
                        }
                    ],
                }
            },
        )
        cfg_file.write_text(yaml.dump(raw))
        config = load_config(str(cfg_file))
        assert config["jira"]["project_key"] == "QA"
        assert config["jira"]["epic"] == "QA-100"
        assert config["jira"]["issue_type"] == []
        assert config["jira"]["priority"] == []
        assert config["jira"]["skill_path"] == "skills/jira-issue"
        assert config["jira"]["routing_hints"][0]["assignee"] == "alice"
        assert config["jira"]["routing_hints"][0]["component"] == "query execution"

    def test_opencode_config_path_can_be_overridden(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        raw = _required_config(tmp_path)
        _deep_merge(raw, {"opencode": {"config_path": "/tmp/custom-opencode.json"}})
        cfg_file.write_text(yaml.dump(raw))
        config = load_config(str(cfg_file))
        assert config["opencode"]["config_path"] == "/tmp/custom-opencode.json"

    def test_load_config_normalizes_structured_legacy_coder_model_by_complexity(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        raw = _required_config(tmp_path)
        _deep_merge(
            raw,
            {
                "opencode": {
                    "coder_model_by_complexity": {
                        "simple": {
                            "model": "coder-s",
                            "variant": "simple-v",
                            "agent": "coder-simple",
                        },
                        "complex": {
                            "model": "coder-c",
                            "variant": "",
                            "agent": "",
                        },
                    }
                }
            },
        )
        cfg_file.write_text(yaml.dump(raw))

        config = load_config(str(cfg_file))

        assert config["opencode"]["coder_model_by_complexity"] == {
            "simple": "coder-s",
            "complex": "coder-c",
        }
        assert config["opencode"]["coder_by_complexity"] == {
            "simple": {
                "model": "coder-s",
                "variant": "simple-v",
                "agent": "coder-simple",
            },
            "complex": {"model": "coder-c", "variant": "", "agent": ""},
        }


class TestRegressionConfigFactory:
    def test_runtime_config_writes_opencode_config_path(self, tmp_path):
        base_config = tmp_path / "base-config.yaml"
        base_config.write_text(
            yaml.dump(
                {
                    "regression": {
                        "default_profile": "stable",
                        "model_profiles": {
                            "stable": {
                                "planner_model": "planner-model",
                                "coder_model_default": "coder-model",
                                "reviewer_models": ["reviewer-model"],
                                "explorer_model": "explorer-model",
                                "map_model": "map-model",
                                "timeout": 60,
                            }
                        },
                    }
                }
            )
        )

        workspace_root = tmp_path / "workspace"
        paths = RegressionPaths(
            root=workspace_root,
            fixture_source=tmp_path / "fixture-source",
            repo=workspace_root / "repo",
            remote=workspace_root / "remote.git",
            worktrees=workspace_root / "worktrees",
            data_dir=workspace_root / "data",
            logs_dir=workspace_root / "logs",
            config_dir=workspace_root / "config",
            config_file=workspace_root / "config" / "config.yaml",
            pid_file=workspace_root / "run" / "daemon.pid",
        )
        for path in [
            paths.root,
            paths.repo,
            paths.worktrees,
            paths.data_dir,
            paths.logs_dir,
            paths.config_dir,
            paths.pid_file.parent,
        ]:
            Path(path).mkdir(parents=True, exist_ok=True)

        workspace = RegressionWorkspace(fixture_name="fixture", paths=paths)
        factory = RegressionConfigFactory(tmp_path, base_config)

        loaded, _profile = factory.create_runtime_config(
            workspace,
            profile_name="stable",
        )

        assert loaded["opencode"]["config_path"] == str(tmp_path / "opencode.json")
        written = yaml.safe_load(paths.config_file.read_text())
        assert written["opencode"]["config_path"] == str(tmp_path / "opencode.json")
