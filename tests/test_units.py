import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dev_factory import marker as markers
from dev_factory.claude_cli import ClaudeCLI, parse_result
from dev_factory.config import ClaudeConfig, DockerConfig
from dev_factory.container import DockerRunner
from dev_factory.engine import CheckState, changes_requested, summarize_checks
from dev_factory.github_cli import GitHubCLI, GitHubCLIError, NotFoundError
from dev_factory.labels import catalog, plan_bootstrap, priority_rank
from dev_factory.models import Check, Comment, Label, PullRequest, Review
from dev_factory.runtime import AlreadyRunning, process_lock
from dev_factory.state_machine import DEFAULT_LABELS, Stage
from dev_factory.workspace import Workspace, violations

T0 = datetime(2026, 10, 27, tzinfo=UTC)


# -- marker -----------------------------------------------------------------------

def test_marker_round_trip():
    m = markers.StateMarker(stage=Stage.NEEDS_INPUT, attempt=2, resume_stage=Stage.SPEC,
                            last_failure="a --> b", spec_pr=7)
    body = markers.render(m, "status")
    assert "-->" not in body.split("\n", 1)[0][:-4]  # payload cannot close the comment
    assert markers.parse(body) == m


@pytest.mark.parametrize("body", [
    "no marker", "<!-- dev-factory:state {bad json} -->",
    '<!-- dev-factory:state {"v":2,"stage":"spec"} -->',
    '<!-- dev-factory:state {"v":1,"stage":"nope"} -->',
])
def test_marker_rejects_invalid(body):
    assert markers.parse(body) is None


def test_marker_only_trusted_from_factory():
    body = markers.render(markers.StateMarker(stage=Stage.REVIEW), "x")
    forged = Comment(1, "attacker", body, T0, T0)
    real = Comment(2, "bot", markers.render(markers.StateMarker(stage=Stage.SPEC), "x"), T0, T0)
    found = markers.find([forged, real], "bot")
    assert found is not None and found[1].stage is Stage.SPEC
    assert markers.find([forged], "bot") is None


# -- labels -----------------------------------------------------------------------

def test_bootstrap_creates_missing_and_reports_drift_without_overwriting():
    expected = catalog(DEFAULT_LABELS)
    existing = [Label("FACTORY:SPEC", "000000", "custom")]
    plan = plan_bootstrap(expected, existing)
    assert len(expected) == 14
    assert "factory:spec" not in {lbl.name for lbl in plan.create}
    assert len(plan.create) == 13
    assert plan.drifted[0][0].name == "factory:spec"


def test_priority_rank():
    assert priority_rank(("priority:high", "type:bug")) == 0
    assert priority_rank(()) == 1
    assert priority_rank(("priority:low",)) == 2


# -- checks and reviews -------------------------------------------------------------

def _check(name, status="completed", conclusion="success"):
    return Check(name, status, conclusion)


def test_check_summary():
    assert summarize_checks([], ()).state is CheckState.PENDING
    assert summarize_checks([_check("a"), _check("b", conclusion="skipped")], ()).state \
        is CheckState.SUCCESS
    assert summarize_checks([_check("a"), _check("b", "in_progress", None)], ()).state \
        is CheckState.PENDING
    failed = summarize_checks([_check("a", conclusion="failure"), _check("b", "queued", None)], ())
    assert failed.state is CheckState.FAILURE and failed.failed[0].name == "a"


def test_required_checks_only_and_missing():
    summary = summarize_checks([_check("lint", conclusion="failure"), _check("test")], ("test",))
    assert summary.state is CheckState.SUCCESS
    missing = summarize_checks([_check("test")], ("test", "build"))
    assert missing.state is CheckState.PENDING and missing.missing == ("build",)


def test_newest_check_run_wins():
    newest_first = [_check("test"), _check("test", conclusion="failure")]
    assert summarize_checks(newest_first, ()).state is CheckState.SUCCESS


def test_changes_requested_only_applies_to_current_head():
    pr = PullRequest(1, "open", False, None, None, "b", "head2", "main", "")
    old = Review("m", "CHANGES_REQUESTED", "head1", T0)
    assert not changes_requested(pr, [old])
    new = Review("m", "CHANGES_REQUESTED", "head2", T0.replace(hour=1))
    assert changes_requested(pr, [old, new])
    approved_later = Review("m", "APPROVED", "head2", T0.replace(hour=2))
    assert not changes_requested(pr, [old, new, approved_later])


# -- claude -------------------------------------------------------------------------

def _claude_output(**overrides):
    data = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.1,
            "structured_output": {"status": "done", "summary": "ok"}}
    data.update(overrides)
    return json.dumps(data)


def test_parse_result_success_and_failures():
    assert parse_result(0, _claude_output(), "").status == "done"
    assert parse_result(1, "not json", "boom").status == "failed"
    assert parse_result(0, _claude_output(subtype="error_max_turns", is_error=True),
                        "").status == "failed"
    assert parse_result(0, _claude_output(structured_output=None), "").status == "failed"
    no_questions = _claude_output(structured_output={"status": "needs_input", "summary": "?"})
    assert parse_result(0, no_questions, "").status == "failed"
    asks = _claude_output(structured_output={"status": "needs_input", "summary": "?",
                                             "questions": ["Which?"]})
    assert parse_result(0, asks, "").questions == ("Which?",)


def test_claude_environment_is_minimal(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    config = ClaudeConfig(env_passthrough=("ANTHROPIC_API_KEY", "GH_TOKEN"))
    env = ClaudeCLI(config).environment()
    assert env["ANTHROPIC_API_KEY"] == "key"
    assert "GH_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env


def test_claude_command_is_locked_down():
    cmd = ClaudeCLI(ClaudeConfig()).command(("Read",))
    assert cmd[:2] == ["claude", "-p"]
    assert "bypassPermissions" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in cmd


# -- gh retries ----------------------------------------------------------------------

class _ScriptedGh(GitHubCLI):
    def __init__(self, outcomes):
        super().__init__(sleep=lambda s: None)
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(self, args, *, check=True, stdin=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return subprocess.CompletedProcess(args, 0, outcome, "")


def test_reads_retry_transient_errors():
    gh = _ScriptedGh([GitHubCLIError("HTTP 502"), GitHubCLIError("rate limit"),
                      '{"login": "bot", "id": 5}'])
    assert gh.current_user().login == "bot" and gh.calls == 3


def test_reads_do_not_retry_permanent_errors():
    gh = _ScriptedGh([GitHubCLIError("HTTP 403 forbidden"), "{}"])
    with pytest.raises(GitHubCLIError):
        gh.repository("a/b")
    assert gh.calls == 1


def test_not_found_maps_to_none():
    gh = _ScriptedGh([NotFoundError("HTTP 404")])
    assert gh.branch("a/b", "nope") is None


def test_mutations_are_never_retried():
    gh = _ScriptedGh([GitHubCLIError("HTTP 502"), "{}"])
    with pytest.raises(GitHubCLIError):
        gh.set_labels("a/b", 1, ["x"])
    assert gh.calls == 1


def test_paginated_object_pages_are_flattened():
    pages = json.dumps([{"total_count": 2, "check_runs": [
        {"name": "a", "status": "completed", "conclusion": "success"}]},
        {"total_count": 2, "check_runs": [
            {"name": "b", "status": "queued", "conclusion": None}]}])
    gh = _ScriptedGh([pages, '{"statuses": []}'])
    assert [c.name for c in gh.checks("a/b", "sha")] == ["a", "b"]


# -- runtime ---------------------------------------------------------------------------

def test_process_lock_is_exclusive(tmp_path):
    lock = tmp_path / "factory.lock"
    with process_lock(lock), pytest.raises(AlreadyRunning), process_lock(lock):
        pass
    with process_lock(lock):
        pass


# -- workspace ---------------------------------------------------------------------------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote(tmp_path) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("hello\n")
    _git("add", ".", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init", cwd=seed)
    _git("push", "origin", "HEAD:main", cwd=seed)
    return bare


def test_workspace_round_trip(tmp_path, remote):
    ws = Workspace(tmp_path / "rt", "acme/app", author_name="bot", author_email="b@b",
                   remote_url=str(remote))
    task = ws.prepare("factory/spec/1", "main")
    assert not (task.agent_dir / ".git").exists()
    assert (task.agent_dir / "README.md").read_text() == "hello\n"

    (task.agent_dir / "specs").mkdir()
    (task.agent_dir / "specs" / "issue-1.md").write_text("spec\n")
    (task.agent_dir / "README.md").unlink()
    nested_git = task.agent_dir / "vendor" / ".git"
    nested_git.mkdir(parents=True)
    (nested_git / "config").write_text("[core]\n\thooksPath = /tmp/evil\n")
    assert sorted(ws.collect(task)) == ["README.md", "specs/issue-1.md"]
    sha = ws.commit_and_push(task, "spec")
    ws.cleanup(task)

    heads = subprocess.run(["git", "ls-remote", str(remote), "factory/spec/1"],
                           capture_output=True, text=True, check=True).stdout
    assert heads.startswith(sha)

    again = ws.prepare("factory/spec/1", "main")  # continues the existing branch
    assert (again.agent_dir / "specs" / "issue-1.md").exists()
    assert again.base_sha == sha


def test_protected_path_violations():
    assert violations([".github/workflows/ci.yml", "src/a.py"], (".github/**",)) == [
        ".github/workflows/ci.yml"]
    assert violations(["specs/features/issue-1.md"], ("specs/features/**",))


def test_container_command_is_locked_down(tmp_path):
    runner = DockerRunner(DockerConfig(network="none"))
    cmd = runner.command("img:1", ["claude", "-p"], mount=tmp_path, name="n",
                         env_names=("CLAUDE_CODE_OAUTH_TOKEN",))
    joined = " ".join(cmd)
    for flag in ("--rm", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--network none", "--pids-limit", "--memory 768m", "--memory-swap 768m",
                 "--oom-score-adj 1000", "--cpu-shares 256"):
        assert flag in joined
    assert f"{tmp_path}:/workspace:rw" in cmd
    assert cmd.count("--volume") == 1  # only the task directory is mounted
    assert "--cgroup-parent" not in cmd
    assert "--env CLAUDE_CODE_OAUTH_TOKEN" in joined  # by name; value never in argv
    assert "--privileged" not in cmd and "/var/run/docker.sock" not in joined
    assert cmd[-3:] == ["img:1", "claude", "-p"]


def test_container_runs_forward_only_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    seen = {}

    class Runner:
        def run(self, image, argv, *, mount, timeout, env=None, stdin=None):
            seen.update(image=image, env=env, stdin=stdin, mount=mount)
            from dev_factory.container import ContainerResult
            return ContainerResult(0, json.dumps({
                "subtype": "success", "is_error": False,
                "structured_output": {"status": "done", "summary": "ok"}}), "")

    result = ClaudeCLI(ClaudeConfig(), Runner()).run("do it", cwd=tmp_path,
                                                     allowed_tools=("Read",), image="img")
    assert result.status == "done"
    assert seen["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    assert seen["stdin"] == "do it" and seen["mount"] == tmp_path


def test_container_home_on_disk_and_slice(tmp_path):
    runner = DockerRunner(DockerConfig(cgroup_parent="dev-factory.slice"))
    home = tmp_path / "home"
    cmd = runner.command("img", ["true"], mount=tmp_path / "w", name="n", home=home)
    assert f"{home}:/home/agent:rw" in cmd
    assert not any(a.startswith("/home/agent:") for a in cmd)  # HOME is not a tmpfs
    assert cmd[cmd.index("--cgroup-parent") + 1] == "dev-factory.slice"


def test_busy_reason():
    from dev_factory.runtime import HostResources, busy_reason
    ok = HostResources(available_mb=2000, total_mb=4000, load1=1.0, cpus=2)
    assert busy_reason(ok, 1400, 1.5) is None
    low = HostResources(available_mb=600, total_mb=1800, load1=0.1, cpus=2)
    assert "600 MB" in busy_reason(low, 1400, 1.5)
    loaded = HostResources(available_mb=2000, total_mb=4000, load1=3.5, cpus=2)
    assert "load 3.5" in busy_reason(loaded, 1400, 1.5)


def test_host_resources_reads_proc():
    from dev_factory.runtime import host_resources
    res = host_resources()
    assert res.total_mb > 0 and 0 <= res.available_mb <= res.total_mb and res.cpus >= 1


def test_project_instructions_reach_both_prompts(tmp_path):
    from dev_factory.models import Issue
    from dev_factory.prompts import implement_prompt, spec_prompt
    template = tmp_path / "t.md"
    template.write_text("TEMPLATE")
    issue = Issue(1, "Title", "Body", "open", (), "", T0, "someone")
    notes = "Run only targeted tests."
    for prompt in (spec_prompt(template, issue, (), "docs/specs/issue-1.md", None, notes),
                   implement_prompt(template, issue, (), "s.md", None, None, notes)):
        assert "## Project notes" in prompt and notes in prompt
        # Operator notes sit outside the untrusted fences.
        assert prompt.index(notes) < prompt.index("<issue>")
    assert "## Project notes" not in spec_prompt(template, issue, (), "p.md", None)


def test_env_file_supplies_only_passthrough_values(tmp_path, monkeypatch):
    from dev_factory.claude_cli import ClaudeCLIError
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    env_file = tmp_path / "agent.env"
    env_file.write_text("# agent\nexport CLAUDE_CODE_OAUTH_TOKEN='tok'\nGH_TOKEN=nope\nOTHER=x\n")
    env_file.chmod(0o600)
    cli = ClaudeCLI(ClaudeConfig(env_file=env_file))
    assert cli.environment(base=False) == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    env_file.chmod(0o644)
    with pytest.raises(ClaudeCLIError, match="chmod 600"):
        cli.environment(base=False)
    assert ClaudeCLI(ClaudeConfig(env_file=tmp_path / "missing")).environment(base=False) == {}


def test_failed_step_excerpt_keeps_the_error_and_caps_groups():
    from dev_factory.observer import failed_step_excerpt
    log = "\n".join([
        "2026-10-27T10:00:00.0Z ##[group]Run actions/checkout@v4",
        "2026-10-27T10:00:01.0Z ##[endgroup]",
        "2026-10-27T10:00:02.0Z ##[group]Run ./tests.sh",
        "2026-10-27T10:00:02.1Z env:",
        "2026-10-27T10:00:02.2Z ##[endgroup]",
        "2026-10-27T10:00:03.0Z \x1b[31mBuilding\x1b[0m",
        "2026-10-27T10:00:04.0Z ##[group]\u274c login test (failed)",
        "2026-10-27T10:00:04.1Z Expected: exactly one",
        *[f"2026-10-27T10:00:05.0Z stack frame {i}" for i in range(50)],
        "2026-10-27T10:00:06.0Z ##[endgroup]",
        "2026-10-27T10:00:07.0Z ##[error]1 test failed.",
        "2026-10-27T10:00:08.0Z ##[group]Run post step",
    ])
    out = failed_step_excerpt(log, group_lines=5)
    lines = out.splitlines()
    assert lines[0] == "Run ./tests.sh"
    assert "env:" not in out and "checkout" not in out and "post step" not in out
    assert "Building" in out and "\x1b" not in out
    assert "Expected: exactly one" in out and "stack frame 3" in out
    assert "stack frame 4" not in out and "... (46 lines omitted)" in out
    assert lines[-1] == "##[error]1 test failed."


def test_failed_step_excerpt_skips_passing_test_groups():
    from dev_factory.observer import failed_step_excerpt
    passing = []
    for i in range(200):
        passing += [f"##[group]\u2705 test {i}", f"ok {i}", "##[endgroup]"]
    log = "\n".join([
        "##[group]Run flutter test",
        "##[endgroup]",
        "##[group]\u274c guest tap opens login (failed)",
        "Bad state: AuthScope not found in widget tree",
        "##[endgroup]",
        *passing,
        "##[error]367 tests passed, 1 failed.",
    ])
    out = failed_step_excerpt(log)
    assert "AuthScope not found" in out
    assert "test 199" not in out and "ok 5" not in out


def test_failed_step_excerpt_shortens_stack_traces():
    from dev_factory.observer import failed_step_excerpt
    trace = []
    for i in range(40):
        trace += [f"#{i}      frame{i} (package:app/a.dart:{i}:1)", "<asynchronous suspension>"]
    log = "\n".join([
        "##[group]Run tests", "##[endgroup]",
        "Bad state: Missing user id in auth session",
        *trace,
        "##[error]3 tests passed, 2 failed.",
    ])
    out = failed_step_excerpt(log, max_lines=20)
    assert "Missing user id" in out
    assert "frame5 " in out and "frame6 " not in out
    assert "asynchronous suspension" not in out


def test_agent_runs_interleave_projects_within_a_priority():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from dev_factory.factory import interleave

    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    def cand(project, minutes, labels=()):
        issue = SimpleNamespace(labels=list(labels), updated_at=t0 + timedelta(minutes=minutes))
        return SimpleNamespace(project=SimpleNamespace(project_id=project),
                               snapshot=SimpleNamespace(issue=issue), name=f"{project}{minutes}")

    busy = [cand("a", m) for m in (1, 2, 3)]
    quiet = cand("b", 9)
    urgent = cand("a", 50, ["priority:high"])
    order = [c.name for c in interleave([*busy, quiet, urgent])]
    assert order == ["a50", "a1", "b9", "a2", "a3"]
