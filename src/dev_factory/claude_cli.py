"""Runs Claude Code headless, normally inside the project's agent container.

Without an image (only with claude.allow_unisolated) it runs on the host with a minimal
environment. Either way the result is a validated structured object.
"""

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dev_factory.config import ClaudeConfig
from dev_factory.container import ContainerTimeout, DockerRunner

log = logging.getLogger(__name__)

# Never forwarded, even if listed in env_passthrough.
_FORBIDDEN_ENV = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK"})
_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
# Forwarded values at least this long are treated as secrets and scrubbed from agent output.
MIN_SECRET_LENGTH = 8
REDACTED = "[redacted]"
NO_ATTRIBUTION = {"attribution": {"commit": "", "pr": ""}, "includeCoAuthoredBy": False}

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "needs_input", "failed"]},
        "summary": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "commit_message": {"type": "string"},
    },
    "required": ["status", "summary"],
}

# Automated review: findings instead of file changes. "blocking" findings are sent back to
# the authoring agent; "suggestion" findings are only shown to the human reviewer.
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "failed"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocking", "suggestion"]},
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "problem": {"type": "string"},
                    "evidence": {"type": "string"},
                    "impact": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "title", "problem", "evidence", "impact", "fix"],
            },
        },
    },
    "required": ["status", "summary", "findings"],
}

# Triage (agent.triage): a read-only discussion of the issue before the spec. No
# open_questions means the issue is clear and the spec starts.
_STRINGS = {"type": "array", "items": {"type": "string"}}
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "failed"]},
        "summary": {"type": "string"},
        "size": {"type": "string", "enum": ["small", "large"]},
        "in_scope": _STRINGS,
        "out_of_scope": _STRINGS,
        "assumptions": _STRINGS,
        "code_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "finding": {"type": "string"},
                },
                "required": ["finding"],
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "suggested_answer": {"type": "string"},
                },
                "required": ["question", "suggested_answer"],
            },
        },
    },
    "required": ["status", "summary", "size", "in_scope", "out_of_scope", "assumptions",
                 "code_findings", "open_questions"],
}


class ClaudeCLIError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentResult:
    status: str  # done | needs_input | failed
    summary: str
    questions: tuple[str, ...] = ()
    cost_usd: float | None = None
    # The full structured output, for schemas with more than status/summary/questions.
    output: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def failure(cls, summary: str) -> "AgentResult":
        return cls("failed", summary)


class ClaudeCLI:
    def __init__(self, config: ClaudeConfig, runner: DockerRunner | None = None):
        self.config = config
        self.runner = runner

    def ensure_available(self) -> None:
        if shutil.which(self.config.cli) is None:
            raise ClaudeCLIError(f"Claude Code CLI not found: {self.config.cli}")

    def environment(self, *, base: bool = True) -> dict[str, str]:
        names = (*(_BASE_ENV if base else ()), *self.config.env_passthrough)
        source = {**read_env_file(self.config.env_file), **os.environ}
        return {
            name: source[name]
            for name in names
            if name in source and name not in _FORBIDDEN_ENV
        }

    def secrets(self) -> tuple[str, ...]:
        """Values the agent receives (its token) that must never leave through its output."""
        return tuple(v for v in self.environment(base=False).values()
                     if len(v) >= MIN_SECRET_LENGTH)

    def command(
        self, allowed_tools: tuple[str, ...], schema: dict[str, Any] = RESULT_SCHEMA,
    ) -> list[str]:
        cmd = [
            self.config.cli, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--max-turns", str(self.config.max_turns),
            "--max-budget-usd", str(self.config.max_budget_usd),
            # Deny anything not explicitly allowed; never bypass permissions.
            "--permission-mode", "dontAsk",
            "--allowedTools", ",".join(allowed_tools),
            # Ignore settings and hooks shipped inside the (untrusted) repository.
            "--setting-sources", "user",
            # No Co-Authored-By or "Generated with" lines: the factory's trailers attribute.
            "--settings", json.dumps(NO_ATTRIBUTION),
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if self.config.model:
            cmd += ["--model", self.config.model]
        return cmd

    def run(
        self, prompt: str, *, cwd: Path, allowed_tools: tuple[str, ...], image: str | None = None,
        schema: dict[str, Any] = RESULT_SCHEMA,
    ) -> AgentResult:
        """Everything in the result is scrubbed of secrets: it ends up in commits and comments."""
        if image is not None:
            result = self._run_in_container(prompt, cwd, allowed_tools, image, schema)
        else:
            result = self._run_on_host(prompt, cwd, allowed_tools, schema)
        return redact_result(result, self.secrets())

    def _run_on_host(
        self, prompt: str, cwd: Path, allowed_tools: tuple[str, ...],
        schema: dict[str, Any] = RESULT_SCHEMA,
    ) -> AgentResult:
        self.ensure_available()
        try:
            proc = subprocess.run(
                self.command(allowed_tools, schema),
                input=prompt,  # stdin, not argv: no size limit and not visible in ps
                cwd=cwd,
                env=self.environment(),
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AgentResult.failure(f"Claude timed out after {self.config.timeout_seconds}s")
        return parse_result(proc.returncode, proc.stdout, proc.stderr)

    def _run_in_container(
        self, prompt: str, cwd: Path, allowed_tools: tuple[str, ...], image: str,
        schema: dict[str, Any] = RESULT_SCHEMA,
    ) -> AgentResult:
        if self.runner is None:
            raise ClaudeCLIError("container runs need a DockerRunner")
        try:
            result = self.runner.run(
                image, self.command(allowed_tools, schema), mount=cwd,
                timeout=self.config.timeout_seconds,
                env=self.environment(base=False), stdin=prompt,
            )
        except ContainerTimeout as exc:
            return AgentResult.failure(f"Claude timed out: {exc}")
        return parse_result(result.returncode, result.stdout, result.stderr)


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    """value with every occurrence of a secret replaced, through nested lists and dicts."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, list | tuple):
        return type(value)(redact(v, secrets) for v in value)
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    return value


def redact_result(result: AgentResult, secrets: tuple[str, ...]) -> AgentResult:
    if not secrets:
        return result
    return replace(result, summary=redact(result.summary, secrets),
                   questions=redact(result.questions, secrets),
                   output=redact(result.output, secrets))


def read_env_file(path: Path | None) -> dict[str, str]:
    """KEY=VALUE lines; the file must not be readable by group or others."""
    if path is None or not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        raise ClaudeCLIError(f"{path} must not be accessible by group/others (chmod 600)")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def parse_result(returncode: int, stdout: str, stderr: str) -> AgentResult:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        tail = (stderr or stdout).strip()[-500:]
        return AgentResult.failure(f"Claude exited {returncode} without JSON output: {tail}")
    cost = data.get("total_cost_usd")
    if data.get("is_error") or data.get("subtype") != "success":
        return AgentResult("failed", f"Claude run ended with {data.get('subtype')}: "
                                     f"{str(data.get('result', ''))[:500]}", cost_usd=cost)
    output = data.get("structured_output")
    if not isinstance(output, dict) or output.get("status") not in ("done", "needs_input",
                                                                     "failed"):
        return AgentResult("failed", "Claude returned no valid structured result", cost_usd=cost)
    questions = tuple(str(q) for q in output.get("questions") or ())
    if output["status"] == "needs_input" and not questions:
        return AgentResult("failed", "Claude asked for input without questions", cost_usd=cost)
    return AgentResult(output["status"], str(output.get("summary", ""))[:4000], questions, cost,
                       output)
