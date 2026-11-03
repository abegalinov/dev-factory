import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from dev_factory.claude_cli import ClaudeCLI, ClaudeCLIError
from dev_factory.config import (
    CONFIG_DIR_ENV,
    ConfigError,
    FactoryConfig,
    ProjectConfig,
    ProjectEntry,
    load_entries,
    load_factory,
    resolve_config_dir,
)
from dev_factory.container import DockerRunner
from dev_factory.deployer import DEFAULT_CONFIG as DEFAULT_DEPLOYER_CONFIG
from dev_factory.factory import Factory
from dev_factory.github_cli import GitHubCLI, GitHubCLIError
from dev_factory.labels import catalog, plan_bootstrap
from dev_factory.release import make_adapter
from dev_factory.repo_config import CONFIG_DIR, RepoConfigLoader, load_local
from dev_factory.runtime import (
    AlreadyRunning,
    busy_reason,
    configure_logging,
    host_lock_path,
    host_resources,
    process_lock,
)
from dev_factory.workspace import Workspace, WorkspaceError


def _load(config_dir: Path) -> tuple[FactoryConfig, list[ProjectEntry]]:
    factory = load_factory(config_dir)
    return factory, load_entries(config_dir / "projects")


def _gh(factory: FactoryConfig) -> GitHubCLI:
    return GitHubCLI(factory.github.cli, factory.github.timeout_seconds,
                     factory.github.max_retries)


def _select(projects: list[ProjectEntry], project_id: str | None) -> list[ProjectEntry]:
    if project_id is None:
        return [p for p in projects if p.enabled]
    chosen = [p for p in projects if p.project_id == project_id]
    if not chosen:
        raise ConfigError(f"unknown project: {project_id}")
    if chosen[0].repository.startswith("YOUR_"):
        raise ConfigError(f"{project_id}: repository is still a placeholder")
    return chosen


def cmd_validate(config_dir: Path) -> int:
    factory, projects = _load(config_dir)
    print(f"Factory home: {factory.home}")
    print(f"Observe-only: {factory.runtime.observe_only}")
    print(f"Projects loaded: {len(projects)}")
    for project in projects:
        state = "enabled" if project.enabled else "disabled"
        print(f"- {project.project_id}: {project.repository} ({state}, "
              f"agent image {project.agent_image or 'unset'})")
    if not any(p.enabled for p in projects):
        print("No projects enabled. Edit config/projects/*.yaml before polling.")
    print(f"Project settings are read from each repository's {CONFIG_DIR}/ on its default "
          "branch; `dev-factory doctor` checks them.")
    return 0


def cmd_config_check(config_dir: Path, directory: Path, project_id: str | None) -> int:
    """Validate a local .dev-factory/ directory against this factory's host config."""
    factory, entries = _load(config_dir)
    entry = next((e for e in entries if e.project_id == project_id), None)
    if project_id and entry is None:
        raise ConfigError(f"unknown project: {project_id}")
    entry = entry or ProjectEntry("check", "local/check", enabled=False)
    project = load_local(directory, entry, factory)
    agent = project.agent
    assert agent is not None
    print(f"OK: {directory}")
    print(f"  spec directory:   {project.spec_directory}")
    print(f"  verification:     {project.verification.mode}, required checks "
          f"{list(project.verification.required_checks) or 'all reported'}")
    print(f"  automated review: {agent.review}")
    print(f"  protected paths:  {list(agent.protected_paths)}")
    print(f"  components:       {[c.name for c in project.components] or 'none'}")
    print("  templates:        " + ", ".join(
        name for name, text in (("spec", project.spec_template),
                                ("review", project.review_checklist),
                                ("pr-body", project.pr_body_template)) if text) or "none")
    if project.policy:
        print(f"  policy:           {project.policy}")
    return 0


def cmd_doctor(config_dir: Path, project_id: str | None) -> int:
    factory, projects = _load(config_dir)
    failures = 0

    def report(level: str, message: str) -> None:
        nonlocal failures
        failures += level == "FAIL"
        print(f"[{level:4}] {message}")

    report("OK" if shutil.which("git") else "FAIL", "git is installed")
    docker = factory.docker.cli
    docker_ok = shutil.which(docker) is not None and subprocess.run(
        [docker, "info"], capture_output=True, check=False, timeout=30).returncode == 0
    report("OK" if docker_ok else "FAIL", f"{docker} daemon reachable")
    try:
        forwarded = ClaudeCLI(factory.claude).environment(base=False)
        report("OK" if forwarded else "WARN",
               f"Claude credentials forwarded to containers: {sorted(forwarded) or 'none'} "
               f"(expects one of {list(factory.claude.env_passthrough)} in the environment "
               f"or {factory.claude.env_file})")
    except ClaudeCLIError as exc:
        report("FAIL", str(exc))
    if factory.claude.allow_unisolated:
        report("WARN", "claude.allow_unisolated is true: agents may run on the host")
    _doctor_resources(factory, report)
    report("OK" if factory.runtime.observe_only else "WARN",
           f"observe_only = {factory.runtime.observe_only}")
    gh = _gh(factory)
    try:
        gh.ensure_available()
        user = gh.current_user()
        report("OK", f"gh authenticated as {user.login}")
    except GitHubCLIError as exc:
        report("FAIL", str(exc))
        return 1

    loader = RepoConfigLoader(gh, factory)
    for entry in _select(projects, project_id):
        repo = entry.repository
        print(f"\n{entry.project_id} ({repo})")
        try:
            gh.repository(repo)
        except GitHubCLIError as exc:
            report("FAIL", f"repository not accessible: {exc}")
            continue
        try:
            project = loader(entry)
        except ConfigError as exc:
            report("FAIL", f"repository config: {exc}")
            continue
        report("OK", f"{CONFIG_DIR}/config.yaml valid on {project.default_branch} at "
                     f"{(project.config_sha or '')[:12]}")
        permission = gh.permission(repo, user.login)
        if permission == "write":
            report("OK", f"{user.login} has write permission")
        elif permission == "admin":
            report("WARN", f"{user.login} has admin; write is sufficient")
        else:
            report("FAIL", f"{user.login} has '{permission}'; write is required")
        branch = gh.branch(repo, project.default_branch)
        if branch is None:
            report("FAIL", f"default branch {project.default_branch} not found")
        elif branch.get("protected"):
            report("OK", f"{project.default_branch} is protected (rules are not visible to "
                         "collaborators; confirm required reviews with the owner)")
        else:
            report("WARN", f"{project.default_branch} is NOT protected: ask the owner to "
                           "require pull request reviews")
        images = [("agent", project.agent.image if project.agent else None)]
        if project.verification.mode == "container":
            images.append(("verification", project.verification.image))
        for role, image in images:
            if not image:
                report("WARN", f"no {role} image: {role} runs are refused in mutation mode")
            elif docker_ok and subprocess.run([docker, "image", "inspect", image],
                                              capture_output=True, check=False,
                                              timeout=30).returncode == 0:
                report("OK", f"{role} image {image} is available locally")
            else:
                report("FAIL", f"{role} image {image} not found (build or pull it)")
        plan = plan_bootstrap(catalog(project.labels), gh.list_labels(repo))
        if plan.create:
            report("FAIL", f"missing labels: {', '.join(lbl.name for lbl in plan.create)} "
                           "(run `dev-factory labels bootstrap`)")
        else:
            report("OK", f"all {len(plan.present)} factory labels exist")
        for expected, actual in plan.drifted:
            report("WARN", f"label {expected.name} differs from catalog "
                           f"(color {actual.color}, description {actual.description!r})")
    return 1 if failures else 0


def _size_mb(value: str) -> int:
    units = {"k": 1 / 1024, "m": 1, "g": 1024}
    value = value.strip().lower().rstrip("b")
    return int(float(value[:-1]) * units[value[-1]]) if value[-1] in units else int(value) // 2**20


def _doctor_resources(factory: FactoryConfig, report) -> None:
    res = host_resources()
    runtime, docker = factory.runtime, factory.docker
    report("OK", f"host: {res.cpus} CPUs, {res.total_mb} MB RAM, {res.available_mb} MB "
                 f"available, load {res.load1:.1f}")
    container_mb = _size_mb(docker.memory) + _size_mb(docker.tmp_size)
    if container_mb > res.total_mb // 2:
        report("WARN", f"docker.memory + tmp_size ({container_mb} MB) exceeds half of host RAM; "
                       "a factory container could starve other services")
    if float(docker.cpus) >= res.cpus:
        report("WARN", f"docker.cpus ({docker.cpus}) leaves no CPU for other services")
    if runtime.min_available_memory_mb < 256:
        report("WARN", f"runtime.min_available_memory_mb ({runtime.min_available_memory_mb}) is "
                       "too low to protect other services")
    try:
        with open("/proc/swaps", encoding="ascii") as stream:
            has_swap = len(stream.read().strip().splitlines()) > 1
    except OSError:
        has_swap = True
    report("OK" if has_swap else "WARN",
           "swap is configured" if has_swap else "no swap: memory spikes go straight to OOM kills "
                                                 "(see deploy/host/setup-host.sh)")
    if docker.cgroup_parent:
        state = subprocess.run(["systemctl", "show", docker.cgroup_parent, "-p", "MemoryMax",
                                "--value"], capture_output=True, text=True, check=False)
        limit = state.stdout.strip()
        if limit and limit != "infinity":
            report("OK", f"containers run in {docker.cgroup_parent} (MemoryMax={limit})")
        else:
            report("WARN", f"{docker.cgroup_parent} has no memory limit; install "
                           "deploy/systemd/dev-factory.slice")
    else:
        report("WARN", "docker.cgroup_parent is not set: no combined cap for factory containers")
    busy = busy_reason(res, runtime.min_available_memory_mb, runtime.max_load_per_cpu)
    if busy:
        report("WARN", f"heavy actions would be deferred right now: {busy}")


def cmd_labels_bootstrap(config_dir: Path, project_id: str | None, apply: bool) -> int:
    factory, projects = _load(config_dir)
    gh = _gh(factory)
    gh.ensure_available()
    loader = RepoConfigLoader(gh, factory)
    for entry in _select(projects, project_id):
        project = loader(entry)  # label names may be overridden in the repository config
        plan = plan_bootstrap(catalog(project.labels), gh.list_labels(project.repository))
        print(f"{project.project_id} ({project.repository}): {len(plan.present)} present, "
              f"{len(plan.create)} to create, {len(plan.drifted)} drifted (left unchanged)")
        for label in plan.create:
            print(f"  {'create' if apply else 'would create'} {label.name} #{label.color}")
            if apply:
                gh.create_label(project.repository, label.name, label.color, label.description)
        for expected, actual in plan.drifted:
            print(f"  drifted {expected.name}: color {actual.color} vs {expected.color}")
    if not apply:
        print("Dry run. Re-run with --apply to create missing labels.")
    return 0


def cmd_poll(config_dir: Path) -> int:
    factory, projects = _load(config_dir)
    configure_logging(factory.runtime.log_level)
    if not any(p.enabled for p in projects):
        print("No enabled projects; nothing to poll.")
        return 0
    with process_lock(host_lock_path()):
        gh = _gh(factory)
        gh.ensure_available()
        user = gh.current_user()

        def make_workspace(project: ProjectConfig) -> Workspace:
            return Workspace(factory.runtime_root / project.project_id, project.repository,
                             author_name=user.login, author_email=user.noreply_email)

        runner = DockerRunner(factory.docker, scratch_root=factory.runtime_root / "scratch")
        poller = Factory(
            factory, projects, gh, ClaudeCLI(factory.claude, runner),
            make_release=lambda project: make_adapter(project.release, gh),
            make_workspace=make_workspace,
            clock=lambda: datetime.now(UTC),
            runner=runner,
            load_project=RepoConfigLoader(gh, factory),
        )
        report = poller.poll()
    print(json.dumps(asdict(report), indent=2, default=str))
    return 1 if report.errors else 0


def cmd_deployer(args: argparse.Namespace) -> int:
    """The production host's pull agent (docs/deployer.md); needs no factory configuration."""
    import signal

    from dev_factory import deployer
    from dev_factory.swarm import Swarm

    if args.deployer_command == "install":
        from dev_factory.install import install

        try:
            install(Path(args.deployer_config), args.repository, args.approver or [],
                    workdir=args.workdir, image_prefixes=args.image_prefix,
                    registry=args.registry, registry_user=args.registry_user,
                    image=args.image)
        except (deployer.GitHubError, deployer.RuntimeFailure) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return 0
    config = deployer.load_config(Path(args.deployer_config))
    if args.deployer_command == "retry":
        previous = deployer.retry(config, args.repository)
        print(f"{args.repository}: forgot {previous}; the next pass applies main again")
        return 0
    if args.deployer_command == "status":
        print("\n".join(deployer.status(config)))
        return 0
    configure_logging("INFO")
    stop = []
    # Finish the current pass on `docker stop`, so an apply is never cut off half way.
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    return deployer.run(config, Swarm(), once=args.once, stopping=lambda: bool(stop))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev-factory")
    parser.add_argument(
        "--config-dir",
        help=(
            "Directory containing factory.yaml and projects/ "
            f"(default: ${CONFIG_DIR_ENV} or ./config)"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Check the host configuration offline")
    config = subparsers.add_parser("config", help="Repository configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help=f"Validate a local {CONFIG_DIR}/ directory")
    check.add_argument("directory", type=Path, help=f"Path to the {CONFIG_DIR}/ directory")
    check.add_argument("--project", help="Check against this project's host entry")
    doctor = subparsers.add_parser("doctor", help="Check tools, access, and labels (read-only)")
    doctor.add_argument("--project", help="Check one project, even if disabled")
    labels = subparsers.add_parser("labels", help="Manage factory labels")
    labels_sub = labels.add_subparsers(dest="labels_command", required=True)
    bootstrap = labels_sub.add_parser("bootstrap", help="Create missing labels (dry run default)")
    bootstrap.add_argument("--project", help="Project id, even if disabled")
    bootstrap.add_argument("--apply", action="store_true", help="Actually create labels")
    poll = subparsers.add_parser("poll", help="Run one discovery/decision pass")
    poll.add_argument("--once", action="store_true", help="Accepted for compatibility")
    deploy = subparsers.add_parser(
        "deployer", help="Production host agent: apply approved release manifests")
    deploy.add_argument("--config", dest="deployer_config", default=str(DEFAULT_DEPLOYER_CONFIG),
                        help=f"Deployer configuration (default: {DEFAULT_DEPLOYER_CONFIG})")
    deploy_sub = deploy.add_subparsers(dest="deployer_command", required=True)
    deploy_run = deploy_sub.add_parser("run", help="Reconcile every target, every interval")
    deploy_run.add_argument("--once", action="store_true", help="One pass, then exit")
    deploy_retry = deploy_sub.add_parser("retry", help="Try the current manifest again")
    deploy_retry.add_argument("repository", help="owner/name of the target")
    deploy_sub.add_parser("status", help="What each target last did")
    deploy_install = deploy_sub.add_parser(
        "install", help="Set up or upgrade this host: swarm, configuration, container")
    deploy_install.add_argument("repository", nargs="?",
                                help="owner/name to add; omit to upgrade or repair")
    deploy_install.add_argument("--approver", action="append",
                                help="GitHub login whose merges reach production (repeatable)")
    deploy_install.add_argument("--workdir", help="Env files and state (default: /srv/<name>)")
    deploy_install.add_argument("--image-prefix", action="append",
                                help="Allowed image prefix (default: <registry>/<owner>/<name>/)")
    deploy_install.add_argument("--registry", default="ghcr.io", help="Default: ghcr.io")
    deploy_install.add_argument("--registry-user", help="Default: the first approver")
    deploy_install.add_argument("--image", help="Deployer image to run (default: this one)")
    args = parser.parse_args(argv)
    try:
        if args.command == "deployer":
            return cmd_deployer(args)
        config_dir = resolve_config_dir(args.config_dir)
        if args.command == "validate":
            return cmd_validate(config_dir)
        if args.command == "config":
            return cmd_config_check(config_dir, args.directory, args.project)
        if args.command == "doctor":
            return cmd_doctor(config_dir, args.project)
        if args.command == "labels":
            return cmd_labels_bootstrap(config_dir, args.project, args.apply)
        if args.command == "poll":
            return cmd_poll(config_dir)
    except AlreadyRunning as exc:
        print(f"SKIP: {exc}", file=sys.stderr)
        return 0
    # Expected operational failures get a one-line message; anything else is a bug
    # and should surface with a traceback.
    except (ConfigError, GitHubCLIError, ClaudeCLIError, WorkspaceError, OSError,
            subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
