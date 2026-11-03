"""Ephemeral runtime concerns: the process lock, host resource checks, structured logging."""

import fcntl
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class AlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class HostResources:
    available_mb: int
    total_mb: int
    load1: float
    cpus: int


def host_resources() -> HostResources:
    meminfo: dict[str, int] = {}
    with open("/proc/meminfo", encoding="ascii") as stream:
        for line in stream:
            key, value = line.split(":", 1)
            meminfo[key] = int(value.split()[0])  # kB
    return HostResources(
        available_mb=meminfo["MemAvailable"] // 1024,
        total_mb=meminfo["MemTotal"] // 1024,
        load1=os.getloadavg()[0],
        cpus=os.cpu_count() or 1,
    )


def busy_reason(res: HostResources, min_available_mb: int, max_load_per_cpu: float) -> str | None:
    """Why heavy work should wait, or None if the host has headroom."""
    if res.available_mb < min_available_mb:
        return f"only {res.available_mb} MB memory available (need {min_available_mb})"
    if res.load1 > max_load_per_cpu * res.cpus:
        return f"load {res.load1:.1f} exceeds {max_load_per_cpu} per CPU on {res.cpus} CPUs"
    return None


@contextmanager
def process_lock(path: Path) -> Iterator[None]:
    """Non-blocking exclusive lock. The kernel releases it if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(f"another dev-factory run holds {path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
