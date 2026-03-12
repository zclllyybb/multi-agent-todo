"""Git worktree management for parallel task execution."""

import logging
import os
import shutil
import subprocess
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


class WorktreeManager:
    def __init__(self, repo_path: str, worktree_dir: str, base_branch: str = "master",
                 hook_env: Optional[dict] = None):
        self.repo_path = os.path.abspath(repo_path)
        self.worktree_dir = os.path.abspath(worktree_dir)
        self.base_branch = base_branch
        self.hook_env: dict = hook_env or {}
        os.makedirs(self.worktree_dir, exist_ok=True)

    def _run_git(self, *args, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = ["git"] + list(args)
        cwd = cwd or self.repo_path
        log.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd)
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            log.error("Git error: %s", result.stderr.strip())
        return result

    def create_worktree(self, branch_name: str, hooks: Optional[List[str]] = None) -> str:
        """Create a new worktree with a new branch based on base_branch.
        After creation, runs each script in *hooks* (paths relative to the
        worktree root or absolute) inside the worktree directory.
        Returns the worktree path.
        """
        worktree_path = os.path.join(self.worktree_dir, branch_name)
        if os.path.exists(worktree_path):
            log.warning("Worktree already exists: %s", worktree_path)
            return worktree_path

        # Fetch latest
        self._run_git("fetch", "origin", self.base_branch)

        # Create worktree with new branch from origin/base_branch
        result = self._run_git(
            "worktree", "add", "-b", branch_name,
            worktree_path, f"origin/{self.base_branch}"
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create worktree: {result.stderr.strip()}"
            )
        log.info("Created worktree: %s (branch: %s)", worktree_path, branch_name)

        # Copy AGENTS.md and hooks/ from the main repo before running hooks.
        # Neither file is tracked by git, so a fresh worktree checkout does not
        # contain them.  hooks/ must be present before the hook scripts execute.
        agents_md_src = os.path.join(self.repo_path, "AGENTS.md")
        if os.path.exists(agents_md_src):
            shutil.copy2(agents_md_src, os.path.join(worktree_path, "AGENTS.md"))
            log.info("Copied AGENTS.md into worktree: %s", worktree_path)
        else:
            log.warning("AGENTS.md not found in repo root, skipping copy")

        hooks_src = os.path.join(self.repo_path, "hooks")
        if os.path.isdir(hooks_src):
            hooks_dst = os.path.join(worktree_path, "hooks")
            shutil.copytree(hooks_src, hooks_dst, dirs_exist_ok=True)
            log.info("Copied hooks/ into worktree: %s", worktree_path)
        else:
            log.warning("hooks/ not found in repo root, skipping copy")

        if hooks:
            self.run_hooks(hooks, worktree_path)

        return worktree_path

    def run_hooks(self, hooks: List[str], worktree_path: str):
        """Run each hook script inside *worktree_path*.

        Each entry in *hooks* is resolved relative to *worktree_path* when it
        is not an absolute path.  Scripts are executed in order; a non-zero
        exit code raises RuntimeError and aborts the sequence.
        """
        env = os.environ.copy()
        if self.hook_env:
            for k, v in self.hook_env.items():
                env[k] = str(v)
                log.info("Hook env set: %s=%s", k, v)

        for hook in hooks:
            script = hook if os.path.isabs(hook) else os.path.join(worktree_path, hook)
            if not os.path.exists(script):
                log.warning("Hook script not found, skipping: %s", script)
                continue
            log.info("Running hook: %s (cwd=%s)", script, worktree_path)
            try:
                result = subprocess.run(
                    [script], cwd=worktree_path, env=env,
                    capture_output=True, text=True, timeout=600,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"Hook timed out: {script}")
            if result.returncode != 0:
                raise RuntimeError(
                    f"Hook failed (exit {result.returncode}): {script}\n"
                    + result.stderr.strip()
                )
            log.info("Hook completed: %s\n%s", script, result.stdout.strip()[:500])

    def copy_files_into(self, worktree_path: str, file_patterns: List[str]):
        """Copy files/dirs from the main repo into the worktree.

        Each entry in *file_patterns* is a path relative to the repo root.
        Directories are copied recursively.  Missing sources are logged and
        skipped.
        """
        for pat in file_patterns:
            pat = pat.strip()
            if not pat:
                continue
            src = os.path.join(self.repo_path, pat)
            dst = os.path.join(worktree_path, pat)
            if not os.path.exists(src):
                log.warning("copy_files_into: source not found, skipping: %s", src)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            log.info("Copied %s -> %s", src, dst)

    def _find_worktree_path(self, branch_name: str) -> Optional[str]:
        """Look up the actual worktree directory for *branch_name* via git."""
        for wt in self.list_worktrees():
            # wt["branch"] looks like "refs/heads/agent/task-..."
            wt_branch = wt.get("branch", "")
            if wt_branch == f"refs/heads/{branch_name}" or wt_branch == branch_name:
                return wt.get("path")
        return None

    def remove_worktree(self, branch_name: str, worktree_path: str = ""):
        """Remove a worktree and its branch.

        *worktree_path* should be the actual worktree directory (from task DB).
        If empty, falls back to computing from worktree_dir, and also queries
        ``git worktree list`` to find the real path — so a stale config cannot
        cause a silent miss.

        Raises RuntimeError if the worktree directory or branch still exists
        after all cleanup attempts.
        """
        # Build candidate paths: caller-provided, config-derived, git-queried
        candidates: list[str] = []
        if worktree_path:
            candidates.append(worktree_path)
        config_path = os.path.join(self.worktree_dir, branch_name)
        if config_path not in candidates:
            candidates.append(config_path)
        git_path = self._find_worktree_path(branch_name)
        if git_path and git_path not in candidates:
            candidates.append(git_path)

        # Try to remove every candidate that exists on disk
        for path in candidates:
            if not os.path.exists(path):
                continue
            log.info("Removing worktree directory: %s", path)
            result = self._run_git("worktree", "remove", "--force", path)
            if result.returncode != 0:
                log.warning("git worktree remove failed, trying shutil.rmtree: %s", path)
                shutil.rmtree(path)  # let OSError propagate on failure
                self._run_git("worktree", "prune")

        # Verify all candidate directories are actually gone
        for path in candidates:
            if os.path.exists(path):
                raise RuntimeError(
                    f"Worktree directory still exists after cleanup: {path}"
                )

        # Prune any stale worktree entries that git still tracks
        self._run_git("worktree", "prune")

        # Delete the branch
        result = self._run_git("branch", "-D", branch_name)
        # Verify branch is gone (ignore if it was already absent)
        check = self._run_git("rev-parse", "--verify", f"refs/heads/{branch_name}")
        if check.returncode == 0:
            raise RuntimeError(
                f"Branch still exists after deletion attempt: {branch_name}"
            )
        log.info("Removed worktree and branch: %s", branch_name)

    def list_worktrees(self) -> List[dict]:
        """List all worktrees."""
        result = self._run_git("worktree", "list", "--porcelain")
        worktrees = []
        current = {}
        for line in result.stdout.strip().split("\n"):
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("HEAD "):
                current["head"] = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1]
            elif line == "bare":
                current["bare"] = True
        if current:
            worktrees.append(current)
        return worktrees

    def get_diff(self, worktree_path: str) -> str:
        """Get the diff of changes in a worktree."""
        result = self._run_git("diff", "HEAD", cwd=worktree_path)
        return result.stdout

    def get_changed_files(self, worktree_path: str) -> List[str]:
        """Get list of changed files in a worktree."""
        result = self._run_git(
            "diff", "--name-only", "HEAD", cwd=worktree_path
        )
        return [f for f in result.stdout.strip().split("\n") if f]

    def get_git_status(self, worktree_path: str) -> dict:
        """Return structured git status for a worktree.

        Returns dict with:
          branch: current branch name
          staged: list of staged file paths
          unstaged: list of unstaged modified file paths
          untracked: list of untracked file paths
          ahead: number of commits ahead of remote
          raw: full 'git status --short' output
        """
        if not worktree_path or not os.path.isdir(worktree_path):
            return {"error": "worktree path does not exist", "raw": ""}

        status_result = self._run_git("status", "--short", "--branch", cwd=worktree_path)
        raw = status_result.stdout

        # Parse branch line: ## branch...remote [ahead N] [behind N]
        branch = ""
        ahead = 0
        staged = []
        unstaged = []
        untracked = []

        for line in raw.splitlines():
            if line.startswith("## "):
                branch_info = line[3:]
                # strip remote tracking info
                branch = branch_info.split("...")[0].split(" ")[0]
                if "ahead" in branch_info:
                    import re as _re
                    m = _re.search(r"ahead (\d+)", branch_info)
                    if m:
                        ahead = int(m.group(1))
                continue
            if len(line) < 2:
                continue
            xy = line[:2]
            path = line[3:]
            x, y = xy[0], xy[1]
            if x == "?":
                untracked.append(path)
            else:
                if x != " ":
                    staged.append(path)
                if y != " ":
                    unstaged.append(path)

        return {
            "branch": branch,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "ahead": ahead,
            "raw": raw,
        }

    def publish_branch(self, branch_name: str, remote: str = "origin") -> Tuple[bool, str]:
        """Push a branch to the given remote.
        Returns (success, message).
        """
        result = self._run_git(
            "push", "--force", "--set-upstream", remote, branch_name
        )
        if result.returncode == 0:
            msg = result.stdout.strip() or result.stderr.strip()
            log.info("Published branch %s to %s: %s", branch_name, remote, msg)
            return True, msg
        err = result.stderr.strip()
        log.error("Failed to publish branch %s to %s: %s", branch_name, remote, err)
        return False, err
