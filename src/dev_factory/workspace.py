"""Disposable git workspaces. Everything here can be deleted and rebuilt from GitHub.

Layout under <runtime_root>/<project_id>/:
  repo/          clone used only by the orchestrator
  work/<branch>  git worktree the orchestrator commits from
  agent/<branch>-<id>  plain copy without .git that the agent (or verification container)
                       sees, mounted at /workspace; unique per task

The agent never sees a .git directory, so it cannot plant hooks or config that the
orchestrator's own git commands would execute.
"""

import fnmatch
import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Orchestrator git commands never run repository-controlled code.
_SAFE_GIT = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
_GH_CREDENTIALS = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Task:
    branch: str
    worktree: Path
    agent_dir: Path
    base_sha: str


class Workspace:
    def __init__(
        self, root: Path, repository: str, *, author_name: str, author_email: str,
        timeout_seconds: int = 600, remote_url: str | None = None,
    ):
        self.root = root
        self.repository = repository
        self.author = (author_name, author_email)
        self.timeout = timeout_seconds
        self.repo_dir = root / "repo"
        self.remote_url = remote_url or f"https://github.com/{repository}.git"

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        cmd = ["git", *_SAFE_GIT, *_GH_CREDENTIALS, *args]
        try:
            result = subprocess.run(
                cmd, cwd=cwd or self.repo_dir, text=True, capture_output=True,
                timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(f"git {args[0]} timed out") from exc
        if result.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[-500:]}")
        return result.stdout

    def _ensure_clone(self) -> None:
        if (self.repo_dir / ".git").is_dir():
            self._git("fetch", "--prune", "origin")
            return
        shutil.rmtree(self.repo_dir, ignore_errors=True)
        self.repo_dir.parent.mkdir(parents=True, exist_ok=True)
        self._git("clone", "--no-checkout", self.remote_url, str(self.repo_dir),
                  cwd=self.root.parent)

    def _remote_has(self, branch: str) -> bool:
        out = self._git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        return bool(out.strip())

    def prepare(self, branch: str, base_branch: str) -> Task:
        """Fresh worktree on the branch (continuing it if it exists remotely) plus an agent copy."""
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_clone()
        slug = branch.replace("/", "-")
        worktree = self.root / "work" / slug
        agent_dir = self.root / "agent" / f"{slug}-{uuid.uuid4().hex[:8]}"
        if worktree.exists():
            self._git("worktree", "remove", "--force", str(worktree))
        shutil.rmtree(worktree, ignore_errors=True)
        self._git("worktree", "prune")
        start = f"origin/{branch}" if self._remote_has(branch) else f"origin/{base_branch}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--force", "-B", branch, str(worktree), start)
        base_sha = self._git("rev-parse", "HEAD", cwd=worktree).strip()
        shutil.copytree(worktree, agent_dir, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        return Task(branch, worktree, agent_dir, base_sha)

    def collect(self, task: Task) -> list[str]:
        """Mirror the agent copy into the worktree, stage everything, return changed paths."""
        for entry in task.worktree.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        shutil.copytree(task.agent_dir, task.worktree, symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git"))
        self._git("add", "--all", cwd=task.worktree)
        out = self._git("diff", "--cached", "--no-renames", "--name-only", "-z", cwd=task.worktree)
        return [p for p in out.split("\0") if p]

    def leaks(self, task: Task, paths: list[str], secrets: tuple[str, ...]) -> list[str]:
        """Changed files (after collect) that contain any of the secrets."""
        needles = [s.encode() for s in secrets]
        found = []
        for path in paths:
            file = task.worktree / path
            if not needles or file.is_symlink() or not file.is_file():
                continue
            data = file.read_bytes()
            if any(n in data for n in needles):
                found.append(path)
        return found

    def commit_and_push(self, task: Task, message: str) -> str:
        name, email = self.author
        self._git("-c", f"user.name={name}", "-c", f"user.email={email}",
                  "commit", "--no-verify", "-m", message, cwd=task.worktree)
        sha = self._git("rev-parse", "HEAD", cwd=task.worktree).strip()
        # Never force-push: if the remote moved, fail and let the next poll re-observe.
        self._git("push", "origin", f"HEAD:refs/heads/{task.branch}", cwd=task.worktree)
        return sha

    def cleanup(self, task: Task) -> None:
        try:
            self._git("worktree", "remove", "--force", str(task.worktree))
        except WorkspaceError:
            shutil.rmtree(task.worktree, ignore_errors=True)
        shutil.rmtree(task.agent_dir, ignore_errors=True)


def violations(paths: list[str], protected: tuple[str, ...]) -> list[str]:
    return [p for p in paths if any(fnmatch.fnmatch(p, pattern) for pattern in protected)]
