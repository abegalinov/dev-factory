import subprocess

import pytest
import yaml

from dev_factory.config import ConfigError
from dev_factory.deployer import GitHubError, RuntimeFailure, load_config
from dev_factory.install import install
from dev_factory.swarm import Swarm

IMAGE = "ghcr.io/acme/factory/deployer:1.0.0"
CONFIG = """release:
  production_environment: production
  deploy: {runtime: swarm, manifest: deploy/production.yml, stack: app}
"""


class FakeDocker:
    def __init__(self, swarm="inactive", addr="", container=False):
        self.calls = []
        self.swarm, self.addr, self.container = swarm, addr, container
        self.fail = set()

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        out, code = "", 1 if args[0] in self.fail else 0
        if args[0] == "info":
            out = f"{self.swarm} {self.addr}\n"
        elif args[:2] == ["container", "inspect"]:
            code = 0 if self.container else 1
        return subprocess.CompletedProcess(argv, code, out, "denied" if code else "")

    def ran(self, command):
        return [c for c in self.calls if c[0] == command]


class FakeAPI:
    def __init__(self, config=CONFIG):
        self.config = config
        self.tokens = []

    def __call__(self, token):
        self.tokens.append(token)
        return self

    def default_branch(self, repo):
        if self.tokens[-1] == "bad":
            raise GitHubError(f"GET repos/{repo}: HTTP 401")
        return "main"

    def head(self, repo, branch):
        return "c" * 40

    def file(self, repo, path, ref):
        return ("blob", self.config) if self.config else None


def answers(*values):
    pending = list(values)
    asked = []

    def ask(question):
        asked.append(question)
        return pending.pop(0)

    ask.asked = asked
    return ask


def run(tmp_path, docker, *, repository="acme/app", approvers=("maintainer",), ask=None,
        api=None, **kwargs):
    lines = []
    install(tmp_path / "deployer.yaml", repository, list(approvers), image=IMAGE,
            swarm=Swarm(run=docker), api=api or FakeAPI(),
            ask=ask or answers("gh-token", "registry-token"), say=lines.append, **kwargs)
    return lines


def test_fresh_host(tmp_path):
    docker = FakeDocker()
    ask = answers("gh-token", "registry-token")
    api = FakeAPI()
    lines = run(tmp_path, docker, ask=ask, api=api)

    assert docker.ran("swarm") == [["swarm", "init", "--advertise-addr", "127.0.0.1",
                                    "--listen-addr", "127.0.0.1:2377"]]
    config = load_config(tmp_path / "deployer.yaml")
    (target,) = config.targets
    assert target.workdir.as_posix() == "/srv/app"
    assert target.approvers == ("maintainer",)
    assert target.image_prefixes == ("ghcr.io/acme/app/",)
    assert target.registry.username == "maintainer"
    assert target.token_file.read_text() == "gh-token\n"
    assert target.registry.password_file.read_text() == "registry-token\n"
    for path in (tmp_path / "deployer.yaml", target.token_file, target.registry.password_file):
        assert path.stat().st_mode & 0o777 == 0o600
    assert api.tokens == ["gh-token"]
    assert ["login", "ghcr.io", "-u", "maintainer", "--password-stdin"] in docker.calls
    (started,) = docker.ran("run")
    assert started[-4:] == [IMAGE, "--config", str(tmp_path / "deployer.yaml"), "run"]
    assert f"{tmp_path}:{tmp_path}:ro" in started and "/srv/app:/srv/app" in started
    assert not docker.ran("stop")
    assert any("4789/udp" in line for line in lines)


def test_public_images_need_no_registry_token(tmp_path):
    docker = FakeDocker()
    run(tmp_path, docker, ask=answers("gh-token", ""))
    (target,) = load_config(tmp_path / "deployer.yaml").targets
    assert target.registry is None
    assert not docker.ran("login")


def test_upgrade_keeps_the_configuration_and_replaces_the_container(tmp_path):
    run(tmp_path, FakeDocker())
    before = (tmp_path / "deployer.yaml").read_text()
    docker = FakeDocker(swarm="active", addr="127.0.0.1", container=True)
    ask = answers()
    run(tmp_path, docker, repository=None, approvers=(), ask=ask)

    assert (tmp_path / "deployer.yaml").read_text() == before
    assert not ask.asked and not docker.ran("swarm")
    order = [c[0] for c in docker.calls if c[0] in ("stop", "rm", "run")]
    assert order == ["stop", "rm", "run"]


def test_adding_a_configured_repository_changes_nothing(tmp_path):
    run(tmp_path, FakeDocker())
    before = (tmp_path / "deployer.yaml").read_text()
    run(tmp_path, FakeDocker(swarm="active"), approvers=("someone-else",), ask=answers())
    assert (tmp_path / "deployer.yaml").read_text() == before


def test_a_second_repository_is_added(tmp_path):
    run(tmp_path, FakeDocker())
    docker = FakeDocker(swarm="active", addr="127.0.0.1", container=True)
    run(tmp_path, docker, repository="acme/other", ask=answers("gh-2", ""))
    raw = yaml.safe_load((tmp_path / "deployer.yaml").read_text())
    assert [t["repository"] for t in raw["targets"]] == ["acme/app", "acme/other"]
    (started,) = docker.ran("run")
    assert "/srv/app:/srv/app" in started and "/srv/other:/srv/other" in started


def test_missing_token_files_are_asked_again(tmp_path):
    run(tmp_path, FakeDocker())
    (target,) = load_config(tmp_path / "deployer.yaml").targets
    target.token_file.unlink()
    ask = answers("new-token")
    run(tmp_path, FakeDocker(swarm="active"), repository=None, ask=ask)
    assert len(ask.asked) == 1 and target.token_file.read_text() == "new-token\n"


def test_a_rejected_token_names_the_file_to_delete(tmp_path):
    docker = FakeDocker()
    with pytest.raises(ConfigError, match=r"HTTP 401.*delete .*app\.token"):
        run(tmp_path, docker, ask=answers("bad", ""))
    assert not docker.ran("run")


def test_a_public_swarm_address_is_reported(tmp_path):
    lines = run(tmp_path, FakeDocker(swarm="active", addr="203.0.113.7"))
    assert any("203.0.113.7" in line and "2377/tcp" in line for line in lines)


@pytest.mark.parametrize("problem", ["no-deploy", "login", "locked"])
def test_problems_stop_before_the_container_starts(tmp_path, problem):
    docker = FakeDocker(swarm="locked" if problem == "locked" else "inactive")
    api = FakeAPI(config="release: {}\n" if problem == "no-deploy" else CONFIG)
    if problem == "login":
        docker.fail.add("login")
    error = RuntimeFailure if problem == "locked" else ConfigError
    with pytest.raises(error, match={"no-deploy": "release.deploy", "login": "delete .*registry",
                                     "locked": "locked"}[problem]):
        run(tmp_path, docker, api=api)
    assert not docker.ran("run")


@pytest.mark.parametrize(("repository", "approvers", "match"), [
    ("acme", ("maintainer",), "owner/name"),
    ("acme/app", (), "--approver"),
    (None, (), "no targets"),
])
def test_bad_arguments(tmp_path, repository, approvers, match):
    with pytest.raises(ConfigError, match=match):
        run(tmp_path, FakeDocker(), repository=repository, approvers=approvers)


def test_an_image_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("DEPLOYER_IMAGE", raising=False)
    with pytest.raises(ConfigError, match="image"):
        install(tmp_path / "deployer.yaml", "acme/app", ["maintainer"],
                swarm=Swarm(run=FakeDocker()), say=lambda _: None)
