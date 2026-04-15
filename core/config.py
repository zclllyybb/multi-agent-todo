"""Configuration loading and validation."""

import copy
import os
from typing import Optional
import yaml


DEFAULT_CONFIG = {
    "repo": {
        "path": "/mnt/disk3/zhaochangle/doris",
        "base_branch": "master",
        "worktree_dir": "/mnt/disk3/zhaochangle/multi-agent-todo/worktrees",
    },
    "opencode": {
        "config_path": "opencode.json",
        "planner": {"model": "opencode/gpt-5-nano", "variant": ""},
        "planner_model": "opencode/gpt-5-nano",
        "coder_default": {"model": "opencode/gpt-5-nano", "variant": ""},
        "coder_model": "opencode/gpt-5-nano",
        "coder_by_complexity": {},
        "reviewer_model": "opencode/gpt-5-nano",
        "reviewers": [],
        "timeout": 600,
    },
    "orchestrator": {
        "max_parallel_tasks": 3,
        "max_retries": 4,
        "poll_interval": 30,
        "auto_scan_todos": False,
    },
    "hook_env": {
        "ROOT_WORKSPACE_PATH": "/mnt/disk3/zhaochangle/doris",
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8778,
    },
    "logging": {
        "level": "DEBUG",
        "file": "/mnt/disk3/zhaochangle/multi-agent-todo/logs/agent.log",
    },
    "database": {
        "path": "/mnt/disk3/zhaochangle/multi-agent-todo/data/tasks.db",
    },
    "explore": {
        "explorer": {"model": "", "variant": ""},
        "map": {"model": "", "variant": ""},
    },
    "jira": {
        "url": "",
        "token": "",
        "user": "",
        "project_key": "",
        "epic": "",
        "issue_type": [],
        "priority": [],
        "routing_hints": [],
        "timeout": 120,
        "skill_path": "skills/jira-issue",
    },
    "regression": {
        "default_profile": "stable",
        "dry_run_jira": False,
        "model_profiles": {
            "stable": {
                "planner": {"model": "github-copilot/gpt-5.4", "variant": ""},
                "planner_model": "github-copilot/gpt-5.4",
                "coder_default": {"model": "github-copilot/gpt-5.4", "variant": ""},
                "coder_model_default": "github-copilot/gpt-5.4",
                "coder_by_complexity": {},
                "coder_model_by_complexity": {},
                "reviewers": [{"model": "github-copilot/gpt-5.4", "variant": ""}],
                "reviewer_models": ["github-copilot/gpt-5.4"],
                "explorer": {"model": "github-copilot/gpt-5.4", "variant": ""},
                "explorer_model": "github-copilot/gpt-5.4",
                "map": {"model": "github-copilot/gpt-5.4", "variant": ""},
                "map_model": "github-copilot/gpt-5.4",
                "timeout": 1800,
            },
            "free": {
                "planner": {"model": "opencode/qwen3.6-plus-free", "variant": ""},
                "planner_model": "opencode/qwen3.6-plus-free",
                "coder_default": {"model": "opencode/qwen3.6-plus-free", "variant": ""},
                "coder_model_default": "opencode/qwen3.6-plus-free",
                "coder_by_complexity": {},
                "coder_model_by_complexity": {},
                "reviewers": [{"model": "opencode/qwen3.6-plus-free", "variant": ""}],
                "reviewer_models": ["opencode/qwen3.6-plus-free"],
                "explorer": {"model": "opencode/qwen3.6-plus-free", "variant": ""},
                "explorer_model": "opencode/qwen3.6-plus-free",
                "map": {"model": "opencode/qwen3.6-plus-free", "variant": ""},
                "map_model": "opencode/qwen3.6-plus-free",
                "timeout": 1800,
            },
        },
    },
}


def load_config(config_path: Optional[str] = None) -> dict:
    """Load configuration from YAML file, falling back to defaults."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.yaml",
        )
    if os.path.exists(config_path):
        with open(config_path) as f:
            user_config = yaml.safe_load(f) or {}
        _deep_merge(config, user_config)
    _normalize_model_config(config)
    config["_meta"] = {"config_path": config_path}
    return config


def _deep_merge(base: dict, override: dict):
    """Recursively merge override into base."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _normalize_model_config(config: dict):
    opencode = config.setdefault("opencode", {})
    explore = config.setdefault("explore", {})

    planner = opencode.get("planner")
    if not isinstance(planner, dict):
        opencode["planner"] = {
            "model": str(opencode.get("planner_model", "")).strip(),
            "variant": "",
        }

    coder_default = opencode.get("coder_default")
    if not isinstance(coder_default, dict):
        opencode["coder_default"] = {
            "model": str(
                opencode.get("coder_model_default", opencode.get("coder_model", ""))
            ).strip(),
            "variant": "",
        }

    coder_by_complexity = opencode.get("coder_by_complexity")
    if not isinstance(coder_by_complexity, dict):
        coder_by_complexity = {}
    if not coder_by_complexity and isinstance(
        opencode.get("coder_model_by_complexity"), dict
    ):
        coder_by_complexity = {
            level: {"model": str(model).strip(), "variant": ""}
            for level, model in opencode.get("coder_model_by_complexity", {}).items()
            if str(model).strip()
        }
    opencode["coder_by_complexity"] = coder_by_complexity

    reviewers = opencode.get("reviewers")
    if not isinstance(reviewers, list) or not reviewers:
        reviewers = []
        for model in opencode.get(
            "reviewer_models", [opencode.get("reviewer_model", "")]
        ):
            model = str(model).strip()
            if model:
                reviewers.append({"model": model, "variant": ""})
    opencode["reviewers"] = reviewers

    legacy_explore_variant = str(explore.get("variant", "")).strip()
    explorer_spec = explore.get("explorer")
    if not isinstance(explorer_spec, dict):
        explore["explorer"] = {
            "model": str(explore.get("explorer_model", "")).strip(),
            "variant": legacy_explore_variant,
        }
    elif legacy_explore_variant and not str(explorer_spec.get("variant", "")).strip():
        explorer_spec["variant"] = legacy_explore_variant

    map_spec = explore.get("map")
    if not isinstance(map_spec, dict):
        explore["map"] = {
            "model": str(
                explore.get("map_model", explore.get("explorer_model", ""))
            ).strip(),
            "variant": legacy_explore_variant,
        }
    elif legacy_explore_variant and not str(map_spec.get("variant", "")).strip():
        map_spec["variant"] = legacy_explore_variant
