"""The factory-owned state marker comment. See docs/issue-contract.md#state-marker."""

import json
import re
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime

from dev_factory.models import Comment
from dev_factory.state_machine import Stage

MARKER_VERSION = 1
_PATTERN = re.compile(r"<!-- dev-factory:state (\{.*?\}) -->", re.DOTALL)


@dataclass(frozen=True)
class StateMarker:
    stage: Stage
    attempt: int = 0
    resume_stage: Stage | None = None
    # PR head SHA at the time rework was requested; rework is due while the head is unchanged.
    rework_base: str | None = None
    last_failure: str | None = None
    spec_pr: int | None = None
    code_pr: int | None = None
    verified_sha: str | None = None
    merge_sha: str | None = None
    asked_at: str | None = None  # ISO timestamp of the needs-input question
    candidate_posted: bool = False
    # Automated review rounds started in the current spec or code cycle.
    review_round: int = 0
    # Triage (agent.triage): rounds posted, and whether the spec may be drafted.
    triage_round: int = 0
    triaged: bool = False
    # The triage sized the task small (agent.fast_track): the spec comes in the code PR.
    fast_track: bool = False
    # Default-branch commit of the repository's .dev-factory/ config last used for this issue.
    config_sha: str | None = None
    updated_at: str | None = None

    def evolve(self, **changes: object) -> "StateMarker":
        return replace(self, **changes)

    @property
    def asked_at_time(self) -> datetime | None:
        return datetime.fromisoformat(self.asked_at) if self.asked_at else None

    @property
    def updated_at_time(self) -> datetime | None:
        return datetime.fromisoformat(self.updated_at) if self.updated_at else None


def render(marker: StateMarker, summary: str) -> str:
    data = {"v": MARKER_VERSION, **asdict(marker)}
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    # "--" cannot appear inside an HTML comment; JSON never needs it unescaped.
    payload = payload.replace("--", "-\\u002d")
    return f"<!-- dev-factory:state {payload} -->\n{summary}"


def parse(body: str) -> StateMarker | None:
    match = _PATTERN.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.pop("v", None) != MARKER_VERSION:
        return None
    known = {f.name for f in fields(StateMarker)}
    data = {k: v for k, v in data.items() if k in known}
    try:
        data["stage"] = Stage(data["stage"])
        if data.get("resume_stage"):
            data["resume_stage"] = Stage(data["resume_stage"])
        return StateMarker(**data)
    except (KeyError, ValueError, TypeError):
        return None


def find(comments: list[Comment], factory_login: str) -> tuple[Comment, StateMarker] | None:
    """The most recently updated valid marker authored by the factory identity."""
    found = [
        (c, m) for c in comments
        if c.author == factory_login and (m := parse(c.body)) is not None
    ]
    return max(found, key=lambda pair: pair[0].updated_at) if found else None
