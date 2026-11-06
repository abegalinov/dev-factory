"""In-memory GitHub, workspace, agent, and clock for integration tests.

FakeGitHub implements the subset of GitHubCLI the factory uses, plus helpers that act
as humans and CI. `crash_on` raises Crash (a BaseException, like process death) on the
Nth call of a method, so tests can kill the orchestrator between any two side effects.
"""

import itertools
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dev_factory.claude_cli import AgentResult
from dev_factory.container import ContainerResult
from dev_factory.engine import ReleaseState, ReleaseStatus
from dev_factory.github_cli import GitHubCLIError
from dev_factory.labels import catalog
from dev_factory.models import (
    Check,
    Comment,
    Issue,
    Label,
    LabelEvent,
    PullRequest,
    Review,
    User,
)
from dev_factory.state_machine import DEFAULT_LABELS
from dev_factory.workspace import Task

FACTORY = "factory-bot"
MAINTAINER = "maintainer"
OUTSIDER = "outsider"
REPO = "acme/app"
SECRET = "sk-agent-token-0123456789"


class Crash(BaseException):  # noqa: N818 - simulates process death, not an error
    pass


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)

    def tick(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@dataclass
class _Issue:
    number: int
    title: str
    body: str
    author: str
    labels: list[str]
    state: str = "open"
    updated_at: datetime | None = None


@dataclass
class _Pull:
    number: int
    head_ref: str
    head_sha: str
    base_ref: str
    state: str = "open"
    merged: bool = False
    merged_by: str | None = None
    merge_commit_sha: str | None = None


class FakeGitHub:
    def __init__(self, clock: Clock, *, labels: bool = True):
        self.clock = clock
        self.user = User(FACTORY, 1)
        self.permissions = {FACTORY: "write", MAINTAINER: "write", OUTSIDER: "read"}
        self.labels: list[Label] = catalog(DEFAULT_LABELS) if labels else []
        self.issues: dict[int, _Issue] = {}
        self.events: dict[int, list[LabelEvent]] = defaultdict(list)
        self.comments: dict[int, list[Comment]] = defaultdict(list)
        self.pulls: dict[int, _Pull] = {}
        self.reviews: dict[int, list[Review]] = defaultdict(list)
        self.feedback: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
        self.discussion: dict[int, list[Comment]] = defaultdict(list)  # PR reviews, inline
        self.branches: dict[str, str] = {"main": "sha-main-0"}
        self.files: dict[str, set[str]] = defaultdict(set)  # branch -> paths
        self.ci_checks: dict[str, list[Check]] = {}
        self.job_logs: dict[int, str] = {}
        self.pull_bodies: dict[int, str] = {}
        self.contents: dict[tuple[str, str], str] = {}  # (ref, path) -> text
        self.commit_dates: dict[str, datetime] = {}
        self.artifacts: dict[str, dict[str, bytes]] = {}  # head sha -> demo-video files
        self.assets: dict[str, bytes] = {}  # factory-evidence asset name -> content
        self.artifact_error: str | None = None
        self._runs: dict[int, str] = {}  # artifact run id -> head sha
        self.calls: list[str] = []
        self._numbers = itertools.count(1)
        self._ids = itertools.count(1000)
        self._shas = itertools.count(1)
        self._crash: dict[str, int] = {}

    # -- test controls ---------------------------------------------------------

    def crash_on(self, method: str, nth: int = 1) -> None:
        self._crash[method] = nth

    def _record(self, method: str) -> None:
        self.calls.append(method)
        if method in self._crash:
            self._crash[method] -= 1
            if self._crash[method] == 0:
                del self._crash[method]
                raise Crash(method)

    def count(self, method: str) -> int:
        return self.calls.count(method)

    def new_sha(self) -> str:
        sha = f"sha-{next(self._shas)}"
        self.commit_dates[sha] = self.clock()
        return sha

    # -- human / CI helpers ------------------------------------------------------

    def open_issue(self, title: str = "Add dark mode", body: str = "Please add it.",
                   author: str = OUTSIDER) -> int:
        n = next(self._numbers)
        self.issues[n] = _Issue(n, title, body, author, [], updated_at=self.clock.tick())
        return n

    def label(self, n: int, label: str, actor: str = MAINTAINER) -> None:
        self.issues[n].labels.append(label)
        self.events[n].append(LabelEvent("labeled", label, actor, self.clock.tick()))

    def relabel(self, n: int, label: str, actor: str = MAINTAINER) -> None:
        """Replace lifecycle labels the way a human does in the UI."""
        lifecycle = set(DEFAULT_LABELS.values())
        for old in [x for x in self.issues[n].labels if x in lifecycle]:
            self.issues[n].labels.remove(old)
            self.events[n].append(LabelEvent("unlabeled", old, actor, self.clock.tick()))
        self.label(n, label, actor)

    def human_comment(self, n: int, body: str, author: str = MAINTAINER) -> None:
        now = self.clock.tick()
        self.comments[n].append(Comment(next(self._ids), author, body, now, now))

    def merge(self, pr: int, by: str = MAINTAINER) -> str:
        p = self.pulls[pr]
        sha = self.new_sha()
        p.state, p.merged, p.merged_by, p.merge_commit_sha = "closed", True, by, sha
        self.branches[p.base_ref] = sha
        self.files[p.base_ref] |= self.files[p.head_ref]
        return sha

    def close_pull(self, pr: int) -> None:
        self.pulls[pr].state = "closed"

    def request_changes(self, pr: int, text: str = "Please change X.",
                        by: str = MAINTAINER) -> None:
        head = self.pulls[pr].head_sha
        now = self.clock.tick()
        self.reviews[pr].append(Review(by, "CHANGES_REQUESTED", head, now))
        self.feedback[pr].append((by, head, text))
        self.discussion[pr].append(Comment(next(self._ids), by,
                                           f"(PR #{pr}, review: changes requested) {text}",
                                           now, now))

    def push_human_commit(self, pr: int) -> str:
        sha = self.new_sha()
        self.pulls[pr].head_sha = sha
        self.branches[self.pulls[pr].head_ref] = sha
        return sha

    def ci(
        self, sha: str, conclusion: str | None = "success", name: str = "test",
        log: str | None = None,
    ) -> None:
        """One CI check; with log it is a GitHub Actions job whose log can be fetched."""
        status = "completed" if conclusion else "in_progress"
        job_id = None
        if log is not None:
            job_id = len(self.job_logs) + 1
            self.job_logs[job_id] = log
        self.ci_checks[sha] = [Check(name, status, conclusion, "", job_id=job_id)]

    def lifecycle(self, n: int) -> list[str]:
        lifecycle = set(DEFAULT_LABELS.values())
        return [x for x in self.issues[n].labels if x in lifecycle]

    def factory_comments(self, n: int) -> list[Comment]:
        return [c for c in self.comments[n] if c.author == FACTORY]

    def pull_for(self, branch: str) -> _Pull | None:
        found = [p for p in self.pulls.values() if p.head_ref == branch]
        return max(found, key=lambda p: (p.state == "open", p.number)) if found else None

    # -- GitHubCLI surface --------------------------------------------------------

    def ensure_available(self) -> None:
        pass

    def current_user(self) -> User:
        return self.user

    def permission(self, repo: str, login: str) -> str:
        self._record("permission")
        return self.permissions.get(login, "none")

    def list_labels(self, repo: str) -> list[Label]:
        return list(self.labels)

    def create_label(self, repo: str, name: str, color: str, description: str) -> None:
        self._record("create_label")
        self.labels.append(Label(name, color, description))

    def _issue(self, i: _Issue) -> Issue:
        return Issue(i.number, i.title, i.body, i.state, tuple(i.labels), f"https://x/{i.number}",
                     i.updated_at or self.clock(), i.author)

    def list_open_issues(self, repo: str, label: str) -> list[Issue]:
        self._record("list_open_issues")
        return [self._issue(i) for i in self.issues.values()
                if i.state == "open" and label in i.labels]

    def get_issue(self, repo: str, number: int) -> Issue:
        self._record("get_issue")
        return self._issue(self.issues[number])

    def label_events(self, repo: str, number: int) -> list[LabelEvent]:
        return list(self.events[number])

    def list_comments(self, repo: str, number: int) -> list[Comment]:
        return list(self.comments[number])

    def create_comment(self, repo: str, number: int, body: str) -> Comment:
        self._record("create_comment")
        now = self.clock.tick()
        comment = Comment(next(self._ids), FACTORY, body, now, now)
        self.comments[number].append(comment)
        return comment

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        self._record("update_comment")
        for thread in self.comments.values():
            for i, c in enumerate(thread):
                if c.id == comment_id:
                    thread[i] = replace(c, body=body, updated_at=self.clock.tick())
                    return
        raise GitHubCLIError("comment not found")

    def set_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._record("set_labels")
        issue = self.issues[number]
        now = self.clock.tick()
        for old in set(issue.labels) - set(labels):
            self.events[number].append(LabelEvent("unlabeled", old, FACTORY, now))
        for new in set(labels) - set(issue.labels):
            self.events[number].append(LabelEvent("labeled", new, FACTORY, now))
        issue.labels = list(labels)

    def close_issue(self, repo: str, number: int) -> None:
        self._record("close_issue")
        self.issues[number].state = "closed"

    def _pull(self, p: _Pull) -> PullRequest:
        return PullRequest(p.number, p.state, p.merged, p.merged_by, p.merge_commit_sha,
                           p.head_ref, p.head_sha, p.base_ref, f"https://x/pull/{p.number}")

    def find_pull_by_branch(self, repo: str, branch: str) -> PullRequest | None:
        p = self.pull_for(branch)
        return self._pull(p) if p else None

    def get_pull(self, repo: str, number: int) -> PullRequest:
        return self._pull(self.pulls[number])

    def create_pull(self, repo: str, *, head: str, base: str, title: str,
                    body: str) -> PullRequest:
        self._record("create_pull")
        if head not in self.branches:
            raise GitHubCLIError("head branch does not exist")
        if any(p.head_ref == head and p.state == "open" for p in self.pulls.values()):
            raise GitHubCLIError("a pull request already exists for this branch")
        n = next(self._numbers)
        self.pulls[n] = _Pull(n, head, self.branches[head], base)
        self.pull_bodies[n] = body
        return self._pull(self.pulls[n])

    def list_reviews(self, repo: str, number: int) -> list[Review]:
        return list(self.reviews[number])

    def create_review(self, repo: str, number: int, commit_id: str, body: str) -> None:
        self._record("create_review")
        self.reviews[number].append(Review(self.user.login, "COMMENTED", commit_id,
                                           self.clock.tick(), body))
        self.feedback[number].append((self.user.login, commit_id, body))

    def compare_diff(self, repo: str, base: str, head: str, limit: int = 60_000) -> str:
        return f"--- diff {base}...{head}"

    def review_feedback(self, repo: str, number: int, commit_id: str) -> list[tuple[str, str]]:
        return [(a, t) for a, sha, t in self.feedback[number] if sha == commit_id]

    def pr_discussion(self, repo: str, number: int) -> list[Comment]:
        notes = [*self.discussion[number],
                 *(replace(c, body=f"(PR #{number}, comment) {c.body}")
                   for c in self.comments[number])]
        return sorted(notes, key=lambda c: c.created_at)

    def branch(self, repo: str, branch: str) -> dict | None:
        if branch not in self.branches:
            return None
        return {"name": branch, "protected": branch == "main",
                "commit": {"sha": self.branches[branch]}}

    def ahead_by(self, repo: str, base: str, head: str) -> int:
        if head not in self.branches:
            return 0
        return 0 if self.branches[head] == self.branches.get(base) else 1

    def default_branch(self, repo: str) -> str:
        return "main"

    def file_text(self, repo: str, path: str, ref: str) -> str | None:
        return self.contents.get((ref, path))

    def file_exists(self, repo: str, path: str, ref: str) -> bool:
        return path in self.files[ref]

    def checks(self, repo: str, sha: str) -> list[Check]:
        return list(self.ci_checks.get(sha, []))

    def find_artifact(self, repo: str, sha: str, name: str) -> int | None:
        if name != "demo-video" or sha not in self.artifacts:
            return None
        run_id = 500 + len(self._runs)
        self._runs[run_id] = sha
        return run_id

    def download_artifact(self, repo: str, run_id: int, name: str, dest: Path) -> None:
        if self.artifact_error:
            raise GitHubCLIError(self.artifact_error)
        sha = self._runs[run_id]
        dest.mkdir(parents=True)
        for file, data in self.artifacts[sha].items():
            (dest / file).write_bytes(data)

    def upload_release_asset(self, repo: str, tag: str, path: Path) -> str:
        self._record("upload_release_asset")
        self.assets[path.name] = path.read_bytes()
        return f"https://github.com/{repo}/releases/download/{tag}/{path.name}"

    def job_log(self, repo: str, job_id: int) -> str:
        return self.job_logs[job_id]

    def create_status(self, repo: str, sha: str, *, state: str, context: str,
                      description: str, target_url: str | None = None) -> None:
        self._record("create_status")
        status = "in_progress" if state == "pending" else "completed"
        conclusion = None if state == "pending" else ("success" if state == "success"
                                                       else "failure")
        others = [c for c in self.ci_checks.get(sha, []) if c.name != context]
        self.ci_checks[sha] = [Check(context, status, conclusion, description), *others]

    def commit_date(self, repo: str, sha: str) -> datetime:
        return self.commit_dates.get(sha, self.clock())


class FakeRelease:
    def __init__(self) -> None:
        self.states: dict[str, ReleaseState] = {}

    def status(self, repo: str, merge_sha: str) -> ReleaseStatus:
        return ReleaseStatus(self.states.get(merge_sha, ReleaseState.READY), f"img:{merge_sha}",
                             "fake")


@dataclass
class AgentScript:
    """What the fake agent does on each run: (result, changed paths)."""

    steps: list[tuple[AgentResult, list[str]]] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    tools: list[tuple[str, ...]] = field(default_factory=list)
    # Called once during the next agent run, e.g. a human acting while the agent works.
    during: Callable[[], None] | None = None

    def push(self, status: str = "done", paths: list[str] | None = None,
             questions: tuple[str, ...] = (), output: dict | None = None) -> None:
        self.steps.append((AgentResult(status, f"agent {status}", questions,
                                       output=output or {}), paths or []))

    def triage(self, *questions: str, summary: str = "Add a settings page.",
               size: str = "large") -> None:
        """A triage run asking these questions (none: the issue is clear)."""
        self.push("done", output={
            "summary": summary, "size": size, "in_scope": ["the settings page"],
            "out_of_scope": ["the profile page"], "assumptions": ["English only"],
            "code_findings": [{"path": "a.py", "line": 3, "finding": "a.py has no routes"}],
            "open_questions": [{"question": q, "suggested_answer": "yes"} for q in questions],
        })
        self.steps[-1] = (replace(self.steps[-1][0], summary=summary), [])

    def review(self, *findings: tuple[str, str]) -> None:
        """A review run returning (severity, title) findings."""
        self.push("done", output={"findings": [
            {"severity": sev, "title": title, "problem": f"{title}: problem",
             "evidence": "line 3", "impact": "it breaks", "fix": "Change line 3.",
             "path": "a.py", "line": 3} for sev, title in findings]})


class FakeAgent:
    def __init__(self, script: AgentScript, workspace: "FakeWorkspace"):
        self.script = script
        self.workspace = workspace
        self.images: list[str | None] = []

    def run(self, prompt: str, *, cwd: Path, allowed_tools: tuple[str, ...],
            image: str | None = None, schema: dict | None = None) -> AgentResult:
        self.images.append(image)
        self.script.prompts.append(prompt)
        self.script.tools.append(allowed_tools)
        if not self.script.steps:
            raise AssertionError("agent invoked unexpectedly")
        result, paths = self.script.steps.pop(0)
        self.workspace.pending[cwd] = paths
        if self.script.during is not None:
            during, self.script.during = self.script.during, None
            during()
        return result

    def secrets(self) -> tuple[str, ...]:
        return (SECRET,)


class FakeRunner:
    """Stands in for DockerRunner: returns scripted exit codes per command."""

    def __init__(self) -> None:
        self.exit_codes: list[int] = []
        self.calls: list[tuple[str, list[str], Path]] = []

    def run(self, image: str, argv: list[str], *, mount: Path, timeout: int,
            env: dict | None = None, stdin: str | None = None) -> ContainerResult:
        self.calls.append((image, argv, mount))
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        stderr = "FAIL: expected 1 got 2\n" if code and code != 137 else ""
        return ContainerResult(code, f"ran {argv[-1]}\n", stderr)

    @property
    def config(self):
        from dev_factory.config import DockerConfig
        return DockerConfig()


class FakeWorkspace:
    def __init__(self, gh: FakeGitHub):
        self.gh = gh
        self.pending: dict[Path, list[str]] = {}
        self.prepared: list[str] = []
        self.messages: list[str] = []
        self.crash_after_push = False
        # Paths whose content contains the agent's secret.
        self.leaky: set[str] = set()
        self._last_paths: list[str] = []

    def prepare(self, branch: str, base_branch: str) -> Task:
        self.prepared.append(branch)
        start = self.gh.branches.get(branch) or self.gh.branches[base_branch]
        return Task(branch, Path(f"/work/{branch}"), Path(f"/agent/{branch}"), start)

    def collect(self, task: Task) -> list[str]:
        self._last_paths = sorted(self.pending.pop(task.agent_dir, []))
        return self._last_paths

    def leaks(self, task: Task, paths: list[str], secrets: tuple[str, ...]) -> list[str]:
        return [p for p in paths if p in self.leaky]

    def commit_and_push(self, task: Task, message: str) -> str:
        self.gh._record("push")
        self.messages.append(message)
        sha = self.gh.new_sha()
        base_files = self.gh.files.get(task.branch) or self.gh.files["main"]
        self.gh.files[task.branch] = set(base_files) | set(self._last_paths)
        self.gh.branches[task.branch] = sha
        for p in self.gh.pulls.values():
            if p.head_ref == task.branch and p.state == "open":
                p.head_sha = sha
        if self.crash_after_push:
            self.crash_after_push = False
            raise Crash("after push")
        return sha

    def cleanup(self, task: Task) -> None:
        pass
