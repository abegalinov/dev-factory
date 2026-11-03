"""`dev-factory deployer install`: set up or upgrade the deployer on a production host.

Runs from the deployer image with the Docker socket and the host's configuration directory
mounted at the same path (docs/deployer.md). Every step is idempotent, so the same command
installs, repairs and upgrades:

1. make the host a single-node swarm, listening on loopback only;
2. add the target to deployer.yaml unless it is configured, asking for missing tokens;
3. check each target's tokens: the repository's release.deploy is readable, the registry
   accepts the login;
4. (re)start the deployer container from the image this command runs in.

A target that is already configured is left as it is; edit deployer.yaml to change it.
"""

import getpass
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from dev_factory.config import ConfigError
from dev_factory.deployer import (
    GitHubAPI,
    GitHubError,
    RuntimeFailure,
    Target,
    TargetRun,
    load_config,
)
from dev_factory.swarm import Swarm

CONTAINER = "dev-factory-deployer"
IMAGE_ENV = "DEPLOYER_IMAGE"  # set by deploy/deployer/Dockerfile


def install(config_path: Path, repository: str | None, approvers: list[str], *,
            workdir: str | None = None, image_prefixes: list[str] | None = None,
            registry: str = "ghcr.io", registry_user: str | None = None,
            image: str | None = None, swarm: Swarm | None = None,
            api: Callable[[str], GitHubAPI] = GitHubAPI,
            ask: Callable[[str], str] = getpass.getpass,
            say: Callable[[str], None] = print) -> None:
    swarm = swarm or Swarm()
    image = image or os.environ.get(IMAGE_ENV, "")
    if not image:
        raise ConfigError(f"no image to run: pass --image or set ${IMAGE_ENV}")

    addr = swarm.ensure()
    say(f"swarm: active on {addr}")
    if addr not in ("127.0.0.1", ""):
        say(f"  it listens on {addr}: keep 2377/tcp and 7946 closed in the firewall")
    say("  keep 4789/udp closed in the firewall (Docker binds it on every interface)")

    raw = _read(config_path)
    targets = raw.setdefault("targets", [])
    if repository and not any(t.get("repository") == repository for t in targets):
        targets.append(_new_target(config_path.parent, repository, approvers, workdir,
                                   image_prefixes, registry, registry_user, ask))
        _write(config_path, raw)
        say(f"{config_path}: added {repository}")
    elif not targets:
        raise ConfigError(f"{config_path}: no targets; name a repository to add")

    config = load_config(config_path)
    for target in config.targets:
        _secret(target.token_file, f"{target.repository}: GitHub token", ask)
        if target.registry:
            _secret(target.registry.password_file,
                    f"{target.registry.server}: token for {target.registry.username}", ask)
        _check(target, swarm, api)
        say(f"{target.repository}: token and registry login work")

    if swarm.call("container", "inspect", CONTAINER, check=False).returncode == 0:
        say(f"{CONTAINER}: stopping (a pass that is applying finishes first)")
        swarm.call("stop", CONTAINER)
        swarm.call("rm", CONTAINER)
    mounts = ["-v", "/var/run/docker.sock:/var/run/docker.sock",
              "-v", f"{config_path.parent}:{config_path.parent}:ro"]
    for target in config.targets:
        mounts += ["-v", f"{target.workdir}:{target.workdir}"]
    swarm.call("run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
               "--stop-timeout", "1800", *mounts, image,
               "--config", str(config_path), "run")
    say(f"{CONTAINER}: running {image}; follow it with docker logs -f {CONTAINER}")


def _new_target(conf_dir: Path, repository: str, approvers: list[str], workdir: str | None,
                image_prefixes: list[str] | None, registry: str, registry_user: str | None,
                ask: Callable[[str], str]) -> dict[str, Any]:
    if repository.count("/") != 1:
        raise ConfigError(f"repository must be owner/name, got {repository!r}")
    if not approvers:
        raise ConfigError("name at least one --approver: who may approve production")
    owner, name = repository.lower().split("/")
    target: dict[str, Any] = {
        "repository": repository,
        "workdir": workdir or f"/srv/{name}",
        "approvers": approvers,
        "token_file": str(conf_dir / f"{name}.token"),
        "image_prefixes": image_prefixes or [f"{registry}/{owner}/{name}/"],
    }
    _secret(Path(target["token_file"]), (
        f"{repository}: fine-grained GitHub token for this repository only, with Contents "
        "read, Pull requests read and Deployments read and write"), ask)
    password_file = conf_dir / f"{name}.registry.token"
    user = registry_user or approvers[0]
    if _secret(password_file, (
            f"{registry}: token for {user} to pull private images (for ghcr.io a classic "
            "token with only read:packages); empty if the images are public"),
            ask, optional=True):
        target["registry"] = {"server": registry, "username": user,
                              "password_file": str(password_file)}
    return target


def _secret(path: Path, question: str, ask: Callable[[str], str],
            optional: bool = False) -> bool:
    """Ask for a secret unless its file has one; False if an optional one was skipped."""
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return True
    value = ask(f"{question}\n> ").strip()
    if not value:
        if optional:
            return False
        raise ConfigError(f"{path}: no token given")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return True


def _check(target: Target, swarm: Swarm, api: Callable[[str], GitHubAPI]) -> None:
    again = "and run install again to be asked for a new one"
    gh = api(target.token_file.read_text(encoding="utf-8").strip())
    try:
        branch = target.branch or gh.default_branch(target.repository)
        head = gh.head(target.repository, branch)
    except GitHubError as exc:
        raise ConfigError(f"{target.repository}: {exc}: check the token's repository and "
                          f"permissions, or delete {target.token_file} {again}") from exc
    TargetRun(target, gh, swarm).deploy_config(head)
    if target.registry:
        try:
            swarm.check_login(target.registry)
        except RuntimeFailure as exc:
            raise ConfigError(f"{exc}: delete {target.registry.password_file} {again}"
                              ) from exc


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return raw


def _write(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        yaml.safe_dump(raw, handle, sort_keys=False)
    os.replace(temporary, path)
