"""Fixture-repository materialization for isolated regression runs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from regression.helpers.models import RegressionPaths, RegressionWorkspace


class RegressionWorkspaceBuilder:
    """Creates a clean git repository and matching bare origin for each test."""

    def __init__(self, repository_root: Path):
        self.repository_root = Path(repository_root)
        self.fixture_root = self.repository_root / "regression" / "fixtures" / "repos"

    def create(self, fixture_name: str, runtime_root: Path) -> RegressionWorkspace:
        fixture_source = self.fixture_root / fixture_name
        if not fixture_source.is_dir():
            raise FileNotFoundError(
                f"Regression fixture repository not found: {fixture_source}"
            )

        runtime_root = Path(runtime_root)
        paths = RegressionPaths(
            root=runtime_root,
            fixture_source=fixture_source,
            repo=runtime_root / "repo",
            remote=runtime_root / "origin.git",
            worktrees=runtime_root / "worktrees",
            data_dir=runtime_root / "data",
            logs_dir=runtime_root / "logs",
            config_dir=runtime_root / "config",
            config_file=runtime_root / "config" / "regression.yaml",
            pid_file=runtime_root / "data" / "daemon.pid",
        )

        self._copy_fixture_tree(fixture_source, paths.repo)
        paths.worktrees.mkdir(parents=True, exist_ok=True)
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_hook_scripts_are_executable(paths.repo)
        self._initialize_bare_remote(paths.remote)
        self._initialize_working_repository(paths.repo, paths.remote)
        return RegressionWorkspace(fixture_name=fixture_name, paths=paths)

    @staticmethod
    def _copy_fixture_tree(source: Path, destination: Path) -> None:
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
        )

    @staticmethod
    def _ensure_hook_scripts_are_executable(repo_path: Path) -> None:
        hooks_dir = repo_path / "hooks"
        if not hooks_dir.is_dir():
            return
        for path in hooks_dir.rglob("*.sh"):
            mode = path.stat().st_mode
            path.chmod(mode | 0o111)

    def _initialize_bare_remote(self, remote_path: Path) -> None:
        remote_path.parent.mkdir(parents=True, exist_ok=True)
        self._run_git(["init", "--bare", str(remote_path)], cwd=remote_path.parent)

    def _initialize_working_repository(
        self, repo_path: Path, remote_path: Path
    ) -> None:
        self._run_git(["init", "-b", "master"], cwd=repo_path)
        self._run_git(["add", "."], cwd=repo_path)
        self._run_git(
            [
                "-c",
                "user.name=Regression Harness",
                "-c",
                "user.email=regression@example.com",
                "commit",
                "-m",
                "Initial regression fixture repository",
            ],
            cwd=repo_path,
        )
        self._run_git(["remote", "add", "origin", str(remote_path)], cwd=repo_path)
        self._run_git(["push", "-u", "origin", "master"], cwd=repo_path)

    @staticmethod
    def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=120,
            env=os.environ.copy(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Git command failed in regression workspace: "
                f"cwd={cwd} args={args!r}\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return result
