"""Runs commands in locked-down, disposable Docker containers.

Every container: removed on exit, no Linux capabilities, no privilege escalation, read-only
root filesystem, small tmpfs /tmp, the host user's uid/gid (so files written to mounts stay
owned by the orchestrator), and only two bind mounts: the task directory at /workspace and a
disposable per-run HOME on disk (package caches must not consume RAM).

Resource policy for shared hosts: hard memory cap with swap disabled, an OOM score that makes
the kernel kill factory containers before anything else, low CPU weight, a pid limit, and an
optional systemd slice capping all factory containers together. Environment variables are
forwarded by name only.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dev_factory.config import DockerConfig

log = logging.getLogger(__name__)

WORKDIR = "/workspace"
HOME = "/home/agent"


class ContainerTimeout(RuntimeError):
    pass


# Exit status of a process killed by SIGKILL, which is what the OOM killer sends.
OOM_EXIT = 137


@dataclass(frozen=True)
class ContainerResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def killed(self) -> bool:
        """Killed by SIGKILL (normally the memory limit); not a verdict on the code."""
        return self.returncode == OOM_EXIT


class DockerRunner:
    def __init__(self, config: DockerConfig, scratch_root: Path | None = None):
        self.config = config
        self.scratch_root = scratch_root

    def command(self, image: str, argv: list[str], *, mount: Path, name: str,
                env_names: tuple[str, ...] = (), home: Path | None = None) -> list[str]:
        c = self.config
        cmd = [
            c.cli, "run", "--rm", "-i", "--name", name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", f"/tmp:rw,exec,size={c.tmp_size}",
            "--env", f"HOME={HOME}",
            "--network", c.network,
            "--memory", c.memory,
            "--memory-swap", c.memory,
            "--oom-score-adj", "1000",
            "--cpus", c.cpus,
            "--cpu-shares", str(c.cpu_shares),
            "--pids-limit", str(c.pids_limit),
            "--volume", f"{mount.resolve()}:{WORKDIR}:rw",
            "--workdir", WORKDIR,
        ]
        if home is not None:
            cmd += ["--volume", f"{home.resolve()}:{HOME}:rw"]
        else:
            cmd += ["--tmpfs", f"{HOME}:rw,exec,size={c.tmp_size}"]
        if c.cgroup_parent:
            cmd += ["--cgroup-parent", c.cgroup_parent]
        for env_name in env_names:
            cmd += ["--env", env_name]  # value comes from the docker client's environment
        return [*cmd, image, *argv]

    def run(
        self, image: str, argv: list[str], *, mount: Path, timeout: int,
        env: Mapping[str, str] | None = None, stdin: str | None = None,
    ) -> ContainerResult:
        """Run argv in a fresh container. `env` values are forwarded by name, never in argv."""
        env = dict(env or {})
        name = f"dev-factory-{uuid.uuid4().hex[:12]}"
        home = None
        if self.scratch_root is not None:
            self.scratch_root.mkdir(parents=True, exist_ok=True)
            home = Path(tempfile.mkdtemp(prefix="home-", dir=self.scratch_root))
        cmd = self.command(image, argv, mount=mount, name=name, env_names=tuple(sorted(env)),
                           home=home)
        # The docker client needs its own environment (DOCKER_HOST etc.) plus the forwarded
        # values; nothing else from it reaches the container.
        client_env = {**os.environ, **env}
        try:
            proc = subprocess.run(cmd, input=stdin, env=client_env, text=True,
                                  capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            subprocess.run([self.config.cli, "rm", "--force", name], capture_output=True,
                           check=False, timeout=60)
            raise ContainerTimeout(f"container {name} exceeded {timeout}s") from exc
        finally:
            if home is not None:
                shutil.rmtree(home, ignore_errors=True)
        if proc.returncode == OOM_EXIT:
            log.warning("container killed (likely memory limit)", extra={
                "container": name, "image": image, "memory": self.config.memory})
        return ContainerResult(proc.returncode, proc.stdout, proc.stderr)
