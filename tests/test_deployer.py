import json
import subprocess
from pathlib import Path

import pytest

from dev_factory import deployer
from dev_factory.config import ConfigError
from dev_factory.deployer import (
    UNLINKED_PASSES,
    Action,
    RegistryLogin,
    RuntimeFailure,
    State,
    Target,
    TargetRun,
    decide,
    load_config,
)
from dev_factory.swarm import Swarm

REPO = "acme/app"
SHA = "a" * 40
OTHER = "b" * 40
CONFIG = """version: 1
release:
  adapter: github-deployments
  candidate_workflow: build.yml
  production_environment: production
  deploy:
    runtime: swarm
    manifest: deploy/production.yml
    stack: app
    url: https://app.example
"""


def manifest(release=SHA, api="ghcr.io/acme/app/api:1111", db="ghcr.io/acme/app/db:2222"):
    return (f"x-release: {release}\nservices:\n  api:\n    image: {api}\n"
            f"  db:\n    image: {db}\n")


class FakeAPI:
    """Main of one repository: its files at the head, who merged what, deployments."""

    def __init__(self):
        self.head_sha = "c1"
        self.files = {".dev-factory/config.yaml": CONFIG,
                      "deploy/production.yml": manifest(release="none")}
        self.changed_in = "m1"
        self.prs = {"m1": [(1, "maintainer")]}
        self.deployments = []  # [id, ref, environment, [statuses]]
        self.calls = []

    def push(self, text, *, commit, merged_by=None, head=None):
        self.files["deploy/production.yml"] = text
        self.changed_in = commit
        self.head_sha = head or f"h-{commit}"
        self.prs[commit] = merged_by or []

    def default_branch(self, repo):
        return "main"

    def head(self, repo, branch):
        self.calls.append("head")
        return self.head_sha

    def file(self, repo, path, ref):
        self.calls.append(f"file {path}")
        text = self.files.get(path)
        return None if text is None else (f"blob-{hash(text) & 0xffffffff:x}", text)

    def last_change(self, repo, path, ref):
        return self.changed_in

    def merged_by(self, repo, commit, branch):
        return self.prs.get(commit, [])

    def create_deployment(self, repo, ref, environment, description):
        self.deployments.append([len(self.deployments) + 1, ref, environment, []])
        return len(self.deployments)

    def deployment_status(self, repo, deployment, state, description, url):
        self.deployments[deployment - 1][3].append((state, url))


class FakeRuntime:
    def __init__(self):
        self.running = {}  # service → image
        self.applied = []
        self.fail = None
        self.prunes = []

    def apply(self, manifest_path, stack, registry):
        self.applied.append((manifest_path.name, stack, manifest_path.read_text()))
        if self.fail:
            raise RuntimeFailure(self.fail)
        self.running = deployer.manifest_images(manifest_path.read_text())[1]
        return ""

    def problems(self, stack, images, *, images_only):
        return [f"{stack}_{s} runs {self.running.get(s)}, wants {i}"
                for s, i in images.items() if self.running.get(s) != i]

    def prune(self, keep, prefixes):
        self.prunes.append(keep)


@pytest.fixture
def env(tmp_path):
    target = Target(REPO, tmp_path, ("maintainer",), tmp_path / "token",
                    image_prefixes=("ghcr.io/acme/app/",))
    api, runtime = FakeAPI(), FakeRuntime()

    def reconcile():
        return TargetRun(target, api, runtime).reconcile()
    return api, runtime, reconcile, tmp_path


# -- decide ---------------------------------------------------------------------------------

TARGET = Target(REPO, Path("/srv/app"), ("maintainer",), Path("/t"),
                image_prefixes=("ghcr.io/acme/app/",))


def test_decide_applies_an_approved_pinned_manifest():
    d = decide(manifest(), [(7, "maintainer")], TARGET)
    assert d.action is Action.APPLY and d.release == SHA
    assert d.images == {"api": "ghcr.io/acme/app/api:1111", "db": "ghcr.io/acme/app/db:2222"}
    assert d.reason == "#7 merged by maintainer"


@pytest.mark.parametrize("approvals", [[], [(3, "someone")], [(4, "")]])
def test_decide_rejects_what_no_approver_merged(approvals):
    d = decide(manifest(), approvals, TARGET)
    assert d.action is Action.REJECT and "not merged by an approver" in d.reason


def test_decide_accepts_any_approving_pr_of_the_commit():
    assert decide(manifest(), [(3, "someone"), (4, "maintainer")], TARGET).action is Action.APPLY


def test_decide_idles_before_the_first_release():
    assert decide(manifest(release="none"), [(1, "maintainer")], TARGET).action is Action.IDLE


@pytest.mark.parametrize("text", [
    manifest(release="main"),
    manifest(api="docker.io/evil/api:1"),
    manifest(api="ghcr.io/acme/app/api"),
    manifest(api="ghcr.io/acme/app/api:${TAG}"),
    "services: [",
    "x-release: " + SHA + "\nservices:\n  api: {}\n",
])
def test_decide_rejects_malformed_or_unpinned_manifests(text):
    assert decide(text, [(1, "maintainer")], TARGET).action is Action.REJECT


def test_decide_without_prefixes_allows_any_pinned_image():
    target = Target(REPO, Path("/srv"), ("maintainer",), Path("/t"))
    d = decide(manifest(api="docker.io/library/nginx:1.27"), [(1, "maintainer")], target)
    assert d.action is Action.APPLY


# -- reconcile ------------------------------------------------------------------------------

def test_lifecycle_idle_apply_unchanged(env):
    api, runtime, reconcile, workdir = env
    assert reconcile().outcome == "idle"
    assert runtime.applied == [] and api.deployments == []

    api.push(manifest(), commit="m2", merged_by=[(2, "maintainer")])
    state = reconcile()
    assert state.outcome == "applied" and state.applied == SHA and state.stack == "app"
    assert runtime.applied[0][:2] == (".incoming-production.yml", "app")
    assert (workdir / "production.yml").read_text() == manifest()
    assert not (workdir / ".incoming-production.yml").exists()
    assert api.deployments == [[1, SHA, "production", [
        ("in_progress", "https://app.example"), ("success", "https://app.example")]]]
    assert runtime.prunes and "ghcr.io/acme/app/api:1111" in runtime.prunes[0]

    api.calls.clear()
    assert reconcile().outcome == "applied"
    assert api.calls == ["head"]  # unchanged head: nothing else read
    assert len(runtime.applied) == 1

    api.head_sha = "c9"  # an unrelated commit on main
    reconcile()
    assert len(runtime.applied) == 1 and len(api.deployments) == 1


def test_only_changed_manifests_are_applied_again(env):
    api, runtime, reconcile, _ = env
    api.push(manifest(), commit="m2", merged_by=[(2, "maintainer")])
    reconcile()
    api.push(manifest(release=OTHER, api="ghcr.io/acme/app/api:3333"), commit="m3",
             merged_by=[(3, "maintainer")])
    state = reconcile()
    assert state.applied == OTHER and runtime.running["api"] == "ghcr.io/acme/app/api:3333"
    assert [d[1] for d in api.deployments] == [SHA, OTHER]


def test_direct_push_and_foreign_merges_are_rejected_once(env):
    api, runtime, reconcile, workdir = env
    api.push(manifest(), commit="m2")  # no PR
    for _ in range(UNLINKED_PASSES):
        assert reconcile().outcome != "rejected"  # waiting for GitHub to link a PR
    state = reconcile()
    assert state.outcome == "rejected" and "no PR" in state.detail and state.unlinked == 0
    api.push(manifest(api="ghcr.io/acme/app/api:4444"), commit="m3",
             merged_by=[(5, "contributor")])
    assert reconcile().outcome == "rejected"
    api.head_sha = "later"
    assert reconcile().outcome == "rejected"
    assert runtime.applied == [] and api.deployments == []
    history = (workdir / ".deployer" / "history").read_text().splitlines()
    assert len(history) == 2 and all(" rejected " in line for line in history)


def test_a_merge_github_links_late_is_still_applied(env):
    api, runtime, reconcile, _ = env
    api.push(manifest(), commit="m2")  # merged, but the PR link is not visible yet
    assert reconcile().unlinked == 1 and runtime.applied == []
    api.prs["m2"] = [(2, "maintainer")]
    state = reconcile()
    assert state.outcome == "applied" and state.unlinked == 0 and len(runtime.applied) == 1


def test_failure_is_reported_and_not_retried_until_asked(env, tmp_path):
    api, runtime, reconcile, workdir = env
    runtime.fail = "app_api update rollback_completed"
    api.push(manifest(), commit="m2", merged_by=[(2, "maintainer")])
    state = reconcile()
    assert state.outcome == "failed" and "rollback_completed" in state.detail
    assert api.deployments[0][3][-1][0] == "failure"
    assert not (workdir / "production.yml").exists()
    api.head_sha = "later"
    reconcile()
    assert len(runtime.applied) == 1

    runtime.fail = None
    config = deployer.DeployerConfig(targets=(Target(REPO, workdir, ("maintainer",),
                                                     tmp_path / "token"),))
    assert deployer.retry(config, REPO).startswith("failed")
    assert reconcile().outcome == "applied"
    assert len(api.deployments) == 2 and api.deployments[1][3][-1][0] == "success"


def test_verification_problems_fail_the_release(env):
    api, runtime, reconcile, _ = env
    runtime.problems = lambda stack, images, images_only: ["app_db replicas 0/1"]
    api.push(manifest(), commit="m2", merged_by=[(2, "maintainer")])
    state = reconcile()
    assert state.outcome == "failed" and state.detail == "app_db replicas 0/1"


def test_an_interrupted_apply_is_reported_failed_not_repeated(env):
    api, runtime, reconcile, workdir = env
    api.push(manifest(), commit="m2", merged_by=[(2, "maintainer")])
    api.create_deployment(REPO, SHA, "production", "")
    State(head=api.head_sha, blob=api.file(REPO, "deploy/production.yml", "")[0],
          outcome="applying", release=SHA, deployment=1).save(
        workdir / ".deployer" / "state.json")
    state = reconcile()
    assert state.outcome == "failed" and "interrupted" in state.detail
    assert api.deployments[0][3] == [("failure", None)]
    assert runtime.applied == []


def test_drift_from_the_applied_release_is_repaired(env):
    api, runtime, reconcile, _ = env
    api.push(manifest(), commit="m2", merged_by=[(2, "maintainer")])
    reconcile()
    runtime.running["api"] = "ghcr.io/acme/app/api:manual"
    assert reconcile().outcome == "applied"
    assert runtime.applied[-1][0] == "production.yml"
    assert runtime.running["api"] == "ghcr.io/acme/app/api:1111"
    assert len(api.deployments) == 1  # a repair is not a new release


def test_failed_repair_stops_repairing(env):
    api, runtime, reconcile, _ = env
    api.push(manifest(), commit="m2", merged_by=[(2, "maintainer")])
    reconcile()
    runtime.running["api"] = "ghcr.io/acme/app/api:manual"
    runtime.fail = "no such image"
    assert reconcile().outcome == "failed"
    reconcile()
    assert len(runtime.applied) == 2


def test_missing_deploy_settings_are_an_error(env):
    api, _, reconcile, _ = env
    api.files[".dev-factory/config.yaml"] = "version: 1\n"
    with pytest.raises(ConfigError, match="release.deploy is not set"):
        reconcile()


def test_run_contains_errors_per_target(tmp_path):
    good = Target("acme/good", tmp_path / "good", ("maintainer",), tmp_path / "token")
    bad = Target("acme/bad", tmp_path / "bad", ("maintainer",), tmp_path / "missing")
    (tmp_path / "token").write_text("t\n")
    seen = []

    def api(token):
        seen.append(token)
        return FakeAPI()
    config = deployer.DeployerConfig(targets=(bad, good))
    assert deployer.run(config, FakeRuntime(), once=True, api=api) == 1
    assert seen == ["t"]
    assert "idle" in deployer.status(config)[1]


# -- host configuration ---------------------------------------------------------------------

def test_host_config(tmp_path):
    path = tmp_path / "deployer.yaml"
    path.write_text("""
interval_seconds: 20
targets:
  - repository: acme/app
    workdir: /srv/app
    approvers: [maintainer]
    token_file: /etc/dev-factory/app.token
    image_prefixes: [ghcr.io/acme/app/]
    registry: {server: ghcr.io, username: maintainer, password_file: /etc/dev-factory/ghcr}
""")
    config = load_config(path)
    assert config.interval_seconds == 20 and config.keep_releases == 5
    target = config.targets[0]
    assert target.approvers == ("maintainer",) and target.workdir == Path("/srv/app")
    assert target.registry == RegistryLogin("ghcr.io", "maintainer",
                                            Path("/etc/dev-factory/ghcr"))


@pytest.mark.parametrize("text, match", [
    ("targets: []\n", "no targets"),
    ("targets:\n  - {repository: a/b, workdir: /w, token_file: /t}\n", "approvers"),
    ("targets:\n  - {repository: a/b, workdir: w, token_file: /t, approvers: [x]}\n", "absolute"),
    ("targets:\n  - {repository: ab, workdir: /w, token_file: /t, approvers: [x]}\n", "owner/name"),
    ("targets:\n  - {repository: a/b, workdir: /w, token_file: /t, approvers: [x], aprovers: 1}\n",
     "aprovers"),
    ("intervall: 3\ntargets: []\n", "intervall"),
    ("targets:\n  - {repository: a/b, workdir: /w, token_file: /t, approvers: [x]}\n"
     "  - {repository: a/c, workdir: /w, token_file: /t, approvers: [x]}\n", "own workdir"),
])
def test_host_config_errors(tmp_path, text, match):
    path = tmp_path / "deployer.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=match):
        load_config(path)


# -- swarm runtime --------------------------------------------------------------------------

class FakeDocker:
    def __init__(self, services=None):
        self.calls = []
        self.services = services or {}  # name → (image, update state, replicas)
        self.fail = set()

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append((args, kwargs))
        out, code = "", 1 if args[0] in self.fail else 0
        if args[:2] == ["stack", "services"]:
            out = "\n".join(json.dumps({"Name": n, "Replicas": r})
                            for n, (_, _, r) in self.services.items())
        elif args[:2] == ["service", "inspect"]:
            out = json.dumps([{"Spec": {"Name": n, "TaskTemplate": {"ContainerSpec": {
                "Image": f"{self.services[n][0]}@sha256:abc"}}},
                "UpdateStatus": {"State": self.services[n][1]} if self.services[n][1] else None}
                for n in args[2:]])
        elif args[:2] == ["image", "ls"]:
            out = "ghcr.io/acme/app/api:old\nghcr.io/acme/app/api:1111\nnginx:1.27\n"
        return subprocess.CompletedProcess(argv, code, out, "boom" if code else "")


def test_swarm_apply_logs_in_with_a_throwaway_config(tmp_path):
    docker = FakeDocker()
    (tmp_path / "pw").write_text("secret\n")
    path = tmp_path / "production.yml"
    Swarm(run=docker).apply(path, "app", RegistryLogin("ghcr.io", "me", tmp_path / "pw"))
    (login, login_kw), (deploy, deploy_kw) = docker.calls
    assert login == ["login", "ghcr.io", "-u", "me", "--password-stdin"]
    assert login_kw["input"] == "secret" and "secret" not in " ".join(login)
    assert deploy == ["stack", "deploy", "--prune", "--with-registry-auth", "--detach=false",
                      "-c", str(path), "app"]
    assert deploy_kw["env"]["DOCKER_CONFIG"] == login_kw["env"]["DOCKER_CONFIG"]
    assert not Path(deploy_kw["env"]["DOCKER_CONFIG"]).exists()


def test_swarm_apply_failure_raises(tmp_path):
    docker = FakeDocker()
    docker.fail.add("stack")
    with pytest.raises(RuntimeFailure, match="boom"):
        Swarm(run=docker).apply(tmp_path / "m.yml", "app", None)


def test_swarm_problems():
    docker = FakeDocker({
        "app_api": ("ghcr.io/acme/app/api:1111", "", "1/1"),
        "app_db": ("ghcr.io/acme/app/db:1", "rollback_completed", "1/1"),
        "app_web": ("ghcr.io/acme/app/web:1", "updating", "0/1"),
    })
    swarm = Swarm(run=docker)
    wanted = {"api": "ghcr.io/acme/app/api:1111", "db": "ghcr.io/acme/app/db:2",
              "web": "ghcr.io/acme/app/web:1", "cache": "redis:7"}
    assert swarm.problems("app", wanted, images_only=False) == [
        "app_cache missing",
        "app_db runs ghcr.io/acme/app/db:1, wants ghcr.io/acme/app/db:2 (rollback_completed)",
        "app_web replicas 0/1",
    ]
    assert swarm.problems("app", wanted, images_only=True) == [
        "app_cache missing",
        "app_db runs ghcr.io/acme/app/db:1, wants ghcr.io/acme/app/db:2 (rollback_completed)",
    ]
    del wanted["cache"], wanted["web"]
    wanted["db"] = "ghcr.io/acme/app/db:1"
    docker.services["app_db"] = ("ghcr.io/acme/app/db:1", "updating", "1/1 (max 1 per node)")
    assert swarm.problems("app", wanted, images_only=False) == []


def test_swarm_prune_keeps_released_and_foreign_images():
    docker = FakeDocker()
    Swarm(run=docker).prune({"ghcr.io/acme/app/api:1111"}, {"ghcr.io/acme/app/api"})
    removed = [args[2] for args, _ in docker.calls if args[:2] == ["image", "rm"]]
    assert removed == ["ghcr.io/acme/app/api:old"]


# -- GitHub API -----------------------------------------------------------------------------

class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        import io
        import urllib.error
        self.requests.append(request)
        code, body = self.responses.pop(0)
        if code >= 400:
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b""))

        class Response(io.BytesIO):
            status = code
        return Response(body.encode())


def _api(opener):
    from dev_factory.deployer import GitHubAPI
    return GitHubAPI("tok", "https://api", opener=opener)


def test_api_reads_files_and_retries_reads(monkeypatch):
    monkeypatch.setattr(deployer.time, "sleep", lambda s: None)
    opener = FakeOpener((502, ""), (200, json.dumps({"type": "file", "sha": "b1",
                                                     "content": "aGk="})), (404, ""))
    api = _api(opener)
    assert api.file(REPO, "deploy/production.yml", SHA) == ("b1", "hi")
    assert api.file(REPO, "missing.yml", SHA) is None
    assert opener.requests[0].get_header("Authorization") == "Bearer tok"


def test_api_never_repeats_writes():
    from dev_factory.deployer import GitHubError
    opener = FakeOpener((502, ""), (201, '{"id": 9}'))
    with pytest.raises(GitHubError, match="502"):
        _api(opener).create_deployment(REPO, SHA, "production", "x")
    assert len(opener.requests) == 1


def test_unrecorded_deployment_applies_nothing(env):
    from dev_factory.deployer import GitHubError
    api, runtime, reconcile, _ = env

    def down(*args):
        raise GitHubError("POST deployments: HTTP 502")
    api.create_deployment = down
    api.push(manifest(), commit="m2", merged_by=[(2, "maintainer")])
    with pytest.raises(GitHubError):
        reconcile()
    assert runtime.applied == []
    assert State.load(env[3] / ".deployer" / "state.json").outcome == "failed"


def test_swarm_docker_timeouts_are_runtime_failures(tmp_path):
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)
    with pytest.raises(RuntimeFailure, match="stack deploy"):
        Swarm(run=slow).apply(tmp_path / "m.yml", "app", None)
