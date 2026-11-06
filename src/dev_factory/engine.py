"""Pure decision logic: GitHub snapshot in, one next action out. No I/O.

Every rule here corresponds to a section of docs/issue-contract.md.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from dev_factory.config import PolicyConfig
from dev_factory.github_cli import PASSING_CONCLUSIONS
from dev_factory.marker import StateMarker
from dev_factory.models import Check, Comment, Issue, PullRequest, Review
from dev_factory.state_machine import LabelState, Stage, require_transition

# Commit status posted when the factory runs verification itself (mode: container).
VERIFY_CONTEXT = "dev-factory/verify"

_REVIEW_TAG = re.compile(r"<!-- dev-factory:ai-review ([\w-]+) (\{.*?\}) -->")
# Marks the factory's triage comments on an issue (agent.triage).
TRIAGE_TAG = "<!-- dev-factory:triage -->"


@dataclass(frozen=True)
class AIReview:
    """The factory's automated review of one PR head commit (a tagged PR review)."""

    sha: str
    blocking: int
    suggestions: int

    def tag(self) -> str:
        counts = json.dumps({"blocking": self.blocking, "suggestions": self.suggestions})
        return f"<!-- dev-factory:ai-review {self.sha} {counts} -->"

    @classmethod
    def parse(cls, body: str) -> "AIReview | None":
        match = _REVIEW_TAG.search(body)
        if not match:
            return None
        try:
            counts = json.loads(match.group(2))
            return cls(match.group(1), int(counts["blocking"]), int(counts["suggestions"]))
        except (ValueError, KeyError, TypeError):
            return None


def spec_branch(issue_number: int) -> str:
    return f"factory/spec/{issue_number}"


def code_branch(issue_number: int) -> str:
    return f"factory/impl/{issue_number}"


def spec_path(spec_directory: str, issue_number: int) -> str:
    # Keyed only by issue number so renaming the issue cannot orphan the spec.
    return f"{spec_directory}/issue-{issue_number}.md"


def changes_requested(pr: PullRequest | None, reviews: list[Review]) -> bool:
    """True if the latest decisive review is CHANGES_REQUESTED on the current head commit."""
    if pr is None:
        return False
    decisive = [r for r in reviews if r.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")]
    if not decisive:
        return False
    latest = max(decisive, key=lambda r: r.submitted_at or datetime.min)
    return latest.state == "CHANGES_REQUESTED" and latest.commit_id == pr.head_sha


class CheckState(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class CheckSummary:
    state: CheckState
    checks: tuple[Check, ...]
    failed: tuple[Check, ...] = ()
    missing: tuple[str, ...] = ()


def summarize_checks(checks: list[Check], required: tuple[str, ...]) -> CheckSummary:
    # The API lists newest runs first; keep the newest result per check name.
    latest: dict[str, Check] = {}
    for check in checks:
        latest.setdefault(check.name, check)
    considered = [latest[n] for n in required if n in latest] if required else list(latest.values())
    missing = tuple(n for n in required if n not in latest)
    failed = tuple(
        c for c in considered
        if c.status == "completed" and c.conclusion not in PASSING_CONCLUSIONS
    )
    if failed:
        state = CheckState.FAILURE
    elif missing or not considered or any(c.status != "completed" for c in considered):
        state = CheckState.PENDING
    else:
        state = CheckState.SUCCESS
    return CheckSummary(state, tuple(considered), failed, missing)


class ReleaseState(StrEnum):
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    DEPLOYED = "deployed"


@dataclass(frozen=True)
class ReleaseStatus:
    state: ReleaseState
    candidate: str
    detail: str = ""


@dataclass(frozen=True)
class Snapshot:
    """Everything the decision needs, read from GitHub just before deciding."""

    issue: Issue
    label_state: LabelState
    factory_login: str
    now: datetime
    label_actor: str | None = None
    label_applied_at: datetime | None = None
    actor_authorized: bool = False
    marker: StateMarker | None = None
    # Comments by users with write access (excluding the factory), oldest first.
    authorized_comments: tuple[Comment, ...] = ()
    spec_pr: PullRequest | None = None
    spec_changes_requested: bool = False
    spec_file_on_default: bool = False
    code_pr: PullRequest | None = None
    code_changes_requested: bool = False
    checks: CheckSummary | None = None
    head_committed_at: datetime | None = None
    # Container verification: no VERIFY_CONTEXT status exists yet on the head commit.
    needs_local_verification: bool = False
    # Output tail of a failed container verification, fed to the next attempt.
    verification_log: str | None = None
    release: ReleaseStatus | None = None
    # Automated review (agent.review): the factory's review of the stage's PR head, if any.
    review_enabled: bool = False
    ai_review: AIReview | None = None
    # A maintainer has reviewed the stage's PR: from then on they are its reviewer.
    human_reviewed: bool = False
    # Triage (agent.triage): the factory's triage comments on the issue, oldest first.
    triage_enabled: bool = False
    triage_notes: tuple[Comment, ...] = ()


class Action(StrEnum):
    WAIT = "wait"  # nothing to do until a human or CI acts
    SKIP = "skip"  # not safe to act (unauthorized, anomaly); reported only
    TRANSITION = "transition"
    RUN_SPEC = "run_spec"
    RUN_IMPLEMENT = "run_implement"
    RUN_VERIFY = "run_verify"
    RUN_REVIEW = "run_review"
    RUN_TRIAGE = "run_triage"
    ANNOUNCE_CANDIDATE = "announce_candidate"
    CLOSE = "close"


AGENT_ACTIONS = frozenset({Action.RUN_SPEC, Action.RUN_IMPLEMENT, Action.RUN_REVIEW,
                           Action.RUN_TRIAGE})
# Long-running actions; at most runtime.max_agent_runs_per_poll of them per poll.
HEAVY_ACTIONS = AGENT_ACTIONS | {Action.RUN_VERIFY}


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    stage: Stage | None = None
    target: Stage | None = None
    # The marker to persist with this action (transitions and agent runs).
    marker: StateMarker | None = field(default=None, compare=False)

    @property
    def mutates(self) -> bool:
        return self.action not in (Action.WAIT, Action.SKIP)


def effective_marker(snap: Snapshot, stage: Stage) -> StateMarker:
    """Reconcile the stored marker with the label, which is authoritative for the stage."""
    marker = snap.marker
    if marker is None:
        return StateMarker(stage=stage)
    if marker.stage == stage:
        return marker
    relabelled_by_human = (
        snap.label_actor != snap.factory_login
        and snap.label_applied_at is not None
        and (marker.updated_at_time is None or snap.label_applied_at > marker.updated_at_time)
    )
    if relabelled_by_human:
        # A human moved the issue: keep pointers to artifacts, reset counters and rework.
        return StateMarker(
            stage=stage, spec_pr=marker.spec_pr, code_pr=marker.code_pr,
            merge_sha=marker.merge_sha, candidate_posted=marker.candidate_posted,
            fast_track=marker.fast_track,
        )
    # An interrupted factory transition: the label never changed, so re-decide from it.
    return marker.evolve(stage=stage)


def decide(snap: Snapshot, policy: PolicyConfig) -> Decision:
    state = snap.label_state
    if not state.is_factory_issue:
        return Decision(Action.SKIP, "no lifecycle label")
    if state.is_conflicting:
        names = ", ".join(s.value for s in state.stages)
        marker = (snap.marker or StateMarker(stage=Stage.BLOCKED)).evolve(stage=Stage.BLOCKED)
        return Decision(
            Action.TRANSITION, f"conflicting lifecycle labels: {names}",
            stage=None, target=Stage.BLOCKED, marker=marker,
        )
    stage = state.stage
    assert stage is not None
    if not snap.actor_authorized:
        who = snap.label_actor or "unknown"
        return Decision(
            Action.SKIP, f"label applied by {who}, who lacks write access", stage=stage,
        )
    marker = effective_marker(snap, stage)
    handler = _HANDLERS[stage]
    return handler(snap, marker, policy)


def _wait(stage: Stage, reason: str) -> Decision:
    return Decision(Action.WAIT, reason, stage=stage)


def _move(stage: Stage, target: Stage, reason: str, marker: StateMarker) -> Decision:
    require_transition(stage, target)
    return Decision(Action.TRANSITION, reason, stage=stage, target=target,
                    marker=marker.evolve(stage=target))


def _block(stage: Stage, reason: str, marker: StateMarker) -> Decision:
    return _move(stage, Stage.BLOCKED, reason, marker)


def _needs_rework(pr: PullRequest | None, marker: StateMarker, requested: bool) -> bool:
    return pr is not None and (requested or marker.rework_base == pr.head_sha)


def _fresh_budget(pr: PullRequest, marker: StateMarker) -> StateMarker:
    """A human's new change request starts a fresh attempt budget: only failed runs use it.

    rework_base on the head means a revision for this request already ran and changed
    nothing; those runs keep counting, so the issue still blocks.
    """
    if marker.rework_base == pr.head_sha:
        return marker
    return marker.evolve(attempt=0, review_round=0, last_failure=None)


def _review_gate(
    snap: Snapshot, stage: Stage, pr: PullRequest, marker: StateMarker, policy: PolicyConfig,
    *, rework_allowed: bool,
) -> Decision | None:
    """Automated review before a human gate. None: proceed to the human gate.

    Returns RUN_REVIEW when the head has no review yet and rounds remain, or a TRANSITION
    placeholder (target unset) carrying the reason when blocking findings should be reworked.
    Each review's blocking findings are reworked once; the revision goes to the humans
    without another review once max_review_rounds reviews ran. The review is advisory and
    only precedes the first human review: after that, the maintainer is the reviewer.
    """
    if not snap.review_enabled or snap.human_reviewed:
        return None
    review = snap.ai_review
    if review is None:
        if marker.review_round >= policy.max_review_rounds:
            return None
        return Decision(Action.RUN_REVIEW,
                        f"automated review of PR #{pr.number} (round {marker.review_round + 1})",
                        stage=stage, marker=marker.evolve(review_round=marker.review_round + 1))
    if review.blocking and rework_allowed:
        return Decision(Action.TRANSITION,
                        f"automated review found {review.blocking} blocking issue(s) "
                        f"on PR #{pr.number}", stage=stage)
    return None


def _review_note(snap: Snapshot, marker: StateMarker) -> str:
    if not snap.review_enabled or snap.human_reviewed:
        return ""
    review = snap.ai_review
    if review is None:
        if marker.review_round:
            return "; revised after the automated review's findings"
        return "; automated review unavailable"
    if review.blocking:
        return (f"; automated review: {review.blocking} blocking issue(s) remain after "
                f"{marker.review_round} round(s)")
    return f"; automated review: no blocking issues ({review.suggestions} suggestion(s))"


def _decide_spec(snap: Snapshot, marker: StateMarker, policy: PolicyConfig) -> Decision:
    pr = snap.spec_pr
    stage = Stage.SPEC
    if pr is not None and pr.closed_unmerged:
        return _block(stage, f"spec PR #{pr.number} was closed without merging", marker)
    if pr is not None and pr.merged:
        return _move(stage, Stage.SPEC_REVIEW, f"spec PR #{pr.number} is already merged",
                     marker.evolve(spec_pr=pr.number))
    if pr is not None and not _needs_rework(pr, marker, snap.spec_changes_requested):
        marker = marker.evolve(spec_pr=pr.number)
        gate = _review_gate(snap, stage, pr, marker, policy,
                            rework_allowed=marker.attempt < policy.max_spec_attempts)
        if gate is not None:
            if gate.action is not Action.TRANSITION:
                return gate
            # Blocking findings: revise the spec with the review as feedback.
            return Decision(Action.RUN_SPEC, gate.reason, stage=stage,
                            marker=marker.evolve(attempt=marker.attempt + 1,
                                                 rework_base=pr.head_sha))
        return _move(stage, Stage.SPEC_REVIEW,
                     f"spec PR #{pr.number} is open for review{_review_note(snap, marker)}",
                     marker.evolve(attempt=0, rework_base=None, review_round=0))
    if pr is not None and snap.spec_changes_requested:
        marker = _fresh_budget(pr, marker)
    if pr is None and snap.triage_enabled and not marker.triaged:
        if marker.triage_round < policy.max_triage_rounds:
            # Discuss the issue in its comments first; answers resume spec via needs_input.
            return Decision(Action.RUN_TRIAGE,
                            f"triage the issue (round {marker.triage_round + 1})", stage=stage,
                            marker=marker.evolve(triage_round=marker.triage_round + 1))
        marker = marker.evolve(triaged=True)  # rounds used up: draft with its assumptions
    if pr is None and marker.fast_track and marker.triaged:
        return _move(stage, Stage.IMPLEMENT, "small task: the code PR carries the spec, and "
                     "merging it approves both", marker.evolve(attempt=0, rework_base=None))
    if marker.attempt >= policy.max_spec_attempts:
        detail = f": {marker.last_failure}" if marker.last_failure else ""
        return _block(stage, f"spec drafting failed {marker.attempt} times{detail}", marker)
    # A revision answers the feedback on the PR's current head: rework_base selects it.
    return Decision(Action.RUN_SPEC, "draft the specification" if pr is None
                    else f"revise spec PR #{pr.number}", stage=stage,
                    marker=marker.evolve(attempt=marker.attempt + 1,
                                         rework_base=pr.head_sha if pr else None))


def _decide_spec_review(snap: Snapshot, marker: StateMarker, policy: PolicyConfig) -> Decision:
    pr = snap.spec_pr
    stage = Stage.SPEC_REVIEW
    if pr is None:
        return _block(stage, "no spec PR found for this issue", marker)
    if pr.merged:
        if pr.merged_by == snap.factory_login:
            return _block(stage, f"spec PR #{pr.number} was merged by the factory identity; "
                                 "human approval is required", marker)
        if not snap.spec_file_on_default:
            return _block(stage, f"spec PR #{pr.number} is merged but the spec file is not on "
                                 "the default branch", marker)
        return _move(stage, Stage.IMPLEMENT,
                     f"spec PR #{pr.number} approved and merged by {pr.merged_by}",
                     marker.evolve(spec_pr=pr.number, attempt=0, rework_base=None,
                                   last_failure=None))
    if pr.closed_unmerged:
        return _block(stage, f"spec PR #{pr.number} was closed without merging", marker)
    if snap.spec_changes_requested:
        return _move(stage, Stage.SPEC, f"changes requested on spec PR #{pr.number}",
                     marker.evolve(rework_base=pr.head_sha, attempt=0, review_round=0))
    return _wait(stage, f"waiting for a human to merge spec PR #{pr.number}")


def _decide_implement(snap: Snapshot, marker: StateMarker, policy: PolicyConfig) -> Decision:
    pr = snap.code_pr
    stage = Stage.IMPLEMENT
    if not snap.spec_file_on_default and not marker.fast_track:
        return _block(stage, "the approved spec is not on the default branch", marker)
    if pr is not None and pr.closed_unmerged:
        return _block(stage, f"code PR #{pr.number} was closed without merging", marker)
    if pr is not None and (pr.merged or not _needs_rework(pr, marker, snap.code_changes_requested)):
        return _move(stage, Stage.VERIFY, f"code PR #{pr.number} is ready for verification",
                     marker.evolve(code_pr=pr.number, rework_base=None))
    if pr is not None and snap.code_changes_requested:
        marker = _fresh_budget(pr, marker)
    if marker.attempt >= policy.max_implementation_attempts:
        detail = f": {marker.last_failure}" if marker.last_failure else ""
        return _block(stage, f"implementation failed {marker.attempt} times{detail}", marker)
    return Decision(Action.RUN_IMPLEMENT, "implement the approved spec" if pr is None
                    else f"rework code PR #{pr.number}", stage=stage,
                    marker=marker.evolve(attempt=marker.attempt + 1,
                                         rework_base=pr.head_sha if pr else None))


def _decide_verify(snap: Snapshot, marker: StateMarker, policy: PolicyConfig) -> Decision:
    pr = snap.code_pr
    stage = Stage.VERIFY
    if pr is None or pr.closed_unmerged:
        return _block(stage, "code PR is missing or was closed without merging", marker)
    if pr.merged:
        return _move(stage, Stage.REVIEW, f"code PR #{pr.number} is already merged",
                     marker.evolve(verified_sha=pr.head_sha))
    if snap.needs_local_verification:
        return Decision(Action.RUN_VERIFY, f"run verification on {pr.head_sha[:12]}",
                        stage=stage, marker=marker)
    checks = snap.checks
    assert checks is not None, "observer must load checks for verify"
    if checks.state is CheckState.SUCCESS:
        gate = _review_gate(snap, stage, pr, marker, policy,
                            rework_allowed=marker.attempt < policy.max_implementation_attempts)
        if gate is not None:
            if gate.action is not Action.TRANSITION:
                return gate
            return _move(stage, Stage.IMPLEMENT, gate.reason,
                         marker.evolve(rework_base=pr.head_sha, last_failure=None))
        return _move(stage, Stage.REVIEW,
                     f"all {len(checks.checks)} checks passed on {pr.head_sha[:12]}"
                     f"{_review_note(snap, marker)}",
                     marker.evolve(verified_sha=pr.head_sha, last_failure=None))
    if checks.state is CheckState.FAILURE:
        failure = "; ".join(
            f"{c.name}: {c.conclusion}" + (f" ({c.summary})" if c.summary else "")
            for c in checks.failed
        )[:1000]
        detail = failure
        if snap.verification_log:
            detail += f"\n\nVerification output (tail):\n{snap.verification_log[-20_000:]}"
        if marker.attempt >= policy.max_implementation_attempts:
            return _block(stage, f"checks failed and no attempts remain: {failure}",
                          marker.evolve(last_failure=detail))
        return _move(stage, Stage.IMPLEMENT, f"checks failed: {failure}",
                     marker.evolve(rework_base=pr.head_sha, last_failure=detail))
    started = snap.head_committed_at
    if started and snap.now - started > timedelta(minutes=policy.checks_timeout_minutes):
        what = f"missing {', '.join(checks.missing)}" if checks.missing else "still pending"
        return _block(stage, f"checks {what} after {policy.checks_timeout_minutes} minutes",
                      marker)
    return _wait(stage, f"waiting for checks on {pr.head_sha[:12]}")


def _decide_review(snap: Snapshot, marker: StateMarker, policy: PolicyConfig) -> Decision:
    pr = snap.code_pr
    stage = Stage.REVIEW
    if pr is None or pr.closed_unmerged:
        return _block(stage, "code PR is missing or was closed without merging", marker)
    if pr.merged:
        if pr.merged_by == snap.factory_login:
            return _block(stage, f"code PR #{pr.number} was merged by the factory identity; "
                                 "human approval is required", marker)
        return _move(stage, Stage.RELEASE,
                     f"code PR #{pr.number} approved and merged by {pr.merged_by}",
                     marker.evolve(merge_sha=pr.merge_commit_sha, candidate_posted=False))
    if marker.verified_sha != pr.head_sha:
        return _move(stage, Stage.VERIFY, f"head of code PR #{pr.number} changed since "
                                          "verification", marker)
    if snap.code_changes_requested:
        return _move(stage, Stage.IMPLEMENT, f"changes requested on code PR #{pr.number}",
                     marker.evolve(rework_base=pr.head_sha, attempt=0, last_failure=None,
                                   review_round=0))
    return _wait(stage, f"waiting for a human to review code PR #{pr.number}")


def _decide_release(snap: Snapshot, marker: StateMarker, policy: PolicyConfig) -> Decision:
    stage = Stage.RELEASE
    release = snap.release
    if release is None:
        return _block(stage, "the merged code PR or its merge commit could not be found", marker)
    if release.state is ReleaseState.FAILED:
        return _block(stage, f"release candidate failed: {release.detail}", marker)
    if release.state is ReleaseState.BUILDING:
        detail = f" ({release.detail})" if release.detail else ""
        return _wait(stage, f"release candidate {release.candidate} is building{detail}")
    if release.state is ReleaseState.DEPLOYED:
        return Decision(Action.CLOSE, f"{release.candidate} deployed to production: "
                                      f"{release.detail}", stage=stage, marker=marker)
    if not marker.candidate_posted:
        return Decision(Action.ANNOUNCE_CANDIDATE, f"release candidate {release.candidate} "
                        "is ready", stage=stage, marker=marker.evolve(candidate_posted=True))
    return _wait(stage, f"waiting for human-approved deployment of {release.candidate}")


def _decide_needs_input(snap: Snapshot, marker: StateMarker, policy: PolicyConfig) -> Decision:
    stage = Stage.NEEDS_INPUT
    resume = marker.resume_stage
    if resume not in (Stage.SPEC, Stage.IMPLEMENT):
        return _block(stage, "cannot tell which stage to resume; relabel the issue manually",
                      marker)
    asked = marker.asked_at_time
    answers = [c for c in snap.authorized_comments if asked is None or c.created_at > asked]
    if not answers:
        return _wait(stage, "waiting for a maintainer to answer the factory's question")
    pr = snap.spec_pr if resume is Stage.SPEC else snap.code_pr
    # Answers to the triage's questions go straight to the spec: a confirming round
    # would only repeat them.
    triaged = marker.triaged or (resume is Stage.SPEC and marker.triage_round > 0)
    return _move(stage, resume, f"{answers[-1].author} answered",
                 marker.evolve(resume_stage=None, asked_at=None, triaged=triaged,
                               rework_base=pr.head_sha if pr and pr.is_open else None))


def _decide_blocked(snap: Snapshot, marker: StateMarker, policy: PolicyConfig) -> Decision:
    return _wait(Stage.BLOCKED, "blocked; a human must fix the cause and relabel the issue")


_HANDLERS = {
    Stage.SPEC: _decide_spec,
    Stage.SPEC_REVIEW: _decide_spec_review,
    Stage.IMPLEMENT: _decide_implement,
    Stage.VERIFY: _decide_verify,
    Stage.REVIEW: _decide_review,
    Stage.RELEASE: _decide_release,
    Stage.NEEDS_INPUT: _decide_needs_input,
    Stage.BLOCKED: _decide_blocked,
}
