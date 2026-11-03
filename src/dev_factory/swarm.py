"""Docker Swarm runtime of the deployer: `docker stack deploy` and what runs afterwards.

The stack's own update_config does the rollout: health-gated, start-first or stop-first per
service, rolled back on failure. `docker stack deploy` leaves services whose spec did not
change alone, so pinned image tags restart only what changed.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from dev_factory.deployer import RegistryLogin, RuntimeFailure

Run = Callable[..., subprocess.CompletedProcess]


class Swarm:
    def __init__(self, docker: str = "docker", run: Run = subprocess.run,
                 timeout_seconds: int = 1800):
        self.docker = docker
        self.run = run
        self.timeout = timeout_seconds

    def call(self, *args: str, env: dict[str, str] | None = None, stdin: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
        try:
            result = self.run([self.docker, *args], input=stdin, capture_output=True,
                              text=True, timeout=self.timeout, env=env, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeFailure(f"docker {' '.join(args[:2])}: {exc}") from exc
        if check and result.returncode != 0:
            raise RuntimeFailure(f"docker {args[0]} {args[1] if len(args) > 1 else ''}: "
                                 f"{(result.stderr or result.stdout).strip()[-2000:]}")
        return result

    def ensure(self) -> str:
        """Make this host a single-node swarm if it is none yet; returns its address.

        A new swarm listens on loopback only, so its management and gossip ports are never
        reachable from outside. The VXLAN port 4789/udp still needs the firewall.
        """
        state, _, addr = self.call("info", "--format",
                                   "{{.Swarm.LocalNodeState}} {{.Swarm.NodeAddr}}"
                                   ).stdout.strip().partition(" ")
        if state == "active":
            return addr
        if state != "inactive":
            raise RuntimeFailure(f"swarm is {state}; fix it with docker swarm first")
        self.call("swarm", "init", "--advertise-addr", "127.0.0.1",
                  "--listen-addr", "127.0.0.1:2377")
        return "127.0.0.1"

    def _login(self, registry: RegistryLogin, env: dict[str, str]) -> None:
        password = registry.password_file.read_text(encoding="utf-8").strip()
        self.call("login", registry.server, "-u", registry.username, "--password-stdin",
                  env=env, stdin=password)

    def check_login(self, registry: RegistryLogin) -> None:
        with tempfile.TemporaryDirectory(prefix="deployer-docker-") as config:
            self._login(registry, {**os.environ, "DOCKER_CONFIG": config})

    def apply(self, manifest: Path, stack: str, registry: RegistryLogin | None) -> str:
        # Registry credentials live in a throwaway Docker config for this apply only;
        # --with-registry-auth hands them to the swarm for the pulls.
        with tempfile.TemporaryDirectory(prefix="deployer-docker-") as config:
            env = {**os.environ, "DOCKER_CONFIG": config}
            if registry is not None:
                self._login(registry, env)
            # Relative paths in the manifest (env files) resolve against its directory.
            result = self.call("stack", "deploy", "--prune", "--with-registry-auth",
                               "--detach=false", "-c", str(manifest), stack, env=env)
        return result.stdout

    def problems(self, stack: str, images: dict[str, str], *, images_only: bool) -> list[str]:
        names = {f"{stack}_{service}": image for service, image in images.items()}
        listed = self.call("stack", "services", stack, "--format", "{{json .}}",
                           check=False)
        replicas = {}
        for line in listed.stdout.splitlines():
            item = json.loads(line)
            replicas[item["Name"]] = item.get("Replicas", "")
        missing = [name for name in names if name not in replicas]
        present = [name for name in names if name in replicas]
        specs = {}
        if present:
            for item in json.loads(self.call("service", "inspect", *present).stdout):
                specs[item["Spec"]["Name"]] = item
        result = [f"{name} missing" for name in missing]
        for name in present:
            spec = specs.get(name, {})
            running = spec.get("Spec", {}).get("TaskTemplate", {}).get(
                "ContainerSpec", {}).get("Image", "").split("@", 1)[0]
            state = (spec.get("UpdateStatus") or {}).get("State", "")
            if running != names[name]:
                result.append(f"{name} runs {running}, wants {names[name]}"
                              f"{f' ({state})' if state else ''}")
            elif images_only:
                continue
            elif state.startswith("rollback") or state == "paused":
                result.append(f"{name} update {state}")
            elif not _converged(replicas[name]):
                result.append(f"{name} replicas {replicas[name]}")
        return result

    def prune(self, keep: set[str], prefixes: set[str]) -> None:
        listed = self.call("image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
                           check=False)
        for ref in listed.stdout.split():
            if ref.rsplit(":", 1)[0] in prefixes and ref not in keep:
                self.call("image", "rm", ref, check=False)


def _converged(replicas: str) -> bool:
    """`docker stack services` replicas, e.g. "1/1" or "1/1 (max 1 per node)"."""
    running, _, desired = replicas.split(" ", 1)[0].partition("/")
    return bool(desired) and running == desired
