"""Configuration loading and validation."""

import copy
import os
import yaml


DEFAULT_CONFIG = {
    "repo": {
        "path": "/mnt/disk3/zhaochangle/doris",
        "base_branch": "master",
        "worktree_dir": "/mnt/disk3/zhaochangle/multi-agent-todo/worktrees",
    },
    "opencode": {
        "planner_model": "opencode/gpt-5-nano",
        "coder_model": "opencode/gpt-5-nano",
        "reviewer_model": "opencode/gpt-5-nano",
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
}


def load_config(config_path: str = None) -> dict:
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
    config["_meta"] = {"config_path": config_path}
    return config


def _deep_merge(base: dict, override: dict):
    """Recursively merge override into base."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
