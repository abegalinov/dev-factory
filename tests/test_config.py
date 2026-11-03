from pathlib import Path

import pytest

from dev_factory.config import (
    ConfigError,
    ProjectEntry,
    load_entries,
    load_entry,
    load_factory,
    resolve_config_dir,
)
from dev_factory.repo_config import BASELINE_PROTECTED, build_project, load_local

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
ROOT = CONFIG_DIR.parent
EXAMPLE = ROOT / "examples" / "dev-factory"
ENTRY = ProjectEntry("app", "acme/app", enabled=True, agent_image="agent:1")


def _factory():
    return load_factory(CONFIG_DIR)


def _repo(text, **files):
    return build_project(ENTRY, {"config.yaml": text, **files}, _factory(),
                         default_branch="main", config_sha="abc123")


# -- host entries -------------------------------------------------------------------------

def test_shipped_example_entry():
    # Only the example is shipped; real registrations are git-ignored and operator-local.
    example = load_entry(CONFIG_DIR / "projects" / "example.yaml")
    assert example.project_id == "example-project" and not example.enabled
    assert example.repository == "YOUR_ORG/YOUR_REPO"
    assert example.agent_image == "dev-factory-agent-node20:2.1.311"


def test_enabled_placeholder_repository_is_rejected(tmp_path):
    (tmp_path / "p.yaml").write_text("repository: YOUR_ORG/YOUR_REPO\nenabled: true\n")
    with pytest.raises(ConfigError):
        load_entries(tmp_path)


def test_project_settings_in_the_host_entry_point_to_the_repository(tmp_path):
    (tmp_path / "p.yaml").write_text("repository: acme/app\nverification: {}\n")
    with pytest.raises(ConfigError, match="moved to the repository's .dev-factory"):
        load_entries(tmp_path)


def test_unknown_host_keys_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="observe_onyl"):
        (tmp_path / "factory.yaml").write_text("version: 1\nruntime:\n  observe_onyl: false\n")
        load_factory(tmp_path)
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "p.yaml").write_text("repository: acme/app\nagent_imag: x\n")
    with pytest.raises(ConfigError, match="agent_imag"):
        load_entries(projects)


def test_config_dir_precedence(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    from_env = tmp_path / "env"
    for d in (explicit, from_env):
        d.mkdir()
        (d / "factory.yaml").write_text("version: 1\n")
    monkeypatch.setenv("DEV_FACTORY_CONFIG", str(from_env))
    assert resolve_config_dir(str(explicit)) == explicit
    assert resolve_config_dir() == from_env


def test_config_dir_without_factory_yaml_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        resolve_config_dir(str(tmp_path))


def test_shipped_factory_config_is_observe_only():
    factory = _factory()
    assert factory.runtime.observe_only is True
    assert factory.claude.allow_unisolated is False


# -- repository config (.dev-factory/) --------------------------------------------------------

def test_example_repository_config_is_valid_and_realistic():
    project = load_local(EXAMPLE, ENTRY, _factory())
    assert project.spec_directory == "docs/specs"
    assert project.verification.required_checks == (
        "api", "admin", "e2e", "mobile-checks", "mobile-emulator")
    assert project.agent.review is True
    assert "768 MB" in project.agent.instructions
    assert "mobile (apps/mobile/**)" in project.agent.instructions
    assert "## 4. Acceptance criteria" in project.spec_template
    assert project.review_checklist and "{{summary}}" in project.pr_body_template


def test_example_repository_config_shows_every_setting():
    """The example is the reference: a new setting must be added (and explained) there."""
    import yaml

    from dev_factory.config import ReleaseConfig
    from dev_factory.repo_config import (
        _REPO_AGENT_KEYS,
        _REPO_KEYS,
        _REPO_POLICY_KEYS,
        _REPO_VERIFICATION_KEYS,
    )
    from dev_factory.state_machine import Stage

    raw = yaml.safe_load((EXAMPLE / "config.yaml").read_text())
    assert set(raw) == _REPO_KEYS
    assert set(raw["specification"]) == {"directory"}
    assert set(raw["labels"]) == {stage.value for stage in Stage}
    assert set(raw["agent"]) == _REPO_AGENT_KEYS
    assert set(raw["verification"]) == _REPO_VERIFICATION_KEYS
    assert set(raw["release"]) == set(ReleaseConfig.__annotations__)
    assert set(raw["policy"]) == _REPO_POLICY_KEYS
    assert {key for c in raw["components"] for key in c} == {"name", "paths", "instructions"}
    host = yaml.safe_load((CONFIG_DIR / "projects" / "example.yaml").read_text())
    from dev_factory.config import _ENTRY_KEYS
    documented = set(host) | {"verification_image", "default_branch"}  # commented out there
    assert documented == _ENTRY_KEYS
    for name in ("verification_image", "default_branch"):
        assert f"# {name}:" in (CONFIG_DIR / "projects" / "example.yaml").read_text()


def test_missing_repository_config_is_an_error():
    with pytest.raises(ConfigError, match="not found on main"):
        build_project(ENTRY, {}, _factory(), default_branch="main", config_sha="abc")


def test_repository_defaults_and_host_values():
    project = _repo("version: 1\n")
    assert project.spec_directory == "specs/features"
    assert project.agent.protected_paths == BASELINE_PROTECTED
    assert project.agent.image == "agent:1"  # from the host entry, never the repository
    assert project.agent.spec_prompt == ROOT / "prompts" / "spec.md"
    assert project.release.adapter == "none" and project.policy is None
    assert project.config_sha == "abc123"


def test_repository_protected_paths_extend_the_baseline():
    project = _repo("version: 1\nagent:\n  protected_paths: [infra/**]\n")
    assert project.agent.protected_paths == (*BASELINE_PROTECTED, "infra/**")


@pytest.mark.parametrize("key", ["image", "review_allowed_tools", "implementation_allowed_tools",
                                 "spec_prompt"])
def test_repository_cannot_set_operator_agent_settings(key):
    with pytest.raises(ConfigError, match=key):
        _repo(f"version: 1\nagent:\n  {key}: x\n")


def test_repository_policy_can_only_tighten():
    project = _repo("version: 1\npolicy:\n  max_implementation_attempts: 1\n")
    assert project.policy.max_implementation_attempts == 1
    assert project.policy.max_spec_attempts == _factory().policy.max_spec_attempts
    with pytest.raises(ConfigError, match="only tighten"):
        _repo("version: 1\npolicy:\n  max_implementation_attempts: 99\n")


def test_container_verification_needs_the_host_image():
    text = "version: 1\nverification:\n  mode: container\n  commands: [npm test]\n"
    with pytest.raises(ConfigError, match="verification_image"):
        _repo(text)
    entry = ProjectEntry("app", "acme/app", enabled=True, verification_image="verify:1")
    project = build_project(entry, {"config.yaml": text}, _factory(), default_branch="main",
                            config_sha=None)
    assert project.verification.image == "verify:1"


def test_repository_config_validation():
    with pytest.raises(ConfigError, match="unsupported version"):
        _repo("specification: {}\n")
    with pytest.raises(ConfigError, match="verificaton"):
        _repo("version: 1\nverificaton: {}\n")
    with pytest.raises(ConfigError, match="specs"):
        _repo("version: 1\nlabels:\n  specs: factory:spec\n")
    with pytest.raises(ConfigError, match="candidate_workflow"):
        _repo("version: 1\nrelease:\n  adapter: github-deployments\n")
    with pytest.raises(ConfigError, match="needs paths"):
        _repo("version: 1\ncomponents:\n  - name: api\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        _repo("version: 1\n  bad: [\n")


def test_instructions_combine_notes_and_components():
    project = _repo("version: 1\ncomponents:\n  - name: api\n    paths: [api/**]\n"
                    "    instructions: Run pytest.\n", **{"instructions.md": "Monorepo."})
    assert project.agent.instructions == (
        "Monorepo.\n\nComponents of this repository:\n- api (api/**)\n  Run pytest.")


def test_release_deploy_settings():
    project = _repo("version: 1\nrelease:\n  deploy:\n    manifest: deploy/production.yml\n"
                    "    stack: app\n")
    assert project.release.deploy.runtime == "swarm"
    assert project.release.deploy.manifest == "deploy/production.yml"
    assert project.release.deploy.url is None
    assert _repo("version: 1\n").release.deploy is None


@pytest.mark.parametrize("deploy, match", [
    ("{manifest: m.yml, stack: app, runtime: k8s}", "runtime"),
    ("{stack: app}", "manifest"),
    ("{manifest: ../m.yml, stack: app}", "manifest"),
    ("{manifest: /etc/m.yml, stack: app}", "manifest"),
    ("{manifest: m.yml, stack: App Stack}", "stack"),
    ("{manifest: m.yml, stack: app, approvers: [x]}", "approvers"),
    ("[m.yml]", "mapping"),
])
def test_release_deploy_validation(deploy, match):
    with pytest.raises(ConfigError, match=match):
        _repo(f"version: 1\nrelease:\n  deploy: {deploy}\n")
