"""Typed, strict configuration. Unknown keys are errors so typos cannot silently disable policy."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dev_factory.state_machine import Stage

CONFIG_DIR_ENV = "DEV_FACTORY_CONFIG"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    observe_only: bool = True
    max_agent_runs_per_poll: int = 1
    runtime_root: Path = Path(".runtime")
    log_level: str = "INFO"
    # Heavy actions (agent, verification) are deferred while the host is short on resources.
    min_available_memory_mb: int = 500
    max_load_per_cpu: float = 1.5


@dataclass(frozen=True)
class GitHubConfig:
    cli: str = "gh"
    timeout_seconds: int = 60
    max_retries: int = 3


@dataclass(frozen=True)
class ClaudeConfig:
    # Executable on the host (unisolated mode) or inside the agent image.
    cli: str = "claude"
    timeout_seconds: int = 1800
    max_turns: int = 150
    max_budget_usd: float = 5.0
    model: str | None = None
    # Agent runs in mutation mode are refused without a container image unless this is set.
    allow_unisolated: bool = False
    # The only variables forwarded to Claude (by name; values never appear in argv).
    env_passthrough: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    # Optional KEY=VALUE file (mode 600) supplying env_passthrough values, e.g. the agent token.
    env_file: Path | None = None


@dataclass(frozen=True)
class DockerConfig:
    """Limits for every container the factory starts (agents and verification).

    Defaults suit a small shared host: production must win any contention.
    """

    cli: str = "docker"
    network: str = "bridge"
    # Hard memory cap per container; swap is disabled for containers (memory-swap = memory).
    memory: str = "768m"
    cpus: str = "1"
    # Relative CPU weight under contention (Docker default 1024).
    cpu_shares: int = 256
    pids_limit: int = 512
    # /tmp is RAM-backed; HOME (caches) is a per-run directory on disk.
    tmp_size: str = "128m"
    # systemd slice that caps all factory containers together (deploy/systemd/dev-factory.slice).
    cgroup_parent: str | None = None


@dataclass(frozen=True)
class PolicyConfig:
    max_spec_attempts: int = 2
    max_implementation_attempts: int = 3
    checks_timeout_minutes: int = 60
    # Automated reviews per spec or code PR before its first human review. Blocking
    # findings of each go back to the authoring agent once; the last revision goes to the
    # human gate without another review (the review is advisory).
    max_review_rounds: int = 1
    # Triage rounds (agent.triage) before the spec; afterwards the spec agent proceeds
    # with the triage's stated assumptions.
    max_triage_rounds: int = 3


@dataclass(frozen=True)
class FactoryConfig:
    home: Path
    runtime: RuntimeConfig
    github: GitHubConfig
    claude: ClaudeConfig
    policy: PolicyConfig
    docker: DockerConfig = field(default_factory=DockerConfig)

    @property
    def runtime_root(self) -> Path:
        root = self.runtime.runtime_root
        return root if root.is_absolute() else self.home / root


@dataclass(frozen=True)
class AgentConfig:
    spec_prompt: Path
    implementation_prompt: Path
    spec_allowed_tools: tuple[str, ...] = ("Read", "Glob", "Grep", "Write", "Edit")
    implementation_allowed_tools: tuple[str, ...] = (
        "Read", "Glob", "Grep", "Write", "Edit", "Bash",
    )
    # Paths the implementation agent must not change (CI config can exfiltrate secrets).
    protected_paths: tuple[str, ...] = (".github/**",)
    # Docker image with Claude Code and the project's toolchain; the agent runs inside it.
    image: str | None = None
    # Project-specific guidance appended to every agent prompt (trusted: written by operators).
    instructions: str = ""
    # Automated review of spec and code PRs by a separate, read-only agent run before the
    # human gates.
    review: bool = False
    review_prompt: Path | None = None
    review_allowed_tools: tuple[str, ...] = ("Read", "Glob", "Grep")
    # Read-only discussion of the issue in its comments before the spec (agent.triage).
    triage: bool = False
    triage_prompt: Path | None = None
    # A task the triage sizes `small` skips the spec PR: the implementation agent writes
    # the spec into the code PR, and merging that PR approves both.
    fast_track: bool = False


@dataclass(frozen=True)
class VerificationConfig:
    # "github-checks": read checks reported by the repository's own CI.
    # "container": the factory runs `commands` in an isolated container on the PR head and
    # reports the result as the commit status VERIFY_CONTEXT (plus any required_checks).
    mode: str = "github-checks"
    required_checks: tuple[str, ...] = ()
    image: str | None = None
    commands: tuple[str, ...] = ()
    timeout_seconds: int = 1800
    # Name of a GitHub Actions artifact with demo videos (.mp4, .gif) recorded by CI on the
    # PR head. Once checks pass, the factory posts them on the code PR and the issue.
    demo_artifact: str | None = None


@dataclass(frozen=True)
class DeployConfig:
    """How the deployer (deployer.py) applies the repository's production manifest.

    The manifest pins every image; the deployer applies it on the production host once a
    PR merged by one of the host's approvers changed it.
    """

    # "swarm": `docker stack deploy` of the manifest as stack `stack`.
    runtime: str = "swarm"
    # Path of the manifest in the repository, e.g. deploy/production.yml.
    manifest: str = ""
    stack: str = ""
    # Recorded on each GitHub deployment.
    url: str | None = None


@dataclass(frozen=True)
class ReleaseConfig:
    # "none": the merge commit is the release candidate; humans deploy and close the issue.
    # "github-deployments": a workflow builds the candidate; a production deployment closes it.
    adapter: str = "none"
    candidate_workflow: str | None = None
    candidate_ref_template: str = "{sha}"
    production_environment: str = "production"
    # Set when the deployer applies this repository's manifest on a production host.
    deploy: DeployConfig | None = None


@dataclass(frozen=True)
class Component:
    """A part of a monorepo: where it lives and what an agent should know about it."""

    name: str
    paths: tuple[str, ...]
    instructions: str = ""


@dataclass(frozen=True)
class ProjectConfig:
    """Everything the factory knows about one project: the host entry merged with the
    repository's .dev-factory/ from the default branch (see repo_config.py)."""

    project_id: str
    repository: str
    default_branch: str
    enabled: bool
    labels: dict[Stage, str]
    spec_directory: str = "specs/features"
    agent: AgentConfig | None = None
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    release: ReleaseConfig = field(default_factory=ReleaseConfig)
    # Default-branch commit the repository config was read at.
    config_sha: str | None = None
    components: tuple[Component, ...] = ()
    spec_template: str = ""
    review_checklist: str = ""
    pr_body_template: str = ""
    # Repository limits (never above the host's); None: the host policy.
    policy: PolicyConfig | None = None

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]


def resolve_config_dir(explicit: str | None = None) -> Path:
    """Pick the config directory: explicit flag, then env var, then ./config."""
    value = explicit or os.environ.get(CONFIG_DIR_ENV) or "config"
    path = Path(value).resolve()
    if not (path / "factory.yaml").is_file():
        raise ConfigError(f"{path} does not contain factory.yaml")
    return path


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return value


def _section(raw: dict[str, Any], key: str, allowed: set[str], where: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: '{key}' must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys in '{key}': {sorted(unknown)}")
    return value


def _check_top_level(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown top-level keys: {sorted(unknown)}")


def load_factory(config_dir: Path) -> FactoryConfig:
    path = config_dir / "factory.yaml"
    raw = load_yaml(path)
    where = str(path)
    _check_top_level(raw, {"version", "runtime", "github", "claude", "policy", "docker"}, where)
    if raw.get("version") != 1:
        raise ConfigError(f"{where}: unsupported version {raw.get('version')!r}")
    runtime = _section(raw, "runtime", set(RuntimeConfig.__annotations__), where)
    github = _section(raw, "github", set(GitHubConfig.__annotations__), where)
    claude = _section(raw, "claude", set(ClaudeConfig.__annotations__), where)
    policy = _section(raw, "policy", set(PolicyConfig.__annotations__), where)
    docker = _section(raw, "docker", set(DockerConfig.__annotations__), where)
    if "runtime_root" in runtime:
        runtime["runtime_root"] = Path(runtime["runtime_root"])
    if "env_passthrough" in claude:
        claude["env_passthrough"] = tuple(str(v) for v in claude["env_passthrough"] or ())
    if claude.get("env_file"):
        claude["env_file"] = Path(str(claude["env_file"])).expanduser()
    if not isinstance(runtime.get("observe_only", True), bool):
        raise ConfigError(f"{where}: runtime.observe_only must be a boolean")
    return FactoryConfig(
        home=config_dir.parent,
        runtime=RuntimeConfig(**runtime),
        github=GitHubConfig(**github),
        claude=ClaudeConfig(**claude),
        policy=PolicyConfig(**policy),
        docker=DockerConfig(**docker),
    )


# Keys of the host entry. Project knowledge lives in the repository (.dev-factory/).
_ENTRY_KEYS = {
    "project_id", "repository", "enabled", "default_branch", "agent_image",
    "verification_image",
}
_MOVED_KEYS = {"workflow", "specification", "agent", "verification", "release"}


@dataclass(frozen=True)
class ProjectEntry:
    """Host-side registration of one repository (trusted, operator-owned)."""

    project_id: str
    repository: str
    enabled: bool
    # The agent container receives the Claude token, so its image is the operator's choice.
    agent_image: str | None = None
    verification_image: str | None = None
    default_branch: str | None = None  # None: the repository's default branch


def load_entry(path: Path) -> ProjectEntry:
    raw = load_yaml(path)
    where = str(path)
    moved = _MOVED_KEYS & set(raw)
    if moved:
        raise ConfigError(f"{where}: {sorted(moved)} moved to the repository's "
                          ".dev-factory/config.yaml (see docs/repo-config.md)")
    _check_top_level(raw, _ENTRY_KEYS, where)
    repository = str(raw.get("repository", ""))
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: enabled must be a boolean")
    placeholder = repository.count("/") != 1 or repository.startswith("YOUR_")
    if placeholder and enabled:
        raise ConfigError(f"{where}: enabled project needs owner/repo")
    return ProjectEntry(
        project_id=str(raw.get("project_id", path.stem)),
        repository=repository,
        enabled=enabled,
        agent_image=str(raw["agent_image"]) if raw.get("agent_image") else None,
        verification_image=(str(raw["verification_image"])
                            if raw.get("verification_image") else None),
        default_branch=str(raw["default_branch"]) if raw.get("default_branch") else None,
    )


def load_entries(directory: Path) -> list[ProjectEntry]:
    entries = [load_entry(path) for path in sorted(directory.glob("*.yaml"))]
    ids = [e.project_id for e in entries]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"{directory}: duplicate project_id values")
    return entries
