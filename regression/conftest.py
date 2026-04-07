"""Pytest integration for the standalone regression framework."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from regression.helpers import (
    RegressionConfigFactory,
    RegressionHarness,
    RegressionSettings,
    RegressionWorkspaceBuilder,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
collect_ignore_glob = ["fixtures/**"]


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("regression")
    group.addoption(
        "--run-regression",
        action="store_true",
        help="Run the real regression suite that invokes actual models and git worktrees.",
    )
    group.addoption(
        "--regression-profile",
        action="store",
        default="",
        help="Regression model profile name. Defaults to the configured regression.default_profile.",
    )
    group.addoption(
        "--regression-base-config",
        action="store",
        default="",
        help="Path to the base config file used to resolve regression model profiles.",
    )
    group.addoption(
        "--regression-keep-artifacts",
        action="store_true",
        help="Keep generated runtime repos, db files, and logs after the test run.",
    )
    group.addoption(
        "--regression-task-timeout",
        action="store",
        default="1800",
        help="Default timeout in seconds for task pipeline regressions.",
    )
    group.addoption(
        "--regression-explore-timeout",
        action="store",
        default="1200",
        help="Default timeout in seconds for explore regressions.",
    )
    group.addoption(
        "--regression-poll-interval",
        action="store",
        default="1.0",
        help="Polling interval in seconds for regression waiters.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "regression: real model-driven regression tests with isolated repositories",
    )


@pytest.fixture(scope="session")
def regression_settings(pytestconfig: pytest.Config) -> RegressionSettings:
    env_enabled = os.getenv("REGRESSION_ENABLE", "").strip() == "1"
    env_keep = os.getenv("REGRESSION_KEEP_ARTIFACTS", "").strip() == "1"
    base_config_raw = (
        pytestconfig.getoption("--regression-base-config")
        or os.getenv("REGRESSION_BASE_CONFIG", "")
        or str(REPO_ROOT / "config.yaml")
    )
    profile_name = (
        pytestconfig.getoption("--regression-profile")
        or os.getenv("REGRESSION_PROFILE", "")
        or ""
    )
    return RegressionSettings(
        enabled=bool(pytestconfig.getoption("--run-regression") or env_enabled),
        profile_name=profile_name,
        keep_artifacts=bool(
            pytestconfig.getoption("--regression-keep-artifacts") or env_keep
        ),
        base_config_path=Path(base_config_raw),
        task_timeout_sec=float(pytestconfig.getoption("--regression-task-timeout")),
        explore_timeout_sec=float(
            pytestconfig.getoption("--regression-explore-timeout")
        ),
        poll_interval_sec=float(pytestconfig.getoption("--regression-poll-interval")),
        daemon_start_timeout_sec=30.0,
    )


@pytest.fixture(autouse=True)
def _require_explicit_regression_enable(regression_settings: RegressionSettings):
    if not regression_settings.enabled:
        pytest.skip(
            "Regression suite is disabled. Use --run-regression or REGRESSION_ENABLE=1 to execute it."
        )


@pytest.fixture
def regression_workspace_factory(
    regression_settings: RegressionSettings,
):
    builder = RegressionWorkspaceBuilder(REPO_ROOT)
    workspaces = []
    runtime_parent = REPO_ROOT / "regression" / ".runtime"
    runtime_parent.mkdir(parents=True, exist_ok=True)

    def _make(fixture_name: str = "tiny_python_app"):
        runtime_root = runtime_parent / (f"{fixture_name}-{uuid.uuid4().hex[:12]}")
        if runtime_root.exists():
            shutil.rmtree(runtime_root)
        workspace = builder.create(fixture_name=fixture_name, runtime_root=runtime_root)
        workspaces.append(workspace)
        return workspace

    yield _make

    if not regression_settings.keep_artifacts:
        for workspace in workspaces:
            workspace.cleanup()
        shutil.rmtree(runtime_parent, ignore_errors=True)


@pytest.fixture
def regression_harness_factory(
    regression_settings: RegressionSettings,
    regression_workspace_factory,
):
    config_factory = RegressionConfigFactory(
        REPO_ROOT, regression_settings.base_config_path
    )
    harnesses = []

    def _make(
        fixture_name: str = "tiny_python_app",
        *,
        config_overrides: dict | None = None,
    ):
        workspace = regression_workspace_factory(fixture_name)
        return _make_with_workspace(workspace, config_overrides=config_overrides)

    def _make_with_workspace(
        workspace,
        *,
        config_overrides: dict | None = None,
    ):
        harness = RegressionHarness.create(
            workspace=workspace,
            settings=regression_settings,
            config_factory=config_factory,
            config_overrides=config_overrides,
        )
        harnesses.append(harness)
        return harness

    yield _make

    for harness in reversed(harnesses):
        harness.close()
