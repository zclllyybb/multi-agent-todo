"""Shared data structures for the standalone regression framework."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RegressionSettings:
    """Execution settings resolved from pytest options and environment."""

    enabled: bool
    profile_name: str
    keep_artifacts: bool
    base_config_path: Path
    task_timeout_sec: float
    explore_timeout_sec: float
    poll_interval_sec: float
    daemon_start_timeout_sec: float


@dataclass(frozen=True)
class RegressionModelProfile:
    """Resolved model selection for a regression run."""

    name: str
    planner_model: str
    coder_model_default: str
    coder_model_by_complexity: dict[str, str]
    reviewer_models: list[str]
    explorer_model: str
    map_model: str
    timeout: int


@dataclass(frozen=True)
class RegressionPaths:
    """Filesystem layout for one isolated regression workspace."""

    root: Path
    fixture_source: Path
    repo: Path
    remote: Path
    worktrees: Path
    data_dir: Path
    logs_dir: Path
    config_dir: Path
    config_file: Path
    pid_file: Path


@dataclass
class RegressionWorkspace:
    """Materialized fixture repository plus all isolated runtime directories."""

    fixture_name: str
    paths: RegressionPaths

    def cleanup(self) -> None:
        shutil.rmtree(self.paths.root, ignore_errors=True)
