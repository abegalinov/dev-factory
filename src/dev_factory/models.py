"""Plain data snapshots of GitHub objects. Parsed once at the client boundary."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class User:
    login: str
    id: int

    @property
    def noreply_email(self) -> str:
        return f"{self.id}+{self.login}@users.noreply.github.com"


@dataclass(frozen=True)
class Label:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    url: str
    updated_at: datetime
    author: str
    is_pull_request: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Issue":
        return cls(
            number=int(data["number"]),
            title=data.get("title") or "",
            body=data.get("body") or "",
            state=data.get("state", "open"),
            labels=tuple(label["name"] for label in data.get("labels", [])),
            url=data.get("html_url", ""),
            updated_at=parse_time(data["updated_at"]),
            author=(data.get("user") or {}).get("login", ""),
            is_pull_request="pull_request" in data,
        )


@dataclass(frozen=True)
class LabelEvent:
    action: str  # "labeled" | "unlabeled"
    label: str
    actor: str
    created_at: datetime


@dataclass(frozen=True)
class Comment:
    id: int
    author: str
    body: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Comment":
        return cls(
            id=int(data["id"]),
            author=(data.get("user") or {}).get("login", ""),
            body=data.get("body") or "",
            created_at=parse_time(data["created_at"]),
            updated_at=parse_time(data.get("updated_at") or data["created_at"]),
        )


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str  # "open" | "closed"
    merged: bool
    merged_by: str | None
    merge_commit_sha: str | None
    head_ref: str
    head_sha: str
    base_ref: str
    url: str

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def closed_unmerged(self) -> bool:
        return self.state == "closed" and not self.merged

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PullRequest":
        merged = bool(data.get("merged") or data.get("merged_at"))
        return cls(
            number=int(data["number"]),
            state=data["state"],
            merged=merged,
            merged_by=(data.get("merged_by") or {}).get("login"),
            merge_commit_sha=data.get("merge_commit_sha") if merged else None,
            head_ref=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            base_ref=data["base"]["ref"],
            url=data.get("html_url", ""),
        )


@dataclass(frozen=True)
class Review:
    author: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
    commit_id: str
    submitted_at: datetime | None
    body: str = ""


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None  # success | failure | neutral | skipped | cancelled | timed_out | ...
    summary: str = ""
    url: str = ""
    job_id: int | None = None  # GitHub Actions job behind this check run, if any


@dataclass(frozen=True)
class WorkflowRun:
    path: str
    name: str
    status: str
    conclusion: str | None
    url: str


@dataclass(frozen=True)
class Deployment:
    id: int
    sha: str
    environment: str
    state: str | None  # latest status state, e.g. success | failure | in_progress
