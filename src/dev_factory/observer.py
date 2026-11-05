"""Builds a Snapshot of one issue from live GitHub state."""

import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from dev_factory import marker as markers
from dev_factory.config import ProjectConfig
from dev_factory.engine import (
    TRIAGE_TAG,
    VERIFY_CONTEXT,
    AIReview,
    CheckState,
    Snapshot,
    changes_requested,
    code_branch,
    spec_branch,
    spec_path,
    summarize_checks,
)
from dev_factory.github_cli import GitHubCLI, GitHubCLIError
from dev_factory.models import Check, Comment, Issue, PullRequest, Review
from dev_factory.release import ReleaseAdapter
from dev_factory.state_machine import Stage, classify_labels

WRITE_PERMISSIONS = frozenset({"admin", "write"})
JOB_LOG_LINES = 100

log = logging.getLogger(__name__)


class Observer:
    def __init__(
        self,
        gh: GitHubCLI,
        project: ProjectConfig,
        release: ReleaseAdapter,
        clock: Callable[[], datetime],
    ):
        self.gh = gh
        self.project = project
        self.release = release
        self.clock = clock
        self.factory_login = gh.current_user().login
        self._permissions: dict[str, str] = {}

    def has_write(self, login: str | None) -> bool:
        if not login:
            return False
        if login == self.factory_login:
            return True
        if login not in self._permissions:
            self._permissions[login] = self.gh.permission(self.project.repository, login)
        return self._permissions[login] in WRITE_PERMISSIONS

    def observe(self, issue: Issue) -> tuple[Snapshot, Comment | None]:
        """Returns the snapshot and the marker comment (needed to update it in place)."""
        repo = self.project.repository
        number = issue.number
        label_state = classify_labels(issue.labels, self.project.labels)
        base = Snapshot(
            issue=issue, label_state=label_state,
            factory_login=self.factory_login, now=self.clock(),
        )
        stage = label_state.stage
        if stage is None:
            if not label_state.is_conflicting:
                return base, None
            # Blocking on conflicting labels updates the existing status comment instead of
            # starting a second one with an empty state.
            found = markers.find(self.gh.list_comments(repo, number), self.factory_login)
            return (replace(base, marker=found[1]), found[0]) if found else (base, None)

        label = self.project.labels[stage]
        applied = [e for e in self.gh.label_events(repo, number)
                   if e.action == "labeled" and e.label == label]
        last = max(applied, key=lambda e: e.created_at) if applied else None
        actor = last.actor if last else None
        authorized = self.has_write(actor)
        snap = replace(base, label_actor=actor,
                       label_applied_at=last.created_at if last else None,
                       actor_authorized=authorized)
        if not authorized:
            return snap, None

        comments = self.gh.list_comments(repo, number)
        found = markers.find(comments, self.factory_login)
        marker_comment, marker = found if found else (None, None)
        authorized_comments = tuple(
            c for c in comments
            if c.author != self.factory_login and self.has_write(c.author)
        )
        fields: dict[str, object] = {
            "marker": marker, "authorized_comments": authorized_comments,
            "review_enabled": bool(self.project.agent and self.project.agent.review),
            "triage_enabled": bool(self.project.agent and self.project.agent.triage),
        }
        if stage is Stage.SPEC:
            fields["triage_notes"] = tuple(
                c for c in comments if c.author == self.factory_login and TRIAGE_TAG in c.body)

        if stage in (Stage.SPEC, Stage.SPEC_REVIEW, Stage.NEEDS_INPUT):
            pr = self.gh.find_pull_by_branch(repo, spec_branch(number))
            fields["spec_pr"] = pr
            if pr is not None and pr.is_open:
                reviews = self.gh.list_reviews(repo, pr.number)
                fields["spec_changes_requested"] = changes_requested(pr, reviews)
                if stage is Stage.SPEC:
                    fields["ai_review"] = self._ai_review(pr, reviews)
        if stage in (Stage.SPEC_REVIEW, Stage.IMPLEMENT):
            fields["spec_file_on_default"] = self.gh.file_exists(
                repo, spec_path(self.project.spec_directory, number),
                self.project.default_branch)
        needs_code = stage in (Stage.IMPLEMENT, Stage.VERIFY, Stage.REVIEW,
                               Stage.RELEASE, Stage.NEEDS_INPUT)
        if needs_code:
            pr = self.gh.find_pull_by_branch(repo, code_branch(number))
            fields["code_pr"] = pr
            if pr is not None and pr.is_open:
                reviews = self.gh.list_reviews(repo, pr.number)
                fields["code_changes_requested"] = changes_requested(pr, reviews)
                if stage is Stage.VERIFY:
                    fields.update(self._verification(pr.number, pr.head_sha))
                    fields["ai_review"] = self._ai_review(pr, reviews)
            if stage is Stage.RELEASE:
                merge_sha = (marker.merge_sha if marker else None) or (
                    pr.merge_commit_sha if pr is not None and pr.merged else None)
                if merge_sha:
                    fields["release"] = self.release.status(repo, merge_sha)

        return replace(snap, **fields), marker_comment

    def _ai_review(self, pr: PullRequest, reviews: list[Review]) -> AIReview | None:
        """The factory's own automated review of the PR's current head, if posted."""
        for review in reversed(reviews):
            if review.author != self.factory_login:
                continue
            parsed = AIReview.parse(review.body)
            if parsed is not None and pr.head_sha.startswith(parsed.sha):
                return parsed
        return None

    def _verification(self, pr_number: int, head_sha: str) -> dict[str, object]:
        repo = self.project.repository
        config = self.project.verification
        checks = self.gh.checks(repo, head_sha)
        required = config.required_checks
        fields: dict[str, object] = {"head_committed_at": self.gh.commit_date(repo, head_sha)}
        if config.mode == "container":
            required = (*required, VERIFY_CONTEXT)
            local = [c for c in checks if c.name == VERIFY_CONTEXT]
            fields["needs_local_verification"] = not local
            if local and local[0].status == "completed" and local[0].conclusion != "success":
                fields["verification_log"] = self._verification_log(pr_number, head_sha)
        summary = summarize_checks(checks, required)
        fields["checks"] = summary
        if config.mode == "github-checks" and summary.state is CheckState.FAILURE:
            fields["verification_log"] = self._job_logs(summary.failed)
        return fields

    def _job_logs(self, failed: tuple[Check, ...]) -> str | None:
        """Failing-step excerpts of failed GitHub Actions jobs, so the next attempt sees
        the actual error (Actions check runs carry no useful title or summary)."""
        parts = []
        for check in failed:
            if check.job_id is None:
                continue
            try:
                text = self.gh.job_log(self.project.repository, check.job_id)
            except GitHubCLIError as exc:  # best effort: the failure itself is still known
                log.warning("could not fetch job log", extra={
                    "check": check.name, "job_id": check.job_id, "error": str(exc)})
                continue
            parts.append(f"### {check.name}\n{failed_step_excerpt(text)}")
        return "\n\n".join(parts) or None

    def _verification_log(self, pr_number: int, head_sha: str) -> str | None:
        tag = verification_log_tag(head_sha)
        for comment in reversed(self.gh.list_comments(self.project.repository, pr_number)):
            if comment.author == self.factory_login and tag in comment.body:
                return comment.body.split("~~~~text\n", 1)[-1].rsplit("\n~~~~", 1)[0]
        return None


def verification_log_tag(sha: str) -> str:
    return f"<!-- dev-factory:verify-log {sha} -->"


_TIMESTAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PASSED = ("\u2705", "Passing tests")  # group titles of passing tests (Flutter's reporter)
# Stack frames ("#12  fn (file:1:2)") and the filler between them; traces keep STACK_FRAMES.
_FRAME = re.compile(r"^#\d+\s")
_FILLER = re.compile(r"^(<asynchronous suspension>|\(elided .*\)|\.\.\.\s+Normal element)")
STACK_FRAMES = 6


def failed_step_excerpt(
    job_log: str, max_lines: int = JOB_LOG_LINES, group_lines: int = 20,
) -> str:
    """The end of the step that failed in a GitHub Actions job log.

    Timestamps and colors are removed. The excerpt starts at the failing step's
    "Run ..." header and ends at its error line. Groups of passing tests are dropped,
    stack traces keep their first STACK_FRAMES frames, and other groups printed inside
    the step (e.g. one per failed test, or a device log) keep only their first
    group_lines lines, where the error usually is; then the last max_lines lines are kept.
    """
    lines = [_ANSI.sub("", _TIMESTAMP.sub("", line)) for line in job_log.splitlines()]
    errors = [i for i, line in enumerate(lines) if line.startswith("##[error]")]
    end = errors[-1] + 1 if errors else len(lines)
    start = max((i for i in range(end) if lines[i].startswith("##[group]Run ")), default=0)
    step: list[str] = []
    in_group = header = passing = False
    kept = omitted = frames = 0
    for i, line in enumerate(lines[start:end]):
        # Long stack traces would fill the excerpt and push the error messages out.
        if _FILLER.match(line):
            continue
        frames = frames + 1 if _FRAME.match(line) else 0
        if frames > STACK_FRAMES:
            continue
        if line.startswith("##[group]"):
            title = line.removeprefix("##[group]")
            # The step's own header group (shell, env) is skipped up to its end, and so
            # are groups of passing tests: hundreds of them would push the failure out.
            header = i == 0
            passing = title.startswith(_PASSED)
            in_group, kept, omitted = not header, 0, 0
            if not passing:
                step.append(title)
        elif line.startswith("##[endgroup]"):
            if omitted and not passing:
                step.append(f"... ({omitted} lines omitted)")
            in_group = header = passing = False
            omitted = 0
        elif header or passing or not line.strip():
            continue
        elif in_group and kept >= group_lines:
            omitted += 1
        else:
            kept += in_group
            step.append(line)
    if omitted:
        step.append(f"... ({omitted} lines omitted)")
    if len(step) > max_lines:
        step = [step[0], f"... ({len(step) - max_lines} lines omitted)", *step[-(max_lines - 1):]]
    return "\n".join(step)
