"""Configuration loading and validation."""

import copy
import os
from typing import Optional
import yaml

from core.model_config import (
    model_spec_list_to_dict,
    model_spec_list_to_config_value,
    model_spec_list_to_model_list,
    model_spec_map_to_dict,
    model_spec_map_to_config_value,
    model_spec_map_to_model_map,
    model_spec_to_dict,
    model_spec_to_config_value,
    parse_model_spec,
    parse_model_spec_list,
    parse_model_spec_map,
)


DEFAULT_CONFIG = {
    "repo": {
        "path": "/mnt/disk3/zhaochangle/doris",
        "base_branch": "master",
        "worktree_dir": "/mnt/disk3/zhaochangle/multi-agent-todo/worktrees",
    },
    "opencode": {
        "config_path": "opencode.json",
        "planner": {"model": "opencode/gpt-5-nano", "variant": "", "agent": ""},
        "planner_model": "opencode/gpt-5-nano",
        "coder_default": {"model": "opencode/gpt-5-nano", "variant": "", "agent": ""},
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
        "explorer": {"model": "", "variant": "", "agent": ""},
        "map": {"model": "", "variant": "", "agent": ""},
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
                "planner": {"model": "github-copilot/gpt-5.4", "variant": "", "agent": ""},
                "planner_model": "github-copilot/gpt-5.4",
                "coder_default": {"model": "github-copilot/gpt-5.4", "variant": "", "agent": ""},
                "coder_model_default": "github-copilot/gpt-5.4",
                "coder_by_complexity": {},
                "coder_model_by_complexity": {},
                "reviewers": [{"model": "github-copilot/gpt-5.4", "variant": "", "agent": ""}],
                "reviewer_models": ["github-copilot/gpt-5.4"],
                "explorer": {"model": "github-copilot/gpt-5.4", "variant": "", "agent": ""},
                "explorer_model": "github-copilot/gpt-5.4",
                "map": {"model": "github-copilot/gpt-5.4", "variant": "", "agent": ""},
                "map_model": "github-copilot/gpt-5.4",
                "timeout": 1800,
            },
            "free": {
                "planner": {"model": "opencode/qwen3.6-plus-free", "variant": "", "agent": ""},
                "planner_model": "opencode/qwen3.6-plus-free",
                "coder_default": {"model": "opencode/qwen3.6-plus-free", "variant": "", "agent": ""},
                "coder_model_default": "opencode/qwen3.6-plus-free",
                "coder_by_complexity": {},
                "coder_model_by_complexity": {},
                "reviewers": [{"model": "opencode/qwen3.6-plus-free", "variant": "", "agent": ""}],
                "reviewer_models": ["opencode/qwen3.6-plus-free"],
                "explorer": {"model": "opencode/qwen3.6-plus-free", "variant": "", "agent": ""},
                "explorer_model": "opencode/qwen3.6-plus-free",
                "map": {"model": "opencode/qwen3.6-plus-free", "variant": "", "agent": ""},
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

    planner_spec = parse_model_spec(opencode.get("planner", opencode.get("planner_model", "")))
    opencode["planner"] = model_spec_to_dict(planner_spec)
    opencode["planner_model"] = planner_spec.model

    default_coder_spec = parse_model_spec(
        opencode.get(
            "coder_default",
            opencode.get("coder_model_default", opencode.get("coder_model", "")),
        )
    )
    opencode["coder_default"] = model_spec_to_dict(default_coder_spec)
    opencode["coder_model_default"] = default_coder_spec.model

    coder_specs = parse_model_spec_map(opencode.get("coder_by_complexity"))
    if not coder_specs:
        coder_specs = parse_model_spec_map(opencode.get("coder_model_by_complexity"))
    opencode["coder_by_complexity"] = model_spec_map_to_dict(coder_specs)
    opencode["coder_model_by_complexity"] = model_spec_map_to_model_map(coder_specs)

    reviewer_specs = parse_model_spec_list(opencode.get("reviewers"))
    if not reviewer_specs:
        reviewer_specs = parse_model_spec_list(
            opencode.get("reviewer_models", [opencode.get("reviewer_model", "")])
        )
    opencode["reviewers"] = model_spec_list_to_dict(reviewer_specs)
    opencode["reviewer_models"] = model_spec_list_to_model_list(reviewer_specs)

    legacy_explore_variant = str(explore.get("variant", "")).strip()
    explorer_spec = parse_model_spec(
        explore.get("explorer", explore.get("explorer_model", ""))
    )
    if legacy_explore_variant and not explorer_spec.variant:
        explorer_spec = explorer_spec.__class__(
            model=explorer_spec.model,
            variant=legacy_explore_variant,
        )
    explore["explorer"] = model_spec_to_dict(explorer_spec)
    explore["explorer_model"] = explorer_spec.model

    map_spec = parse_model_spec(explore.get("map", explore.get("map_model", "")))
    if not map_spec.is_set:
        map_spec = parse_model_spec(explore.get("explorer_model", ""))
    if legacy_explore_variant and not map_spec.variant:
        map_spec = map_spec.__class__(model=map_spec.model, variant=legacy_explore_variant)
    explore["map"] = model_spec_to_dict(map_spec)
    explore["map_model"] = map_spec.model
