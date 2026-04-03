"""Tests for core/config.py: _deep_merge and load_config."""

import os
import yaml
import pytest

from core.config import _deep_merge, load_config


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
    def test_loads_yaml_file(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.dump(
                {
                    "web": {"port": 9999},
                    "orchestrator": {"max_parallel_tasks": 10},
                }
            )
        )
        config = load_config(str(cfg_file))
        assert config["web"]["port"] == 9999
        assert config["orchestrator"]["max_parallel_tasks"] == 10
        assert config["_meta"]["config_path"] == str(cfg_file)
        # Default keys still present
        assert "repo" in config
        assert "database" in config

    def test_missing_file_uses_defaults(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        config = load_config(str(missing))
        assert config["orchestrator"]["max_parallel_tasks"] == 3
        assert config["web"]["port"] == 8778
        assert config["_meta"]["config_path"] == str(missing)

    def test_partial_override_keeps_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"web": {"port": 1234}}))
        config = load_config(str(cfg_file))
        assert config["web"]["port"] == 1234
        assert config["web"]["host"] == "0.0.0.0"  # default preserved

    def test_jira_defaults_and_partial_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.dump(
                {
                    "jira": {
                        "project_key": "QA",
                        "routing_hints": [
                            {
                                "about": "planner issues",
                                "assignee": "alice",
                                "component": "query execution",
                                "labels": ["planner"],
                            }
                        ],
                    }
                }
            )
        )
        config = load_config(str(cfg_file))
        assert config["jira"]["project_key"] == "QA"
        assert config["jira"]["issue_type"] == []
        assert config["jira"]["priority"] == []
        assert config["jira"]["skill_path"] == "skills/jira-issue"
        assert config["jira"]["routing_hints"][0]["assignee"] == "alice"
        assert config["jira"]["routing_hints"][0]["component"] == "query execution"
