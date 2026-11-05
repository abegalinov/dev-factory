"""GitHub access through `gh api`. Reads are retried; mutations never are."""

import base64
import json
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dev_factory.models import (
    Check,
    Comment,
    Deployment,
    Issue,
    Label,
    LabelEvent,
    PullRequest,
    Review,
    User,
    WorkflowRun,
    parse_time,
)

log = logging.getLogger(__name__)

_TRANSIENT = re.compile(
    r"HTTP (429|500|502|503|504)|rate limit|timed out|timeout|connection (reset|refused)"
    r"|could not resolve host|TLS handshake|EOF",
    re.IGNORECASE,
)

# `gh api --slurp` (paginated reads) arrived in 2.48; distribution packages can be older.
MIN_GH_VERSION = (2, 48)
_VERSION = re.compile(r"gh version (\d+)\.(\d+)")

# GitHub check conclusions that count as passing.
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


class GitHubCLIError(RuntimeError):
    pass


class NotFoundError(GitHubCLIError):
    pass


class GitHubCLI:
    def __init__(
        self,
        executable: str = "gh",
        timeout_seconds: int = 60,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._sleep = sleep
        self._user: User | None = None

    # -- transport -----------------------------------------------------------

    def ensure_available(self) -> None:
        if shutil.which(self.executable) is None:
            raise GitHubCLIError(f"GitHub CLI not found: {self.executable}")
        version = _VERSION.search(self.run(["--version"], check=False).stdout)
        if version is None or tuple(map(int, version.groups())) < MIN_GH_VERSION:
            found = ".".join(version.groups()) if version else "unknown"
            need = ".".join(map(str, MIN_GH_VERSION))
            raise GitHubCLIError(f"GitHub CLI {found} is too old: {need}+ is needed (install it "
                                 "from https://cli.github.com, not the distribution package)")
        result = self.run(["auth", "status"], check=False)
        if result.returncode != 0:
            raise GitHubCLIError("GitHub CLI is not authenticated for the current identity")

    def run(
        self, args: list[str], *, check: bool = True, stdin: str | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.executable, *args],
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout or self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitHubCLIError(f"gh {' '.join(args[:3])} timed out") from exc
        if check and result.returncode != 0:
            stderr = result.stderr.strip()
            error = NotFoundError if "HTTP 404" in stderr else GitHubCLIError
            raise error(f"gh {' '.join(args[:3])} failed ({result.returncode}): {stderr}")
        return result

    def _get(self, path: str, *, paginate: bool = False) -> Any:
        args = ["api", "-H", "Accept: application/vnd.github+json", path]
        if paginate:
            args[1:1] = ["--paginate", "--slurp"]
        for attempt in range(self.max_retries + 1):
            try:
                result = self.run(args)
                break
            except NotFoundError:
                raise
            except GitHubCLIError as exc:
                if attempt >= self.max_retries or not _TRANSIENT.search(str(exc)):
                    raise
                delay = 2 ** attempt
                log.warning("transient GitHub error, retrying", extra={
                    "path": path, "attempt": attempt + 1, "delay": delay, "error": str(exc),
                })
                self._sleep(delay)
        try:
            data = json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise GitHubCLIError(f"gh returned invalid JSON for {path}") from exc
        if paginate:
            # --slurp wraps each page; pages are lists or objects holding a list.
            items: list[Any] = []
            for page in data:
                items.extend(page if isinstance(page, list) else _page_items(page))
            return items
        return data

    def _get_or_none(self, path: str) -> Any:
        try:
            return self._get(path)
        except NotFoundError:
            return None

    def _mutate(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        args = ["api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
        stdin = None
        if payload is not None:
            args += ["--input", "-"]
            stdin = json.dumps(payload)
        result = self.run(args, stdin=stdin)
        return json.loads(result.stdout) if result.stdout.strip() else None

    # -- identity and repository ---------------------------------------------

    def current_user(self) -> User:
        if self._user is None:
            data = self._get("user")
            self._user = User(login=data["login"], id=int(data["id"]))
        return self._user

    def repository(self, repo: str) -> dict[str, Any]:
        return self._get(f"repos/{repo}")

    def permission(self, repo: str, login: str) -> str:
        """Returns admin | write | read | none (maintain maps to write, triage to read)."""
        data = self._get_or_none(f"repos/{repo}/collaborators/{quote(login)}/permission")
        return (data or {}).get("permission", "none")

    def branch(self, repo: str, branch: str) -> dict[str, Any] | None:
        return self._get_or_none(f"repos/{repo}/branches/{quote(branch, safe='')}")

    def default_branch(self, repo: str) -> str:
        return str(self.repository(repo)["default_branch"])

    def file_text(self, repo: str, path: str, ref: str) -> str | None:
        """A file's text at ref, or None if it does not exist."""
        data = self._get_or_none(f"repos/{repo}/contents/{quote(path)}?ref={quote(ref, safe='')}")
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        return base64.b64decode(data.get("content") or "").decode("utf-8")

    def file_exists(self, repo: str, path: str, ref: str) -> bool:
        data = self._get_or_none(f"repos/{repo}/contents/{quote(path)}?ref={quote(ref, safe='')}")
        return isinstance(data, dict) and data.get("type") == "file"

    # -- labels ----------------------------------------------------------------

    def list_labels(self, repo: str) -> list[Label]:
        return [
            Label(d["name"], d.get("color", ""), d.get("description") or "")
            for d in self._get(f"repos/{repo}/labels?per_page=100", paginate=True)
        ]

    def create_label(self, repo: str, name: str, color: str, description: str) -> None:
        self._mutate("POST", f"repos/{repo}/labels", {
            "name": name, "color": color, "description": description,
        })

    # -- issues ----------------------------------------------------------------

    def list_open_issues(self, repo: str, label: str) -> list[Issue]:
        path = f"repos/{repo}/issues?state=open&labels={quote(label, safe='')}&per_page=100"
        return [Issue.from_api(d) for d in self._get(path, paginate=True)]

    def get_issue(self, repo: str, number: int) -> Issue:
        return Issue.from_api(self._get(f"repos/{repo}/issues/{number}"))

    def label_events(self, repo: str, number: int) -> list[LabelEvent]:
        events = self._get(f"repos/{repo}/issues/{number}/events?per_page=100", paginate=True)
        return [
            LabelEvent(
                action=e["event"],
                label=e["label"]["name"],
                actor=(e.get("actor") or {}).get("login", ""),
                created_at=parse_time(e["created_at"]),
            )
            for e in events
            if e.get("event") in ("labeled", "unlabeled") and e.get("label")
        ]

    def list_comments(self, repo: str, number: int) -> list[Comment]:
        data = self._get(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True)
        return [Comment.from_api(d) for d in data]

    def create_comment(self, repo: str, number: int, body: str) -> Comment:
        data = self._mutate("POST", f"repos/{repo}/issues/{number}/comments", {"body": body})
        return Comment.from_api(data)

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        self._mutate("PATCH", f"repos/{repo}/issues/comments/{comment_id}", {"body": body})

    def set_labels(self, repo: str, number: int, labels: list[str]) -> None:
        """Replace all labels in one request, so a transition is atomic."""
        self._mutate("PUT", f"repos/{repo}/issues/{number}/labels", {"labels": labels})

    def close_issue(self, repo: str, number: int) -> None:
        self._mutate("PATCH", f"repos/{repo}/issues/{number}", {
            "state": "closed", "state_reason": "completed",
        })

    # -- pull requests -----------------------------------------------------------

    def find_pull_by_branch(self, repo: str, branch: str) -> PullRequest | None:
        owner = repo.split("/", 1)[0]
        head = quote(f"{owner}:{branch}", safe="")
        pulls = self._get(f"repos/{repo}/pulls?state=all&head={head}&per_page=100", paginate=True)
        if not pulls:
            return None
        # Prefer an open PR; otherwise the most recent one.
        pulls.sort(key=lambda p: (p["state"] == "open", p["number"]), reverse=True)
        return self.get_pull(repo, int(pulls[0]["number"]))

    def get_pull(self, repo: str, number: int) -> PullRequest:
        return PullRequest.from_api(self._get(f"repos/{repo}/pulls/{number}"))

    def create_pull(self, repo: str, *, head: str, base: str, title: str, body: str) -> PullRequest:
        data = self._mutate("POST", f"repos/{repo}/pulls", {
            "head": head, "base": base, "title": title, "body": body,
        })
        return PullRequest.from_api(data)

    def list_reviews(self, repo: str, number: int) -> list[Review]:
        data = self._get(f"repos/{repo}/pulls/{number}/reviews?per_page=100", paginate=True)
        return [
            Review(
                author=(r.get("user") or {}).get("login", ""),
                state=r.get("state", ""),
                commit_id=r.get("commit_id") or "",
                submitted_at=parse_time(r.get("submitted_at")),
                body=r.get("body") or "",
            )
            for r in data
        ]

    def create_review(self, repo: str, number: int, commit_id: str, body: str) -> None:
        """A COMMENT review: GitHub does not let a PR's author approve or request changes."""
        self._mutate("POST", f"repos/{repo}/pulls/{number}/reviews",
                     {"commit_id": commit_id, "body": body, "event": "COMMENT"})

    def compare_diff(self, repo: str, base: str, head: str, limit: int = 60_000) -> str:
        """Unified diff of head against its merge base with base, cut at limit characters."""
        data = self._get(f"repos/{repo}/compare/{quote(base, safe='')}...{head}")
        parts = []
        for f in data.get("files") or []:
            patch = f.get("patch")
            header = f"--- {f.get('filename')} ({f.get('status')}, +{f.get('additions')} " \
                     f"-{f.get('deletions')})"
            parts.append(f"{header}\n{patch}" if patch else f"{header}\n(no textual diff)")
        diff = "\n\n".join(parts)
        if len(diff) > limit:
            diff = diff[:limit] + "\n\n(diff truncated; read the files for the rest)"
        return diff

    def review_feedback(self, repo: str, number: int, commit_id: str) -> list[tuple[str, str]]:
        """(author, text) for review bodies and inline comments made on one commit."""
        feedback: list[tuple[str, str]] = []
        reviews = self._get(f"repos/{repo}/pulls/{number}/reviews?per_page=100", paginate=True)
        for r in reviews:
            if r.get("commit_id") == commit_id and (r.get("body") or "").strip():
                feedback.append(((r.get("user") or {}).get("login", ""), r["body"]))
        inline = self._get(f"repos/{repo}/pulls/{number}/comments?per_page=100", paginate=True)
        for c in inline:
            if commit_id in (c.get("commit_id"), c.get("original_commit_id")):
                where = f"{c.get('path')}:{c.get('line') or c.get('original_line') or '?'}"
                feedback.append(((c.get("user") or {}).get("login", ""), f"{where}: {c['body']}"))
        return feedback

    # -- commits, checks, releases ----------------------------------------------

    def commit_date(self, repo: str, sha: str) -> datetime:
        data = self._get(f"repos/{repo}/commits/{sha}")
        return parse_time(data["commit"]["committer"]["date"])

    def checks(self, repo: str, sha: str) -> list[Check]:
        runs = self._get(f"repos/{repo}/commits/{sha}/check-runs?per_page=100", paginate=True)
        checks = [
            Check(
                name=r["name"],
                status=r["status"],
                conclusion=r.get("conclusion"),
                summary=((r.get("output") or {}).get("title") or "")[:300],
                url=r.get("html_url") or "",
                job_id=_actions_job_id(r),
            )
            for r in runs
        ]
        # Legacy commit statuses (e.g. external CI) are folded in as checks.
        status = self._get(f"repos/{repo}/commits/{sha}/status")
        for s in status.get("statuses", []):
            state = s["state"]
            checks.append(Check(
                name=s["context"],
                status="completed" if state != "pending" else "in_progress",
                conclusion={"success": "success", "pending": None}.get(state, "failure"),
                summary=(s.get("description") or "")[:300],
                url=s.get("target_url") or "",
            ))
        return checks

    def job_log(self, repo: str, job_id: int) -> str:
        """Plain-text log of a GitHub Actions job (raw, with timestamps and colors)."""
        # gh refuses to print logs containing terminal escapes unless allowed.
        args = ["api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"]
        return self.run(args).stdout

    def create_status(
        self, repo: str, sha: str, *, state: str, context: str, description: str,
        target_url: str | None = None,
    ) -> None:
        payload = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            payload["target_url"] = target_url
        self._mutate("POST", f"repos/{repo}/statuses/{sha}", payload)

    def workflow_runs(self, repo: str, sha: str) -> list[WorkflowRun]:
        data = self._get(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")
        return [
            WorkflowRun(
                path=r.get("path", ""), name=r.get("name", ""), status=r["status"],
                conclusion=r.get("conclusion"), url=r.get("html_url", ""),
            )
            for r in data.get("workflow_runs", [])
        ]

    # -- artifacts and release assets (demo videos) --------------------------

    def find_artifact(self, repo: str, sha: str, name: str) -> int | None:
        """The workflow run id of the newest unexpired artifact `name` built on sha."""
        data = self._get(f"repos/{repo}/actions/artifacts?name={quote(name)}&per_page=100")
        runs = [a["workflow_run"] for a in data.get("artifacts", [])
                if not a.get("expired") and (a.get("workflow_run") or {}).get("head_sha") == sha]
        return int(runs[0]["id"]) if runs else None

    def download_artifact(self, repo: str, run_id: int, name: str, dest: Path) -> None:
        self.run(["run", "download", str(run_id), "-R", repo, "-n", name, "-D", str(dest)],
                 timeout=600)

    def upload_release_asset(self, repo: str, tag: str, path: Path) -> str:
        """Uploads (or replaces) an asset on the pre-release `tag`, created if missing."""
        if self.run(["release", "view", tag, "-R", repo], check=False).returncode != 0:
            self.run(["release", "create", tag, "-R", repo, "--prerelease", "--latest=false",
                      "--title", "Dev Factory evidence",
                      "--notes", "Demo videos recorded by CI for factory pull requests. "
                                 "Managed by Dev Factory; not a product release."])
        self.run(["release", "upload", tag, str(path), "-R", repo, "--clobber"], timeout=600)
        return f"https://github.com/{repo}/releases/download/{quote(tag)}/{quote(path.name)}"

    def deployments(self, repo: str, environment: str) -> list[Deployment]:
        env = quote(environment, safe="")
        data = self._get(f"repos/{repo}/deployments?environment={env}&per_page=30")
        result = []
        for d in data:
            statuses = self._get(f"repos/{repo}/deployments/{d['id']}/statuses?per_page=1")
            result.append(Deployment(
                id=int(d["id"]), sha=d["sha"], environment=d.get("environment", environment),
                state=statuses[0]["state"] if statuses else None,
            ))
        return result

    def ahead_by(self, repo: str, base: str, head: str) -> int:
        """Commits on head that base lacks; 0 if head is unknown."""
        data = self._get_or_none(
            f"repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}")
        return int((data or {}).get("ahead_by", 0))

    def is_ancestor(self, repo: str, ancestor: str, descendant: str) -> bool:
        data = self._get_or_none(f"repos/{repo}/compare/{ancestor}...{descendant}")
        return bool(data) and data.get("status") in ("ahead", "identical")


def _page_items(page: dict[str, Any]) -> list[Any]:
    for key in ("check_runs", "workflow_runs", "items"):
        if key in page:
            return page[key]
    raise GitHubCLIError(f"Unexpected paginated object keys: {sorted(page)}")


_JOB_URL = re.compile(r"/actions/runs/\d+/job/(\d+)")


def _actions_job_id(run: dict[str, Any]) -> int | None:
    # For GitHub Actions the check run id is the job id; the URL confirms it is a job.
    match = _JOB_URL.search(run.get("html_url") or "")
    return int(match.group(1)) if match else None
