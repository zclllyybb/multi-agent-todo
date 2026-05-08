"""Runtime config generation for isolated regression executions."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from core.config import DEFAULT_CONFIG, load_config
from regression.helpers.models import RegressionModelProfile, RegressionWorkspace
from regression.helpers.network import allocate_loopback_port


_COMPLEXITY_LEVELS = ("simple", "medium", "complex", "very_complex")


class RegressionConfigFactory:
    """Builds a temporary orchestrator config from a named regression profile."""

    def __init__(self, repository_root: Path, base_config_path: Path):
        self.repository_root = Path(repository_root)
        self.base_config_path = Path(base_config_path)
        self._raw_base_config = self._load_raw_base_config()
        self._base_config = load_config(
            str(self.base_config_path),
            validate_required=False,
        )

    def _load_raw_base_config(self) -> dict:
        if not self.base_config_path.exists():
            return {}
        with self.base_config_path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    @staticmethod
    def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> None:
        for key, value in override.items():
            if isinstance(base.get(key), dict) and isinstance(value, dict):
                RegressionConfigFactory._merge_dicts(base[key], value)
            else:
                base[key] = value

    def resolve_model_profile(self, profile_name: str) -> RegressionModelProfile:
        regression = self._base_config.get("regression", {})
        profiles = regression.get("model_profiles", {})
        selected_name = profile_name or regression.get("default_profile", "free")
        raw_profile = profiles.get(selected_name)
        if not isinstance(raw_profile, dict):
            available = ", ".join(sorted(profiles)) or "<none>"
            raise KeyError(
                f"Regression model profile '{selected_name}' is not defined. "
                f"Available profiles: {available}"
            )

        planner_model = str(raw_profile.get("planner_model", "")).strip()
        coder_model_default = str(
            raw_profile.get("coder_model_default") or planner_model
        ).strip()
        if not planner_model:
            planner_model = coder_model_default
        if not planner_model or not coder_model_default:
            raise ValueError(
                f"Regression profile '{selected_name}' must define planner_model and coder_model_default"
            )

        raw_complexity = raw_profile.get("coder_model_by_complexity", {})
        cleaned_complexity = {}
        if isinstance(raw_complexity, dict):
            for level, model in raw_complexity.items():
                text = str(model).strip()
                if text:
                    cleaned_complexity[str(level)] = text
        for level in _COMPLEXITY_LEVELS:
            cleaned_complexity.setdefault(level, coder_model_default)

        reviewer_models = [
            str(model).strip()
            for model in raw_profile.get("reviewer_models", [])
            if str(model).strip()
        ]
        if not reviewer_models:
            reviewer_models = [coder_model_default]

        explorer_model = str(raw_profile.get("explorer_model") or planner_model).strip()
        map_model = str(raw_profile.get("map_model") or explorer_model).strip()
        timeout = int(
            raw_profile.get(
                "timeout",
                self._base_config.get("opencode", {}).get("timeout", 1800),
            )
        )
        timeout = min(timeout, 300)

        return RegressionModelProfile(
            name=selected_name,
            planner_model=planner_model,
            coder_model_default=coder_model_default,
            coder_model_by_complexity=cleaned_complexity,
            reviewer_models=reviewer_models,
            explorer_model=explorer_model,
            map_model=map_model,
            timeout=timeout,
        )

    def create_runtime_config(
        self,
        workspace: RegressionWorkspace,
        *,
        profile_name: str,
        config_overrides: dict[str, Any] | None = None,
    ) -> tuple[dict, RegressionModelProfile]:
        profile = self.resolve_model_profile(profile_name)
        runtime = copy.deepcopy(DEFAULT_CONFIG)
        daemon_port = allocate_loopback_port()

        runtime["repo"] = {
            "path": str(workspace.paths.repo),
            "base_branch": "master",
            "worktree_dir": str(workspace.paths.worktrees),
            "worktree_hooks": ["hooks/regression_setup.sh"],
        }
        runtime["hook_env"] = {"ROOT_WORKSPACE_PATH": str(workspace.paths.repo)}
        runtime["database"] = {"path": str(workspace.paths.data_dir / "tasks.db")}
        runtime["logging"] = {
            "level": "INFO",
            "file": str(workspace.paths.logs_dir / "regression.log"),
        }
        runtime["web"] = {"host": "127.0.0.1", "port": daemon_port}
        runtime["publish"] = {"remote": "origin"}
        runtime["orchestrator"] = {
            "max_parallel_tasks": 1,
            "max_retries": 1,
            "poll_interval": 1,
            "auto_scan_todos": False,
        }
        runtime["opencode"] = {
            "config_path": str(self.repository_root / "opencode.json"),
            "planner": {"model": profile.planner_model, "variant": ""},
            "planner_model": profile.planner_model,
            "coder_default": {"model": profile.coder_model_default, "variant": ""},
            "coder_model_default": profile.coder_model_default,
            "coder_by_complexity": {
                level: {"model": model, "variant": ""}
                for level, model in profile.coder_model_by_complexity.items()
            },
            "coder_model_by_complexity": dict(profile.coder_model_by_complexity),
            "reviewers": [
                {"model": model, "variant": ""} for model in profile.reviewer_models
            ],
            "reviewer_models": list(profile.reviewer_models),
            "timeout": profile.timeout,
        }
        runtime["explore"] = {
            "explorer": {"model": profile.explorer_model, "variant": ""},
            "explorer_model": profile.explorer_model,
            "map": {"model": profile.map_model, "variant": ""},
            "map_model": profile.map_model,
            "max_parallel_runs": 1,
            "categories": ["maintainability"],
            "auto_task_severity": "major",
        }
        runtime["jira"] = {
            "url": "https://dry-run.invalid",
            "token": "regression-dry-run-token",
            "user": "",
            "project_key": "QA",
            "epic": "QA-1",
            "issue_type": ["Task", "Improvement"],
            "priority": ["Medium", "Low"],
            "routing_hints": [
                {
                    "about": "all unmatched items",
                    "assignee": "regression-bot",
                }
            ],
            "timeout": 120,
            "skill_path": "skills/jira-issue",
        }
        runtime["regression"] = {
            "default_profile": profile.name,
            # Regression must stay non-destructive even when the main config has
            # no explicit regression block.
            "dry_run_jira": bool(
                self._raw_base_config.get("regression", {}).get("dry_run_jira", True)
            ),
            "pid_file": str(workspace.paths.pid_file),
            "model_profiles": self._base_config.get("regression", {}).get(
                "model_profiles", {}
            ),
            "active_profile": profile.name,
            "fixture_name": workspace.fixture_name,
        }

        if config_overrides:
            self._merge_dicts(runtime, copy.deepcopy(config_overrides))

        with workspace.paths.config_file.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(runtime, handle, sort_keys=False)

        loaded = load_config(str(workspace.paths.config_file))
        loaded.setdefault("regression", {})["active_profile"] = profile.name
        loaded["regression"]["fixture_name"] = workspace.fixture_name
        return loaded, profile
