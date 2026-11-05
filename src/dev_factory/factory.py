"""One poll: discover, observe, decide, and (outside observe-only mode) act."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from dev_factory.claude_cli import ClaudeCLI
from dev_factory.config import ConfigError, FactoryConfig, ProjectConfig, ProjectEntry
from dev_factory.container import DockerRunner
from dev_factory.engine import AGENT_ACTIONS, HEAVY_ACTIONS, Action, Decision, Snapshot, decide
from dev_factory.executor import Executor, StaleSnapshot
from dev_factory.github_cli import GitHubCLI, GitHubCLIError
from dev_factory.labels import priority_rank
from dev_factory.models import Comment, Issue
from dev_factory.observer import Observer
from dev_factory.repo_config import ProjectLoader
from dev_factory.runtime import HostResources, busy_reason, host_resources
from dev_factory.workspace import Workspace, WorkspaceError

log = logging.getLogger(__name__)


@dataclass
class IssueReport:
    project_id: str
    issue: int
    title: str
    url: str
    stage: str | None
    action: str
    reason: str
    target: str | None = None
    outcome: str | None = None


@dataclass
class PollReport:
    observe_only: bool
    issues: list[IssueReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # project_id -> default-branch commit its .dev-factory/ config was read at
    configs: dict[str, str | None] = field(default_factory=dict)


@dataclass
class _Candidate:
    project: ProjectConfig
    observer: Observer
    executor: Executor
    snapshot: Snapshot
    marker_comment: Comment | None
    decision: Decision
    report: IssueReport


def discover(gh: GitHubCLI, project: ProjectConfig) -> list[Issue]:
    """All open issues (not PRs) carrying any lifecycle label, deduplicated."""
    found: dict[int, Issue] = {}
    for label in project.labels.values():
        for issue in gh.list_open_issues(project.repository, label):
            if not issue.is_pull_request:
                found[issue.number] = issue
    return sorted(found.values(), key=lambda i: i.number)


def missing_lifecycle_labels(gh: GitHubCLI, project: ProjectConfig) -> list[str]:
    existing = {label.name.lower() for label in gh.list_labels(project.repository)}
    return [name for name in project.labels.values() if name.lower() not in existing]


def interleave(heavy: list[_Candidate]) -> list[_Candidate]:
    """Priority first, then round-robin across projects (oldest first within each), so one
    busy repository cannot take every agent run."""
    def own_order(c: _Candidate) -> tuple:
        return (priority_rank(c.snapshot.issue.labels), c.snapshot.issue.updated_at)
    turn: dict[int, int] = {}
    seen: dict[tuple[str, int], int] = {}
    for c in sorted(heavy, key=own_order):
        key = (c.project.project_id, priority_rank(c.snapshot.issue.labels))
        seen[key] = seen.get(key, 0) + 1
        turn[id(c)] = seen[key]
    return sorted(heavy, key=lambda c: (priority_rank(c.snapshot.issue.labels), turn[id(c)],
                                        c.snapshot.issue.updated_at))


class Factory:
    def __init__(
        self,
        config: FactoryConfig,
        projects: list[ProjectEntry],
        gh: GitHubCLI,
        agent: ClaudeCLI,
        make_release: Callable,
        make_workspace: Callable[[ProjectConfig], Workspace],
        clock: Callable[[], datetime],
        runner: DockerRunner | None = None,
        resources: Callable[[], HostResources] = host_resources,
        *,
        load_project: ProjectLoader,
    ):
        self.config = config
        self.projects = [p for p in projects if p.enabled]
        self.load_project = load_project
        self.gh = gh
        self.agent = agent
        self.make_release = make_release
        self.make_workspace = make_workspace
        self.clock = clock
        self.runner = runner
        self.resources = resources

    @property
    def observe_only(self) -> bool:
        return self.config.runtime.observe_only

    def poll(self) -> PollReport:
        report = PollReport(observe_only=self.observe_only)
        candidates: list[_Candidate] = []
        for entry in self.projects:
            try:
                # The repository's .dev-factory/ from its default branch, read once per poll.
                project = self.load_project(entry)
            except (ConfigError, GitHubCLIError) as exc:
                log.warning("project config unusable; skipping", extra={
                    "project": entry.project_id, "error": str(exc)})
                report.errors.append(f"{entry.project_id}: {exc}")
                continue
            report.configs[project.project_id] = project.config_sha
            try:
                candidates.extend(self._collect(project, report))
            except (GitHubCLIError, WorkspaceError) as exc:
                log.exception("project poll failed", extra={"project": project.project_id})
                report.errors.append(f"{project.project_id}: {exc}")
        if self.observe_only:
            return report

        cheap = [c for c in candidates if c.decision.mutates
                 and c.decision.action not in HEAVY_ACTIONS]
        heavy = [c for c in candidates if c.decision.action in HEAVY_ACTIONS]
        for candidate in cheap:
            self._apply(candidate, report)
        heavy = interleave(heavy)
        budget = self.config.runtime.max_agent_runs_per_poll
        for candidate in heavy:
            if budget <= 0:
                candidate.report.outcome = "deferred: budget for this poll used"
                continue
            if candidate.decision.action in AGENT_ACTIONS and not self._isolated(candidate):
                candidate.report.outcome = ("refused: agent.image is not set; configure the "
                                            "agent container or set claude.allow_unisolated")
                continue
            busy = busy_reason(self.resources(), self.config.runtime.min_available_memory_mb,
                               self.config.runtime.max_load_per_cpu)
            if busy:
                log.warning("host busy, deferring heavy action", extra={"reason": busy})
                candidate.report.outcome = f"deferred: host busy ({busy})"
                continue
            budget -= 1
            if self._apply(candidate, report):
                self._follow_up(candidate, report)
        return report

    def _isolated(self, c: "_Candidate") -> bool:
        agent = c.project.agent
        return bool(agent and agent.image) or self.config.claude.allow_unisolated

    def _collect(self, project: ProjectConfig, report: PollReport) -> list[_Candidate]:
        missing = missing_lifecycle_labels(self.gh, project)
        if missing:
            report.errors.append(f"{project.project_id}: missing labels {missing}; run "
                                 "`dev-factory labels bootstrap`")
            return []
        observer = Observer(self.gh, project, self.make_release(project), self.clock)
        executor = Executor(self.gh, project, observer, self.agent,
                            self.make_workspace(project), self.clock, self.runner,
                            policy=project.policy or self.config.policy)
        result = []
        for issue in discover(self.gh, project):
            try:
                result.append(self._evaluate(project, observer, executor, issue, report))
            except GitHubCLIError as exc:
                log.exception("issue observation failed", extra={"issue": issue.number})
                report.errors.append(f"{project.project_id}#{issue.number}: {exc}")
        return result

    def _evaluate(
        self, project: ProjectConfig, observer: Observer, executor: Executor, issue: Issue,
        report: PollReport,
    ) -> _Candidate:
        snapshot, comment = observer.observe(issue)
        decision = decide(snapshot, project.policy or self.config.policy)
        entry = IssueReport(
            project_id=project.project_id, issue=issue.number, title=issue.title,
            url=issue.url, stage=decision.stage.value if decision.stage else None,
            action=decision.action.value, reason=decision.reason,
            target=decision.target.value if decision.target else None,
        )
        report.issues.append(entry)
        log.info("decision", extra={"project": project.project_id, "issue": issue.number,
                                    "action": entry.action, "stage": entry.stage,
                                    "target": entry.target, "reason": entry.reason})
        return _Candidate(project, observer, executor, snapshot, comment, decision, entry)

    def _apply(self, c: _Candidate, report: PollReport) -> bool:
        try:
            c.report.outcome = c.executor.apply(c.snapshot, c.decision, c.marker_comment)
        except StaleSnapshot as exc:
            c.report.outcome = f"skipped: {exc}"
            return False
        except (GitHubCLIError, WorkspaceError, OSError) as exc:
            log.exception("action failed", extra={"issue": c.snapshot.issue.number})
            c.report.outcome = f"failed: {exc}"
            report.errors.append(f"{c.project.project_id}#{c.snapshot.issue.number}: {exc}")
            return False
        log.info("applied", extra={"issue": c.snapshot.issue.number, "outcome": c.report.outcome})
        return True

    def _follow_up(self, c: _Candidate, report: PollReport) -> None:
        """After an agent run, apply the resulting transition now instead of next poll."""
        issue = self.gh.get_issue(c.project.repository, c.snapshot.issue.number)
        if issue.state != "open":
            return
        try:
            follow = self._evaluate(c.project, c.observer, c.executor, issue, report)
        except GitHubCLIError as exc:
            report.errors.append(f"{c.project.project_id}#{issue.number}: {exc}")
            return
        if follow.decision.mutates and follow.decision.action not in HEAVY_ACTIONS:
            self._apply(follow, report)
        elif follow.decision.action in HEAVY_ACTIONS:
            follow.report.outcome = "deferred: next poll"
        if follow.decision.action is Action.WAIT:
            follow.report.outcome = "waiting"
