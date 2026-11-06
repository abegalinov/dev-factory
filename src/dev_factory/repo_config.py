"""Project configuration from the repository's .dev-factory/ directory (docs/repo-config.md).

The files are read from the default branch at one commit per poll, never from a PR branch or
an agent worktree, so a change only takes effect once a human has merged it. They are merged
with the host entry; the repository can tighten the host's policy but never loosen it.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

import yaml

from dev_factory.config import (
    AgentConfig,
    Component,
    ConfigError,
    DeployConfig,
    FactoryConfig,
    PolicyConfig,
    ProjectConfig,
    ProjectEntry,
    ReleaseConfig,
    VerificationConfig,
    _check_top_level,
    _section,
)
from dev_factory.state_machine import label_map_from_config

CONFIG_DIR = ".dev-factory"
CONFIG_FILE = "config.yaml"
INSTRUCTIONS_FILE = "instructions.md"
SPEC_TEMPLATE = "templates/spec.md"
REVIEW_CHECKLIST = "templates/review.md"
PR_BODY_TEMPLATE = "templates/pr-body.md"
FILES = (CONFIG_FILE, INSTRUCTIONS_FILE, SPEC_TEMPLATE, REVIEW_CHECKLIST, PR_BODY_TEMPLATE)

DEPLOY_RUNTIMES = ("swarm",)

# Always protected from agents, whatever the repository configures. CI definitions can
# expose secrets, and the factory's own configuration must change only through human PRs.
BASELINE_PROTECTED = (".github/**", f"{CONFIG_DIR}/**")
_MAX_TEXT = 20_000  # per file: these texts go into every prompt

_REPO_KEYS = {"version", "specification", "labels", "agent", "components", "verification",
              "release", "policy"}
_REPO_AGENT_KEYS = {"review", "triage", "fast_track", "protected_paths"}
_REPO_VERIFICATION_KEYS = {"mode", "required_checks", "commands", "timeout_seconds",
                           "demo_artifact"}
_REPO_POLICY_KEYS = {f.name for f in fields(PolicyConfig)}


def build_project(
    entry: ProjectEntry, files: Mapping[str, str | None], factory: FactoryConfig, *,
    default_branch: str, config_sha: str | None,
) -> ProjectConfig:
    """Merge the host entry with the repository files (paths relative to .dev-factory/)."""
    where = f"{entry.repository}:{CONFIG_DIR}/{CONFIG_FILE}"
    text = files.get(CONFIG_FILE)
    if text is None:
        raise ConfigError(f"{where} not found on {default_branch}; add it (see "
                          "examples/dev-factory/) or disable the project")
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{where}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must contain a YAML mapping")
    _check_top_level(raw, _REPO_KEYS, where)
    if raw.get("version") != 1:
        raise ConfigError(f"{where}: unsupported version {raw.get('version')!r}")

    try:
        labels = label_map_from_config(
            {str(k): str(v) for k, v in (raw.get("labels") or {}).items()})
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"{where}: labels: {exc}") from exc
    spec = _section(raw, "specification", {"directory"}, where)
    components = _components(raw.get("components"), where)
    agent = _agent(entry, raw, files, components, factory, where)
    verification = _verification(entry, raw, where)
    release = _release(raw, where)
    policy = _policy(raw, factory.policy, where)

    return ProjectConfig(
        project_id=entry.project_id,
        repository=entry.repository,
        default_branch=default_branch,
        enabled=entry.enabled,
        labels=labels,
        spec_directory=str(spec.get("directory", "specs/features")).strip("/"),
        agent=agent,
        verification=verification,
        release=release,
        config_sha=config_sha,
        components=components,
        spec_template=_text(files, SPEC_TEMPLATE, where),
        review_checklist=_text(files, REVIEW_CHECKLIST, where),
        pr_body_template=_text(files, PR_BODY_TEMPLATE, where),
        policy=policy,
    )


def _text(files: Mapping[str, str | None], name: str, where: str) -> str:
    text = (files.get(name) or "").strip()
    if len(text) > _MAX_TEXT:
        raise ConfigError(f"{where}: {CONFIG_DIR}/{name} exceeds {_MAX_TEXT} characters")
    return text


def _strings(value: Any, key: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: {key} must be a list of strings")
    return tuple(value)


def _components(value: Any, where: str) -> tuple[Component, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where}: components must be a list")
    result = []
    for item in value:
        if not isinstance(item, dict) or not item.get("name"):
            raise ConfigError(f"{where}: each component needs a name")
        unknown = set(item) - {"name", "paths", "instructions"}
        if unknown:
            raise ConfigError(f"{where}: unknown keys in component {item['name']!r}: "
                              f"{sorted(unknown)}")
        paths = _strings(item.get("paths"), f"components.{item['name']}.paths", where)
        if not paths:
            raise ConfigError(f"{where}: component {item['name']!r} needs paths")
        result.append(Component(str(item["name"]), paths,
                                str(item.get("instructions") or "").strip()))
    return tuple(result)


def _instructions(notes: str, components: tuple[Component, ...]) -> str:
    parts = [notes] if notes else []
    if components:
        lines = ["Components of this repository:"]
        for c in components:
            lines.append(f"- {c.name} ({', '.join(c.paths)})")
            lines += [f"  {line}" for line in c.instructions.splitlines()]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _agent(
    entry: ProjectEntry, raw: dict[str, Any], files: Mapping[str, str | None],
    components: tuple[Component, ...], factory: FactoryConfig, where: str,
) -> AgentConfig:
    agent = _section(raw, "agent", _REPO_AGENT_KEYS, where)
    review = agent.get("review", False)
    triage = agent.get("triage", False)
    fast_track = agent.get("fast_track", False)
    for key, value in (("review", review), ("triage", triage), ("fast_track", fast_track)):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: agent.{key} must be a boolean")
    if fast_track and not triage:
        raise ConfigError(f"{where}: agent.fast_track needs agent.triage (the triage sizes "
                          "the task)")
    prompts = factory.home / "prompts"
    for name in ("spec.md", "implement.md", *(["review.md"] if review else []),
                 *(["triage.md"] if triage else [])):
        if not (prompts / name).is_file():
            raise ConfigError(f"factory prompt not found: {prompts / name}")
    extra = _strings(agent.get("protected_paths"), "agent.protected_paths", where)
    return AgentConfig(
        spec_prompt=prompts / "spec.md",
        implementation_prompt=prompts / "implement.md",
        protected_paths=tuple(dict.fromkeys((*BASELINE_PROTECTED, *extra))),
        image=entry.agent_image,
        instructions=_instructions(_text(files, INSTRUCTIONS_FILE, where), components),
        review=review,
        review_prompt=prompts / "review.md" if review else None,
        triage=triage,
        triage_prompt=prompts / "triage.md" if triage else None,
        fast_track=fast_track,
    )


def _verification(entry: ProjectEntry, raw: dict[str, Any], where: str) -> VerificationConfig:
    v = _section(raw, "verification", _REPO_VERIFICATION_KEYS, where)
    config = VerificationConfig(
        mode=str(v.get("mode", "github-checks")),
        required_checks=_strings(v.get("required_checks"), "verification.required_checks",
                                 where),
        image=entry.verification_image,
        commands=_strings(v.get("commands"), "verification.commands", where),
        timeout_seconds=int(v.get("timeout_seconds", VerificationConfig.timeout_seconds)),
        demo_artifact=v.get("demo_artifact"),
    )
    if config.demo_artifact is not None and not (
            isinstance(config.demo_artifact, str) and _ARTIFACT.fullmatch(config.demo_artifact)):
        raise ConfigError(f"{where}: verification.demo_artifact must be an artifact name "
                          "(letters, digits, . _ -)")
    if config.mode not in ("github-checks", "container"):
        raise ConfigError(f"{where}: unknown verification mode {config.mode!r}")
    if config.mode == "container":
        if not config.commands:
            raise ConfigError(f"{where}: container verification needs commands")
        if not config.image:
            raise ConfigError(f"{where}: container verification needs verification_image in "
                              f"the host entry for {entry.project_id}")
    return config


def _release(raw: dict[str, Any], where: str) -> ReleaseConfig:
    values = _section(raw, "release", {f.name for f in fields(ReleaseConfig)}, where)
    deploy = parse_deploy(values.pop("deploy", None), where)
    release = ReleaseConfig(**values, deploy=deploy)
    if release.adapter not in ("none", "github-deployments"):
        raise ConfigError(f"{where}: unknown release adapter {release.adapter!r}")
    if release.adapter == "github-deployments" and not release.candidate_workflow:
        raise ConfigError(f"{where}: github-deployments requires release.candidate_workflow")
    return release


_STACK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def parse_deploy(value: Any, where: str) -> DeployConfig | None:
    """release.deploy; shared with the deployer, which reads it from the default branch."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: release.deploy must be a mapping")
    unknown = set(value) - {f.name for f in fields(DeployConfig)}
    if unknown:
        raise ConfigError(f"{where}: unknown keys in release.deploy: {sorted(unknown)}")
    deploy = DeployConfig(**value)
    if deploy.runtime not in DEPLOY_RUNTIMES:
        raise ConfigError(f"{where}: unknown release.deploy.runtime {deploy.runtime!r}")
    manifest = str(deploy.manifest or "")
    if not manifest or manifest.startswith("/") or ".." in manifest.split("/"):
        raise ConfigError(f"{where}: release.deploy.manifest must be a path in the repository")
    if not _STACK_NAME.fullmatch(str(deploy.stack or "")):
        raise ConfigError(f"{where}: release.deploy.stack must be a stack name "
                          "(lowercase letters, digits, - and _)")
    return deploy


def _policy(raw: dict[str, Any], host: PolicyConfig, where: str) -> PolicyConfig | None:
    values = _section(raw, "policy", _REPO_POLICY_KEYS, where)
    if not values:
        return None
    for key, value in values.items():
        limit = getattr(host, key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{where}: policy.{key} must be a non-negative integer")
        if value > limit:
            raise ConfigError(f"{where}: policy.{key}={value} exceeds the host limit {limit}; "
                              "a repository can only tighten limits")
    return PolicyConfig(**{**{f.name: getattr(host, f.name) for f in fields(host)}, **values})


class RepoConfigLoader:
    """Reads .dev-factory/ from a repository's default branch at its current head."""

    def __init__(self, gh, factory: FactoryConfig):
        self.gh = gh
        self.factory = factory

    def __call__(self, entry: ProjectEntry) -> ProjectConfig:
        repo = entry.repository
        branch = entry.default_branch or self.gh.default_branch(repo)
        head = self.gh.branch(repo, branch)
        if head is None:
            raise ConfigError(f"{repo}: default branch {branch} not found")
        sha = head["commit"]["sha"]
        files = {name: self.gh.file_text(repo, f"{CONFIG_DIR}/{name}", sha) for name in FILES}
        return build_project(entry, files, self.factory, default_branch=branch, config_sha=sha)


def load_local(directory: Path, entry: ProjectEntry, factory: FactoryConfig) -> ProjectConfig:
    """Validate a checked-out .dev-factory/ directory (`dev-factory config check`)."""
    def read(name: str) -> str | None:
        path = directory / name
        return path.read_text(encoding="utf-8") if path.is_file() else None
    return build_project(entry, {name: read(name) for name in FILES}, factory,
                         default_branch=entry.default_branch or "main", config_sha=None)


ProjectLoader = Callable[[ProjectEntry], ProjectConfig]
