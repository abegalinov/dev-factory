"""Carries out one Decision. Every side effect is preceded by a fresh read and is safe to repeat.

Order of operations for any transition: artifacts first, marker next, label last.
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime

from dev_factory import marker as markers
from dev_factory.claude_cli import REVIEW_SCHEMA, AgentResult, ClaudeCLI
from dev_factory.config import ProjectConfig
from dev_factory.container import ContainerTimeout, DockerRunner
from dev_factory.engine import (
    VERIFY_CONTEXT,
    Action,
    AIReview,
    Decision,
    Snapshot,
    code_branch,
    spec_branch,
    spec_path,
)
from dev_factory.github_cli import GitHubCLI
from dev_factory.marker import StateMarker
from dev_factory.models import Comment, PullRequest
from dev_factory.observer import Observer, verification_log_tag
from dev_factory.prompts import implement_prompt, review_prompt, spec_prompt
from dev_factory.repo_config import BASELINE_PROTECTED
from dev_factory.state_machine import Stage, classify_labels
from dev_factory.workspace import Task, Workspace, violations

log = logging.getLogger(__name__)

SIGNATURE = "Dev Factory"


class StaleSnapshot(RuntimeError):
    """Issue changed between observation and action; the next poll re-decides."""


class Executor:
    def __init__(
        self,
        gh: GitHubCLI,
        project: ProjectConfig,
        observer: Observer,
        agent: ClaudeCLI,
        workspace: Workspace,
        clock: Callable[[], datetime],
        runner: DockerRunner | None = None,
    ):
        self.gh = gh
        self.project = project
        self.observer = observer
        self.agent = agent
        self.workspace = workspace
        self.clock = clock
        self.runner = runner
        self.repo = project.repository

    def apply(self, snap: Snapshot, decision: Decision, marker_comment: Comment | None) -> str:
        handler = {
            Action.TRANSITION: self.transition,
            Action.RUN_SPEC: self.run_spec,
            Action.RUN_IMPLEMENT: self.run_implement,
            Action.RUN_VERIFY: self.run_verify,
            Action.RUN_REVIEW: self.run_review,
            Action.ANNOUNCE_CANDIDATE: self.announce_candidate,
            Action.CLOSE: self.close,
        }.get(decision.action)
        if handler is None:
            return "no-op"
        return handler(snap, decision, marker_comment)

    # -- shared building blocks ----------------------------------------------------

    def _fresh_check(self, snap: Snapshot) -> list[str]:
        fresh = self.gh.get_issue(self.repo, snap.issue.number)
        if fresh.state != "open":
            raise StaleSnapshot("issue is no longer open")
        if classify_labels(fresh.labels, self.project.labels).stages != snap.label_state.stages:
            raise StaleSnapshot("lifecycle labels changed since observation")
        return list(fresh.labels)

    def _recheck(self, snap: Snapshot, pr: PullRequest | None) -> None:
        """After an agent run (up to claude.timeout_seconds): nothing is pushed or posted if a
        human closed or relabelled the issue, or closed or pushed to the PR, meanwhile."""
        self._fresh_check(snap)
        if pr is not None and pr.is_open:
            fresh = self.gh.get_pull(self.repo, pr.number)
            if not fresh.is_open:
                raise StaleSnapshot(f"PR #{pr.number} was closed during the agent run")
            if fresh.head_sha != pr.head_sha:
                raise StaleSnapshot(f"head of PR #{pr.number} moved during the agent run")

    def _leaks(self, task: Task, changed: list[str]) -> list[str]:
        return self.workspace.leaks(task, changed, self.agent.secrets())

    def write_marker(
        self, number: int, comment: Comment | None, marker: StateMarker, note: str
    ) -> Comment:
        marker = marker.evolve(updated_at=self.clock().isoformat(),
                               config_sha=self.project.config_sha or marker.config_sha)
        body = markers.render(marker, _status_summary(marker, note))
        if comment is None:
            return self.gh.create_comment(self.repo, number, body)
        self.gh.update_comment(self.repo, comment.id, body)
        return comment

    def transition(self, snap: Snapshot, decision: Decision, comment: Comment | None) -> str:
        target = decision.target
        assert target is not None and decision.marker is not None
        labels = self._fresh_check(snap)
        number = snap.issue.number
        if target is Stage.REVIEW and snap.code_pr is not None and snap.checks is not None:
            self._post_evidence(snap)
        self.write_marker(number, comment, decision.marker, decision.reason)
        lifecycle = set(self.project.labels.values())
        keep = [name for name in labels if name not in lifecycle]
        self.gh.set_labels(self.repo, number, [*keep, self.project.labels[target]])
        source = decision.stage.value if decision.stage else "conflicting labels"
        self.gh.create_comment(self.repo, number, f"**{SIGNATURE}:** `{source}` → "
                                                  f"`{target.value}`: {decision.reason}")
        return f"{source} -> {target.value}"

    def _post_evidence(self, snap: Snapshot) -> None:
        pr, checks = snap.code_pr, snap.checks
        assert pr is not None and checks is not None
        tag = f"<!-- dev-factory:evidence {pr.head_sha} -->"
        existing = self.gh.list_comments(self.repo, pr.number)
        if any(c.author == snap.factory_login and tag in c.body for c in existing):
            return
        rows = "\n".join(
            f"| {c.name} | {c.conclusion} | {f'[link]({c.url})' if c.url else ''} |"
            for c in checks.checks
        )
        body = (f"{tag}\n**{SIGNATURE} verification evidence** for `{pr.head_sha}`\n\n"
                f"| Check | Result | |\n|---|---|---|\n{rows}\n\n"
                "Verification only reads CI results; it does not approve or merge.")
        self.gh.create_comment(self.repo, pr.number, body)

    def _block_now(
        self, snap: Snapshot, stage: Stage, marker: StateMarker, comment: Comment, reason: str
    ) -> str:
        decision = Decision(Action.TRANSITION, reason, stage=stage, target=Stage.BLOCKED,
                            marker=marker.evolve(stage=Stage.BLOCKED, last_failure=reason))
        return self.transition(snap, decision, comment)

    def _ask(
        self, snap: Snapshot, stage: Stage, marker: StateMarker, comment: Comment,
        result: AgentResult,
    ) -> str:
        questions = "\n".join(f"{i}. {q}" for i, q in enumerate(result.questions, 1))
        asked = self.gh.create_comment(
            self.repo, snap.issue.number,
            f"**{SIGNATURE} needs input** before continuing `{stage.value}`:\n\n{questions}\n\n"
            "Reply in a comment. A maintainer's reply resumes the work.")
        decision = Decision(
            Action.TRANSITION, "questions posted", stage=stage, target=Stage.NEEDS_INPUT,
            marker=marker.evolve(stage=Stage.NEEDS_INPUT, resume_stage=stage,
                                 asked_at=asked.created_at.isoformat()),
        )
        return self.transition(snap, decision, comment)

    def _feedback(self, pr: PullRequest | None, commit: str | None) -> str | None:
        if pr is None or not commit:
            return None
        items = [
            f"@{author}: {text}"
            for author, text in self.gh.review_feedback(self.repo, pr.number, commit)
            if self.observer.has_write(author)
        ]
        return "\n\n".join(items) or None

    def _recover_pushed_branch(self, branch: str, title: str, body: str) -> PullRequest | None:
        """A branch pushed before a crash, without a PR, is completed instead of redone."""
        if self.gh.branch(self.repo, branch) is None:
            return None
        if self.gh.ahead_by(self.repo, self.project.default_branch, branch) == 0:
            return None
        log.info("recovering pushed branch without PR", extra={"branch": branch})
        return self.gh.create_pull(self.repo, head=branch, base=self.project.default_branch,
                                   title=title, body=body)

    # -- agent stages ----------------------------------------------------------------

    def run_spec(self, snap: Snapshot, decision: Decision, comment: Comment | None) -> str:
        number, issue = snap.issue.number, snap.issue
        branch, path = spec_branch(number), spec_path(self.project.spec_directory, number)
        title = f"Spec: {issue.title} (#{number})"
        body = (f"Refs #{number}\n\nSpecification drafted by {SIGNATURE} at `{path}`.\n\n"
                "Merging this PR approves the spec and starts implementation.")
        self._fresh_check(snap)
        if snap.spec_pr is None and self._recover_pushed_branch(branch, title, body):
            return "recovered spec PR"
        marker = decision.marker
        assert marker is not None and self.project.agent is not None
        comment = self.write_marker(number, comment, marker, f"drafting spec (attempt "
                                                             f"{marker.attempt})")
        task = self.workspace.prepare(branch, self.project.default_branch)
        try:
            prompt = spec_prompt(self.project.agent.spec_prompt, issue,
                                 snap.authorized_comments, path,
                                 self._feedback(snap.spec_pr, marker.rework_base),
                                 self.project.agent.instructions, self.project.spec_template)
            result = self.agent.run(prompt, cwd=task.agent_dir,
                                    allowed_tools=self.project.agent.spec_allowed_tools,
                                    image=self.project.agent.image)
            log.info("spec agent finished", extra={"issue": number, "status": result.status,
                                                   "cost_usd": result.cost_usd})
            self._recheck(snap, snap.spec_pr)
            if result.status == "needs_input":
                return self._ask(snap, Stage.SPEC, marker, comment, result)
            if result.status != "done":
                self.write_marker(number, comment, marker.evolve(last_failure=result.summary),
                                  "spec attempt failed")
                return f"spec attempt failed: {result.summary[:200]}"
            changed = self.workspace.collect(task)
            if not changed:
                self.write_marker(number, comment,
                                  marker.evolve(last_failure=_no_changes(result)),
                                  "spec attempt produced no changes")
                return "spec attempt produced no changes"
            if changed != [path]:
                return self._block_now(snap, Stage.SPEC, marker, comment,
                                       f"spec agent changed files other than {path}: "
                                       f"{', '.join(changed[:10])}")
            if self._leaks(task, changed):
                return self._block_now(snap, Stage.SPEC, marker, comment,
                                       f"the agent's credential appears in {path}; nothing "
                                       "was pushed")
            self.workspace.commit_and_push(task, commit_message(
                result, f"Spec for #{number}: {issue.title}", number, marker.attempt,
                self.project.config_sha))
            pr = snap.spec_pr if snap.spec_pr and snap.spec_pr.is_open else self.gh.create_pull(
                self.repo, head=branch, base=self.project.default_branch, title=title, body=body)
            self.write_marker(number, comment,
                              marker.evolve(spec_pr=pr.number, last_failure=None),
                              f"spec pushed to PR #{pr.number}")
            return f"spec pushed to PR #{pr.number}"
        finally:
            self.workspace.cleanup(task)

    def run_implement(self, snap: Snapshot, decision: Decision, comment: Comment | None) -> str:
        number, issue = snap.issue.number, snap.issue
        branch = code_branch(number)
        spec = spec_path(self.project.spec_directory, number)
        title = f"{issue.title} (#{number})"
        body = (f"Refs #{number}\n\nImplements the approved spec `{spec}`.\n\n"
                f"Opened by {SIGNATURE}. Merging requires human review; it does not deploy.")
        if self.project.pr_body_template:
            body = (f"{_render(self.project.pr_body_template, issue=f'#{number}', spec=spec)}"
                    f"\n\n---\nRefs #{number}. Opened by {SIGNATURE}. Merging requires human "
                    "review; it does not deploy.")
        self._fresh_check(snap)
        if snap.code_pr is None and self._recover_pushed_branch(branch, title, body):
            return "recovered code PR"
        marker = decision.marker
        agent_cfg = self.project.agent
        assert marker is not None and agent_cfg is not None
        comment = self.write_marker(number, comment, marker,
                                    f"implementing (attempt {marker.attempt})")
        task = self.workspace.prepare(branch, self.project.default_branch)
        try:
            prompt = implement_prompt(agent_cfg.implementation_prompt, issue,
                                      snap.authorized_comments, spec, marker.last_failure,
                                      self._feedback(snap.code_pr, marker.rework_base),
                                      agent_cfg.instructions)
            result = self.agent.run(prompt, cwd=task.agent_dir,
                                    allowed_tools=agent_cfg.implementation_allowed_tools,
                                    image=agent_cfg.image)
            log.info("implementation agent finished", extra={
                "issue": number, "status": result.status, "cost_usd": result.cost_usd})
            self._recheck(snap, snap.code_pr)
            if result.status == "needs_input":
                return self._ask(snap, Stage.IMPLEMENT, marker, comment, result)
            if result.status != "done":
                self.write_marker(number, comment, marker.evolve(last_failure=result.summary),
                                  "implementation attempt failed")
                return f"implementation attempt failed: {result.summary[:200]}"
            changed = self.workspace.collect(task)
            protected = (*BASELINE_PROTECTED, *agent_cfg.protected_paths,
                         f"{self.project.spec_directory}/**")
            bad = violations(changed, protected)
            if bad:
                return self._block_now(snap, Stage.IMPLEMENT, marker, comment,
                                       f"agent changed protected paths: {', '.join(bad[:10])}")
            leaked = self._leaks(task, changed)
            if leaked:
                return self._block_now(snap, Stage.IMPLEMENT, marker, comment,
                                       "the agent's credential appears in "
                                       f"{', '.join(leaked[:10])}; nothing was pushed")
            if not changed:
                self.write_marker(number, comment,
                                  marker.evolve(last_failure=_no_changes(result)),
                                  "implementation attempt produced no changes")
                return "implementation attempt produced no changes"
            self.workspace.commit_and_push(task, commit_message(
                result, f"Implement #{number}: {issue.title}", number, marker.attempt,
                self.project.config_sha))
            summary = f"### Agent summary\n{result.summary}"
            if "{{summary}}" in self.project.pr_body_template:
                body, summary = body.replace("{{summary}}", result.summary), ""
            pr = snap.code_pr if snap.code_pr and snap.code_pr.is_open else self.gh.create_pull(
                self.repo, head=branch, base=self.project.default_branch, title=title,
                body=f"{body}\n\n{summary}".strip())
            self.write_marker(number, comment, marker.evolve(code_pr=pr.number),
                              f"implementation pushed to PR #{pr.number}")
            return f"implementation pushed to PR #{pr.number}"
        finally:
            self.workspace.cleanup(task)

    def run_review(self, snap: Snapshot, decision: Decision, comment: Comment | None) -> str:
        """Automated read-only review of the stage's PR head, posted as a COMMENT review.

        Blocking findings reach the authoring agent as review feedback on that commit.
        """
        number, issue = snap.issue.number, snap.issue
        kind = "spec" if decision.stage is Stage.SPEC else "code"
        pr = snap.spec_pr if kind == "spec" else snap.code_pr
        marker, agent_cfg = decision.marker, self.project.agent
        assert pr is not None and marker is not None and agent_cfg is not None
        assert agent_cfg.review_prompt is not None
        self._fresh_check(snap)
        fresh = self.gh.get_pull(self.repo, pr.number)
        if fresh.head_sha != pr.head_sha:
            raise StaleSnapshot(f"head of PR #{pr.number} changed since observation")
        comment = self.write_marker(number, comment, marker,
                                    f"reviewing PR #{pr.number} (round {marker.review_round})")
        task = self.workspace.prepare(pr.head_ref, self.project.default_branch)
        try:
            if task.base_sha != pr.head_sha:
                raise StaleSnapshot(f"branch of PR #{pr.number} moved during checkout")
            diff = self.gh.compare_diff(self.repo, self.project.default_branch, pr.head_sha)
            prompt = review_prompt(agent_cfg.review_prompt, kind, issue,
                                   snap.authorized_comments,
                                   spec_path(self.project.spec_directory, number), diff,
                                   agent_cfg.instructions, self.project.review_checklist)
            result = self.agent.run(prompt, cwd=task.agent_dir,
                                    allowed_tools=agent_cfg.review_allowed_tools,
                                    image=agent_cfg.image, schema=REVIEW_SCHEMA)
            log.info("review agent finished", extra={"issue": number, "status": result.status,
                                                     "cost_usd": result.cost_usd})
            self._recheck(snap, pr)
            if result.status != "done":
                # Advisory: after max_review_rounds the PR goes to the humans without it.
                self.write_marker(number, comment, marker, f"review attempt failed: "
                                                           f"{result.summary[:300]}")
                return f"review attempt failed: {result.summary[:200]}"
            body, review = review_body(result, pr.head_sha)
            self.gh.create_review(self.repo, pr.number, pr.head_sha, body)
            note = (f"review of PR #{pr.number}: {review.blocking} blocking, "
                    f"{review.suggestions} suggestion(s)")
            self.write_marker(number, comment, marker, note)
            return note
        finally:
            self.workspace.cleanup(task)

    # -- verification ------------------------------------------------------------------

    def run_verify(self, snap: Snapshot, decision: Decision, comment: Comment | None) -> str:
        """Run the project's commands on the PR head in a container; report a commit status.

        A pending status is posted first, so a run that never finishes ends in the checks
        timeout instead of being retried forever.
        """
        config = self.project.verification
        pr = snap.code_pr
        assert pr is not None and config.image is not None
        if self.runner is None:
            raise StaleSnapshot("container verification needs a DockerRunner")
        self._fresh_check(snap)
        if self.gh.get_pull(self.repo, pr.number).head_sha != pr.head_sha:
            raise StaleSnapshot("code PR head moved since observation")
        sha = pr.head_sha
        self.gh.create_status(self.repo, sha, state="pending", context=VERIFY_CONTEXT,
                              description=f"{SIGNATURE} verification running")
        task = self.workspace.prepare(pr.head_ref, self.project.default_branch)
        try:
            if task.base_sha != sha:
                raise StaleSnapshot("checked-out head differs from the PR head")
            passed, failed_command, killed, log = True, None, False, []
            for command in config.commands:
                try:
                    result = self.runner.run(config.image, ["sh", "-c", command],
                                             mount=task.agent_dir,
                                             timeout=config.timeout_seconds)
                except ContainerTimeout:
                    log.append(f"$ {command}\n(timed out after {config.timeout_seconds}s)")
                    passed, failed_command = False, command
                    break
                output = (result.stdout + result.stderr)[-4000:]
                log.append(f"$ {command}\n(exit {result.returncode})\n{output}")
                if result.killed:
                    killed, failed_command = True, command
                    break
                if result.returncode != 0:
                    passed, failed_command = False, command
                    break
        finally:
            self.workspace.cleanup(task)
        text = "\n\n".join(log)[-6000:].replace("~~~~", "~ ~ ~ ~")
        if killed:
            # An infrastructure limit, not a verdict on the code: no attempt is consumed.
            # The status stays pending, so the checks timeout blocks the issue for a human.
            self.gh.create_comment(
                self.repo, pr.number,
                f"**{SIGNATURE} verification** was killed at `{failed_command}` for `{sha}`, "
                f"most likely by the container memory limit ({self.runner.config.memory}). "
                "Raise `docker.memory` or run tests in CI.\n\n"
                f"<details><summary>Output</summary>\n\n~~~~text\n{text}\n~~~~\n</details>")
            return f"verification killed on {sha[:12]} (memory limit?)"
        verdict = "passed" if passed else f"failed at `{failed_command}`"
        self.gh.create_comment(
            self.repo, pr.number,
            f"{verification_log_tag(sha)}\n**{SIGNATURE} verification** {verdict} for "
            f"`{sha}` in `{config.image}`.\n\n<details><summary>Output</summary>\n\n"
            f"~~~~text\n{text}\n~~~~\n</details>")
        description = (f"{len(config.commands)} commands passed" if passed
                       else f"failed: {failed_command}")
        self.gh.create_status(self.repo, sha, state="success" if passed else "failure",
                              context=VERIFY_CONTEXT, description=description,
                              target_url=pr.url or None)
        return f"verification {'passed' if passed else 'failed'} on {sha[:12]}"

    # -- release -----------------------------------------------------------------------

    def announce_candidate(
        self, snap: Snapshot, decision: Decision, comment: Comment | None
    ) -> str:
        assert snap.release is not None and decision.marker is not None
        self._fresh_check(snap)
        self.gh.create_comment(
            self.repo, snap.issue.number,
            f"**{SIGNATURE}:** release candidate `{snap.release.candidate}` is ready.\n\n"
            "Production deployment requires explicit human approval through the project's "
            "deployment process. The factory never deploys.")
        self.write_marker(snap.issue.number, comment, decision.marker,
                          f"release candidate {snap.release.candidate} announced")
        return "release candidate announced"

    def close(self, snap: Snapshot, decision: Decision, comment: Comment | None) -> str:
        assert decision.marker is not None
        self._fresh_check(snap)
        self.gh.create_comment(self.repo, snap.issue.number,
                               f"**{SIGNATURE}:** {decision.reason}. Closing.")
        self.write_marker(snap.issue.number, comment, decision.marker, decision.reason)
        self.gh.close_issue(self.repo, snap.issue.number)
        return "closed"


def _no_changes(result: AgentResult) -> str:
    """Why the agent changed nothing, in its own words: the human needs it to unblock."""
    summary = result.summary.strip()
    return f"agent produced no changes: {summary}" if summary else "agent produced no changes"


def _status_summary(marker: StateMarker, note: str) -> str:
    rows = [("Stage", f"`{marker.stage.value}`"), ("Last update", note)]
    if marker.attempt:
        rows.append(("Attempt", str(marker.attempt)))
    if marker.spec_pr:
        rows.append(("Spec PR", f"#{marker.spec_pr}"))
    if marker.code_pr:
        rows.append(("Code PR", f"#{marker.code_pr}"))
    if marker.merge_sha:
        rows.append(("Merge commit", f"`{marker.merge_sha[:12]}`"))
    if marker.last_failure:
        rows.append(("Last failure",
                     marker.last_failure[:300].replace("\n", " ").replace("|", "\\|")))
    table = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return (f"**{SIGNATURE} status** (maintained automatically; edits are overwritten)\n\n"
            f"| | |\n|---|---|\n{table}")


_CO_AUTHOR = re.compile(r"\s*co-authored-by:", re.IGNORECASE)
_TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: .+$")


def _render(template: str, **values: str) -> str:
    """{{name}} placeholders; {{summary}} is left for after the agent run."""
    for name, value in values.items():
        template = template.replace(f"{{{{{name}}}}}", value)
    return template


def commit_message(
    result: AgentResult, fallback: str, issue: int, attempt: int, config_sha: str | None = None,
) -> str:
    """The agent's what-and-why message (fallback: a generic subject), plus git trailers.

    Co-Authored-By lines are dropped: the factory's trailers say where a commit came from.
    The trailers join any other trailer block the agent's message ends with, so
    `git interpret-trailers` sees a single block.
    """
    message = str(result.output.get("commit_message") or "")[:4000]
    message = "\n".join(line for line in message.splitlines()
                        if not _CO_AUTHOR.match(line)).strip()
    if not message:
        message = f"{fallback}\n\n{result.summary.strip()}".strip()
    trailers = f"Refs: #{issue}\nDev-Factory-Attempt: {attempt}"
    if config_sha:
        trailers += f"\nDev-Factory-Config: {config_sha[:12]}"
    paragraphs = message.split("\n\n")
    last = paragraphs[-1].splitlines()
    if len(paragraphs) > 1 and last and all(_TRAILER.match(line) for line in last):
        return f"{message}\n{trailers}"
    return f"{message}\n\n{trailers}"


def review_body(result: AgentResult, head_sha: str) -> tuple[str, AIReview]:
    findings = [f for f in result.output.get("findings") or [] if isinstance(f, dict)]
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    suggestions = [f for f in findings if f.get("severity") != "blocking"]
    review = AIReview(head_sha, len(blocking), len(suggestions))

    def render(f: dict) -> str:
        where = str(f.get("path") or "")
        if where and f.get("line"):
            where += f":{f['line']}"
        where = f" (`{where}`)" if where else ""
        return f"- **{f.get('title', '')}**{where}\n  {str(f.get('detail', '')).strip()}"

    parts = [review.tag(), f"**{SIGNATURE} automated review** of `{head_sha[:12]}`",
             "", result.summary.strip()]
    if blocking:
        parts += ["", f"### Blocking ({len(blocking)})", *map(render, blocking)]
    if suggestions:
        parts += ["", f"### Suggestions ({len(suggestions)})", *map(render, suggestions)]
    if not findings:
        parts += ["", "No findings."]
    parts += ["", "Advisory only: blocking findings go back to the authoring agent; a human "
                  "still approves and merges."]
    return "\n".join(parts), review
