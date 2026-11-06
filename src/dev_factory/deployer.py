"""The deployer: applies a repository's production manifest on the production host.

It runs on the production host (docs/deployer.md), never on the factory host, and pulls:
GitHub holds no credentials for the host and never connects to it. Every pass, per target:

1. observe: the head of the default branch, the repository's release.deploy settings and the
   manifest at that commit, through the GitHub API (one file each, never a checkout);
2. decide (pure, `decide`): a new manifest is applied only if the commit that last changed it
   belongs to a PR into the default branch merged by one of the host's approvers, it names a
   release commit, and every image is pinned under the host's allowed prefixes;
3. act: record a GitHub deployment of the release commit, apply the manifest with the runtime
   (swarm.py), verify it and report success or failure.

Each manifest is tried once. A failure, a rejection or a pass that died while applying is
not repeated until `dev-factory deployer retry`; side effects are never blindly repeated.
While the applied manifest is current, services that drifted from it are put back.

Trust stays on the host: approvers, allowed images, tokens and the work directory come from
the host's deployer.yaml; the repository only says which file to apply and how.
"""

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Protocol

import yaml

from dev_factory.config import ConfigError, DeployConfig
from dev_factory.repo_config import CONFIG_DIR, CONFIG_FILE, parse_deploy

log = logging.getLogger("dev_factory.deployer")

DEFAULT_CONFIG = Path("/etc/dev-factory/deployer.yaml")
STATE_DIR = ".deployer"  # inside each target's work directory


# -- host configuration ---------------------------------------------------------------------

@dataclass(frozen=True)
class RegistryLogin:
    """Credentials to pull private images, passed to the runtime for each apply only."""

    server: str
    username: str
    password_file: Path


@dataclass(frozen=True)
class Target:
    """One repository deployed on this host."""

    repository: str
    # Holds the env files and other host-only inputs the manifest refers to, the applied
    # manifest and the deployer's state. Mount it at the same path inside the container.
    workdir: Path
    approvers: tuple[str, ...]
    token_file: Path
    # Every image in the manifest must start with one of these. Empty: any image.
    image_prefixes: tuple[str, ...] = ()
    registry: RegistryLogin | None = None
    branch: str | None = None  # default: the repository's default branch


@dataclass(frozen=True)
class DeployerConfig:
    targets: tuple[Target, ...]
    interval_seconds: int = 30
    keep_releases: int = 5  # applied releases whose manifests and images are kept


_HOST_KEYS = {"interval_seconds", "keep_releases", "targets"}
_TARGET_KEYS = {f.name for f in fields(Target)}
_REGISTRY_KEYS = {f.name for f in fields(RegistryLogin)}


def load_config(path: Path) -> DeployerConfig:
    where = str(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{where}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must contain a YAML mapping")
    _strict(raw, _HOST_KEYS, where)
    targets = []
    for item in raw.get("targets") or []:
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: each target must be a mapping")
        _strict(item, _TARGET_KEYS, f"{where}: target")
        repo = str(item.get("repository") or "")
        if repo.count("/") != 1:
            raise ConfigError(f"{where}: target repository must be owner/name, got {repo!r}")
        approvers = item.get("approvers") or []
        if not approvers or not all(isinstance(a, str) and a for a in approvers):
            raise ConfigError(f"{where}: {repo}: approvers must list GitHub logins")
        for key in ("workdir", "token_file"):
            if not str(item.get(key) or "").startswith("/"):
                raise ConfigError(f"{where}: {repo}: {key} must be an absolute path")
        registry = item.get("registry")
        if registry is not None:
            if not isinstance(registry, dict):
                raise ConfigError(f"{where}: {repo}: registry must be a mapping")
            _strict(registry, _REGISTRY_KEYS, f"{where}: {repo}: registry")
            if set(registry) != _REGISTRY_KEYS:
                raise ConfigError(f"{where}: {repo}: registry needs {sorted(_REGISTRY_KEYS)}")
            registry = RegistryLogin(str(registry["server"]), str(registry["username"]),
                                     Path(registry["password_file"]))
        targets.append(Target(
            repository=repo,
            workdir=Path(item["workdir"]),
            approvers=tuple(approvers),
            token_file=Path(item["token_file"]),
            image_prefixes=tuple(str(p) for p in item.get("image_prefixes") or ()),
            registry=registry,
            branch=item.get("branch"),
        ))
    if not targets:
        raise ConfigError(f"{where}: no targets")
    if len({t.workdir for t in targets}) != len(targets):
        raise ConfigError(f"{where}: each target needs its own workdir")
    return DeployerConfig(
        targets=tuple(targets),
        interval_seconds=int(raw.get("interval_seconds", DeployerConfig.interval_seconds)),
        keep_releases=int(raw.get("keep_releases", DeployerConfig.keep_releases)),
    )


def _strict(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")


# -- GitHub ---------------------------------------------------------------------------------

class GitHubError(RuntimeError):
    pass


class GitHubAPI:
    """The few REST calls the deployer needs. Reads retry; writes never do."""

    def __init__(self, token: str, base: str = "https://api.github.com", *,
                 timeout: int = 30, retries: int = 3,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        self.token = token
        self.base = base
        self.timeout = timeout
        self.retries = retries
        self.opener = opener

    def _request(self, method: str, path: str, body: dict | None = None,
                 accept: str = "application/vnd.github+json") -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        attempts = self.retries if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                f"{self.base}/{path}", data=data, method=method,
                headers={"Authorization": f"Bearer {self.token}", "Accept": accept,
                         "X-GitHub-Api-Version": "2022-11-28",
                         **({"Content-Type": "application/json"} if data else {})})
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return 404, b""
                if exc.code < 500 or attempt == attempts:
                    raise GitHubError(f"{method} {path}: HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == attempts:
                    raise GitHubError(f"{method} {path}: {exc}") from exc
            time.sleep(2 * attempt)
        raise AssertionError("unreachable")

    def _json(self, path: str) -> Any:
        status, data = self._request("GET", path)
        return None if status == 404 else json.loads(data)

    def default_branch(self, repo: str) -> str:
        return self._json(f"repos/{repo}")["default_branch"]

    def head(self, repo: str, branch: str) -> str:
        status, data = self._request("GET", f"repos/{repo}/commits/{branch}",
                                     accept="application/vnd.github.sha")
        if status == 404:
            raise GitHubError(f"{repo}: branch {branch} not found")
        return data.decode().strip()

    def file(self, repo: str, path: str, ref: str) -> tuple[str, str] | None:
        """(blob sha, text) of a file at a commit, or None if it does not exist there."""
        item = self._json(f"repos/{repo}/contents/{path}?ref={ref}")
        if not item or item.get("type") != "file":
            return None
        return item["sha"], base64.b64decode(item["content"]).decode("utf-8")

    def last_change(self, repo: str, path: str, ref: str) -> str:
        commits = self._json(f"repos/{repo}/commits?path={path}&sha={ref}&per_page=1") or []
        if not commits:
            raise GitHubError(f"{repo}: no commit changed {path}")
        return commits[0]["sha"]

    def merged_by(self, repo: str, commit: str, branch: str) -> list[tuple[int, str]]:
        """(PR number, merger) of each PR into `branch` that contains `commit`."""
        result = []
        for pr in self._json(f"repos/{repo}/commits/{commit}/pulls") or []:
            if pr.get("merged_at") and pr.get("base", {}).get("ref") == branch:
                merger = (self._json(f"repos/{repo}/pulls/{pr['number']}") or {}).get(
                    "merged_by") or {}
                result.append((pr["number"], merger.get("login", "")))
        return result

    def create_deployment(self, repo: str, ref: str, environment: str,
                          description: str) -> int:
        _, data = self._request("POST", f"repos/{repo}/deployments", {
            "ref": ref, "environment": environment, "description": description,
            "auto_merge": False, "required_contexts": [], "production_environment": True})
        return int(json.loads(data)["id"])

    def deployment_status(self, repo: str, deployment: int, state: str, description: str,
                          url: str | None) -> None:
        body = {"state": state, "description": description[:140], "auto_inactive": True}
        if url:
            body["environment_url"] = url
        self._request("POST", f"repos/{repo}/deployments/{deployment}/statuses", body)


# -- runtime --------------------------------------------------------------------------------

class Runtime(Protocol):
    def apply(self, manifest: Path, stack: str, registry: RegistryLogin | None) -> str:
        """Apply the manifest; raises RuntimeFailure with the output on failure."""

    def problems(self, stack: str, images: dict[str, str], *, images_only: bool) -> list[str]:
        """What differs from `images` (service → image); [] when the stack matches."""

    def prune(self, keep: set[str], prefixes: set[str]) -> None:
        """Remove local images under `prefixes` that are not in `keep`."""


class RuntimeFailure(RuntimeError):
    pass


# -- decision -------------------------------------------------------------------------------

class Action(Enum):
    APPLY = "apply"
    IDLE = "idle"        # approved, but nothing released yet (x-release: none)
    REJECT = "reject"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    release: str = ""
    images: dict[str, str] = field(default_factory=dict)


def manifest_images(text: str) -> tuple[str, dict[str, str]]:
    """(x-release, service → image) of a manifest; raises ValueError if malformed."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict):
        raise ValueError("no services")  # noqa: TRY004 - one error type for a bad manifest
    images = {}
    for name, service in raw["services"].items():
        image = service.get("image") if isinstance(service, dict) else None
        if not isinstance(image, str) or not image:
            raise ValueError(f"service {name} has no image")
        images[str(name)] = image
    return str(raw.get("x-release") or ""), images


def decide(text: str, approvals: list[tuple[int, str]], target: Target) -> Decision:
    """Whether a new manifest may be applied. Pure: everything it needs is passed in."""
    approved = [(n, login) for n, login in approvals if login in target.approvers]
    if not approved:
        merged = ", ".join(f"#{n} by {login or '?'}" for n, login in approvals) or "no PR"
        return Decision(Action.REJECT, f"not merged by an approver ({merged}); approvers: "
                                       f"{', '.join(target.approvers)}")
    by = f"#{approved[0][0]} merged by {approved[0][1]}"
    try:
        release, images = manifest_images(text)
    except ValueError as exc:
        return Decision(Action.REJECT, f"{by}, but the manifest is malformed: {exc}")
    if release == "none":
        return Decision(Action.IDLE, f"{by}: nothing released yet (x-release: none)")
    if not (len(release) == 40 and all(c in "0123456789abcdef" for c in release)):
        return Decision(Action.REJECT, f"{by}, but x-release is not a commit sha: {release!r}")
    bad = [f"{name}: {image}" for name, image in images.items()
           if "$" in image or ":" not in image.rsplit("/", 1)[-1]
           or (target.image_prefixes and not image.startswith(target.image_prefixes))]
    if bad:
        under = f" under {', '.join(target.image_prefixes)}" if target.image_prefixes else ""
        return Decision(Action.REJECT, f"{by}, but images must be pinned{under}: "
                                       f"{'; '.join(bad)}")
    return Decision(Action.APPLY, by, release, images)


# -- state ----------------------------------------------------------------------------------

class Outcome(StrEnum):
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    REJECTED = "rejected"
    IDLE = "idle"


@dataclass
class State:
    """What this host did with the last manifest it saw (workdir/.deployer/state.json)."""

    head: str = ""        # last default-branch commit looked at
    blob: str = ""        # last manifest seen
    outcome: str = ""
    release: str = ""     # release of `blob`, if it got that far
    deployment: int = 0   # GitHub deployment of the current apply
    applied: str = ""     # last release applied successfully
    stack: str = ""       # ... its stack
    manifest: str = ""    # ... and the file it was applied from, in the work directory
    detail: str = ""
    unlinked: int = 0     # passes `blob` has waited for GitHub to link its commit to a PR

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        return cls(**{k: v for k, v in raw.items() if k in {f.name for f in fields(cls)}})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


# -- the deployer ---------------------------------------------------------------------------

UNLINKED_PASSES = 10  # how long a manifest change without a PR waits before it is rejected

class TargetRun:
    """One reconcile pass for one target."""

    def __init__(self, target: Target, gh: GitHubAPI, runtime: Runtime, *,
                 keep_releases: int = 5, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.target = target
        self.gh = gh
        self.runtime = runtime
        self.keep_releases = keep_releases
        self.now = now
        self.dir = target.workdir / STATE_DIR
        self.state_path = self.dir / "state.json"
        self.environment = "production"

    @property
    def repo(self) -> str:
        return self.target.repository

    def reconcile(self) -> State:
        state = State.load(self.state_path)
        branch = self.target.branch or self.gh.default_branch(self.repo)
        head = self.gh.head(self.repo, branch)
        if state.outcome == Outcome.APPLYING:
            # The previous pass died while applying. Don't repeat it blindly.
            self._finish(state, Outcome.FAILED, "interrupted while applying; "
                         "check the services, then `dev-factory deployer retry`")
        if head == state.head and state.outcome:
            self._repair(state)
            return state

        deploy = self.deploy_config(head)
        found = self.gh.file(self.repo, deploy.manifest, head)
        if found is None:
            raise ConfigError(f"{self.repo}: {deploy.manifest} not found at {head[:12]}")
        blob, text = found
        if blob == state.blob and state.outcome:
            state.head = head
            state.save(self.state_path)
            self._repair(state)
            return state

        # A new manifest: decide once, whatever happens next.
        commit = self.gh.last_change(self.repo, deploy.manifest, head)
        approvals = self.gh.merged_by(self.repo, commit, branch)
        if not approvals and state.unlinked < UNLINKED_PASSES:
            # GitHub links a merge commit to its PR seconds after the merge: look again.
            state.unlinked += 1
            state.save(self.state_path)
            log.info("%s: %s changed in %s, not linked to a PR yet", self.repo,
                     deploy.manifest, commit[:12])
            return state
        decision = decide(text, approvals, self.target)
        state = replace(state, head=head, blob=blob, release=decision.release, deployment=0,
                        unlinked=0,
                        detail=f"{deploy.manifest} changed in {commit[:12]}: {decision.reason}")
        if decision.action is Action.REJECT:
            log.warning("%s: not applied: %s", self.repo, state.detail)
            self._finish(state, Outcome.REJECTED, state.detail)
            return state
        if decision.action is Action.IDLE:
            log.info("%s: %s", self.repo, decision.reason)
            self._finish(state, Outcome.IDLE, decision.reason)
            return state
        self._apply(state, deploy, text, decision)
        return state

    def deploy_config(self, head: str) -> DeployConfig:
        found = self.gh.file(self.repo, f"{CONFIG_DIR}/{CONFIG_FILE}", head)
        where = f"{self.repo}:{CONFIG_DIR}/{CONFIG_FILE}@{head[:12]}"
        if found is None:
            raise ConfigError(f"{where} not found")
        raw = yaml.safe_load(found[1]) or {}
        release = raw.get("release") or {} if isinstance(raw, dict) else {}
        deploy = parse_deploy(release.get("deploy"), where)
        if deploy is None:
            raise ConfigError(f"{where}: release.deploy is not set")
        self.environment = release.get("production_environment") or "production"
        return deploy

    def _manifest_path(self, deploy: DeployConfig) -> Path:
        # Next to the env files, so relative paths in the manifest resolve on the host.
        return self.target.workdir / Path(deploy.manifest).name

    def _apply(self, state: State, deploy: DeployConfig, text: str, decision: Decision) -> None:
        log.info("%s: applying release %s (%s)", self.repo, decision.release[:12],
                 decision.reason)
        state.outcome = Outcome.APPLYING
        state.save(self.state_path)
        try:
            state.deployment = self.gh.create_deployment(
                self.repo, decision.release, self.environment, decision.reason)
        except GitHubError as exc:
            # Nothing applied yet; the manifest waits for `retry` like any failure.
            self._finish(state, Outcome.FAILED, f"could not record the deployment: {exc}")
            raise
        state.save(self.state_path)
        try:
            self.gh.deployment_status(self.repo, state.deployment, "in_progress",
                                      f"{deploy.runtime}: applying", deploy.url)
        except GitHubError as exc:
            log.warning("%s: could not report in_progress: %s", self.repo, exc)
        manifest = self._manifest_path(deploy)
        incoming = manifest.with_name(f".incoming-{manifest.name}")
        incoming.write_text(text, encoding="utf-8")
        try:
            self.runtime.apply(incoming, deploy.stack, self.target.registry)
            problems = self.runtime.problems(deploy.stack, decision.images, images_only=False)
        except RuntimeFailure as exc:
            problems = [str(exc)]
        if problems:
            log.error("%s: release %s failed:\n  %s", self.repo, decision.release[:12],
                      "\n  ".join(problems))
            self._finish(state, Outcome.FAILED, "; ".join(problems), deploy.url)
            return
        os.replace(incoming, manifest)
        releases = self.dir / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        (releases / f"{decision.release}.yml").write_text(text, encoding="utf-8")
        state.applied, state.stack, state.manifest = decision.release, deploy.stack, str(manifest)
        self._finish(state, Outcome.APPLIED, decision.reason, deploy.url)
        log.info("%s: applied release %s", self.repo, decision.release[:12])
        self._prune(decision.images)

    def _finish(self, state: State, outcome: Outcome, detail: str,
                url: str | None = None) -> None:
        state.outcome = outcome
        state.detail = detail
        if state.deployment and outcome in (Outcome.APPLIED, Outcome.FAILED):
            try:
                self.gh.deployment_status(
                    self.repo, state.deployment,
                    "success" if outcome is Outcome.APPLIED else "failure", detail, url)
            except GitHubError as exc:
                log.warning("%s: could not report deployment %s: %s", self.repo,
                            state.deployment, exc)
            state.deployment = 0
        state.save(self.state_path)
        if outcome is not Outcome.APPLYING:
            with (self.dir / "history").open("a", encoding="utf-8") as f:
                f.write(f"{self.now().isoformat(timespec='seconds')} {outcome.value} "
                        f"{state.release or '-'} {detail}\n")

    def _repair(self, state: State) -> None:
        """Put back services that drifted from the applied manifest (e.g. a manual update)."""
        manifest = Path(state.manifest)
        if state.outcome != Outcome.APPLIED or not state.stack or not manifest.is_file():
            return
        _, images = manifest_images(manifest.read_text(encoding="utf-8"))
        drift = self.runtime.problems(state.stack, images, images_only=True)
        if not drift:
            return
        log.warning("%s: services drifted from release %s, applying it again:\n  %s",
                    self.repo, state.applied[:12], "\n  ".join(drift))
        try:
            self.runtime.apply(manifest, state.stack, self.target.registry)
            problems = self.runtime.problems(state.stack, images, images_only=False)
        except RuntimeFailure as exc:
            problems = [str(exc)]
        if problems:
            log.error("%s: repair failed:\n  %s", self.repo, "\n  ".join(problems))
            self._finish(state, Outcome.FAILED, "repair failed: " + "; ".join(problems))

    def _prune(self, images: dict[str, str]) -> None:
        releases = sorted((self.dir / "releases").glob("*.yml"), key=lambda p: p.stat().st_mtime)
        for old in releases[:-self.keep_releases]:
            old.unlink()
        keep = set(images.values())
        for kept in releases[-self.keep_releases:]:
            try:
                keep |= set(manifest_images(kept.read_text(encoding="utf-8"))[1].values())
            except ValueError:
                continue
        prefixes = {image.rsplit(":", 1)[0] for image in keep}
        self.runtime.prune(keep, prefixes)


# -- loop and commands ----------------------------------------------------------------------

def run(config: DeployerConfig, runtime: Runtime, *, once: bool = False,
        api: Callable[[str], GitHubAPI] = GitHubAPI,
        sleep: Callable[[float], None] = time.sleep,
        stopping: Callable[[], bool] = lambda: False) -> int:
    """Reconcile every target, then wait; forever unless `once`. Returns 1 if a pass failed."""
    failed = False
    while True:
        failed = False
        for target in config.targets:
            try:
                token = target.token_file.read_text(encoding="utf-8").strip()
                TargetRun(target, api(token), runtime,
                          keep_releases=config.keep_releases).reconcile()
            except (ConfigError, GitHubError, RuntimeFailure, OSError, ValueError) as exc:
                failed = True
                log.error("%s: %s", target.repository, exc)  # noqa: TRY400 - expected, no trace
            if stopping():
                return 1 if failed else 0
        if once:
            return 1 if failed else 0
        for _ in range(config.interval_seconds):
            if stopping():
                return 1 if failed else 0
            sleep(1)


def retry(config: DeployerConfig, repository: str) -> str:
    """Forget the last manifest of a target, so the next pass decides and applies it again."""
    target = next((t for t in config.targets if t.repository == repository), None)
    if target is None:
        raise ConfigError(f"no target {repository}")
    path = target.workdir / STATE_DIR / "state.json"
    state = State.load(path)
    if state.outcome == Outcome.APPLYING:
        raise ConfigError(f"{repository}: a pass is applying right now")
    previous = f"{state.outcome or 'nothing'} {state.blob[:12]}".strip()
    state.head = state.blob = state.outcome = state.release = ""
    state.unlinked = 0
    state.save(path)
    return previous


def status(config: DeployerConfig) -> list[str]:
    lines = []
    for target in config.targets:
        state = State.load(target.workdir / STATE_DIR / "state.json")
        lines.append(f"{target.repository}: release {state.applied[:12] or 'none'} applied; "
                     f"last manifest {state.blob[:12] or '-'}: {state.outcome or 'not seen'}"
                     f"{f' ({state.detail})' if state.detail else ''}")
    return lines
