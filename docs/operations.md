# Operating Dev Factory

How to install, onboard a repository, prove it in observe-only mode, and then enable it.
See [architecture.md](architecture.md) for how it works.

## 1. Install

Requirements: Python 3.11+, `git`, GitHub CLI `gh`, Claude Code CLI `claude`.

```bash
git clone git@github.com:abegalinov/dev-factory.git /opt/dev-factory
cd /opt/dev-factory
python3 -m venv .venv          # or: uv venv .venv   (if python3-venv is not installed)
.venv/bin/pip install -e ".[dev]"   # or: uv pip install -p .venv -e ".[dev]"
.venv/bin/pytest -q
```

The config directory is `--config-dir`, else `$DEV_FACTORY_CONFIG`, else `./config`. Relative
paths in config (prompts, `runtime_root`) are resolved against the parent of the config directory.

## 2. Identity

- Use a dedicated GitHub machine account if possible. The repository owner adds it as a
  collaborator with the **Write** role.
- Log in as that account on the factory host: `gh auth login` (HTTPS). Git pushes use `gh`
  as the credential helper on each command; global git config is not modified.
- Log in to Claude Code as the account that will run the agent (see isolation below).

## 3. Onboard a repository

In the repository, add `.dev-factory/` through a normal PR (start from
`examples/dev-factory/`, see [repo-config.md](repo-config.md)) and validate it first:

```bash
.venv/bin/dev-factory config check /path/to/checkout/.dev-factory
```

On the factory host, register the repository:

```bash
cp config/projects/example.yaml config/projects/<id>.yaml
# set repository: owner/repo and agent_image; keep enabled: false
.venv/bin/dev-factory validate
.venv/bin/dev-factory doctor --project <id>
.venv/bin/dev-factory labels bootstrap --project <id>          # dry run
.venv/bin/dev-factory labels bootstrap --project <id> --apply
.venv/bin/dev-factory doctor --project <id>                    # should be all OK/WARN
```

`doctor` reads `.dev-factory/config.yaml` from the default branch and fails if it is missing
or invalid; a poll skips such a repository and reports why.

Ask the repository owner to:

- protect the default branch and require at least one approving review;
- keep production secrets in a GitHub Environment with required reviewers, never in secrets
  available to `pull_request` workflows;
- optionally add an issue template that applies `type:*` labels (never `factory:*`).

## 4. Observe-only trial

Set `enabled: true` in the project file and keep `runtime.observe_only: true`.

```bash
.venv/bin/dev-factory poll
```

The poll prints, for each factory issue, the action it **would** take and why. It changes
nothing on GitHub and never runs Claude. Try it with real issues in every state you can
create: unlabelled, labelled by a non-maintainer, two lifecycle labels, an existing spec PR,
a merged PR. Check that each decision matches [issue-contract.md](issue-contract.md).

## 5. Host placement and resources

**Run the factory on its own machine, not on a production host.** The factory user needs
the `docker` group, which is root-equivalent on that host. A headless Claude Code run needs
about 300–400 MB and fits a 768 MB container. Full test suites are what need gigabytes
(a typical Jest setup requests a 4 GB heap), so they belong in CI. A
4 vCPU / 8 GB machine is comfortable; the factory keeps no state, so moving it is a clone
and a config copy. Prefer PR CI on GitHub (`verification.mode: github-checks`) over container
verification, because then tests use GitHub's runners and not yours.

When the factory has to share a host, run this once:

```bash
sudo sh deploy/host/setup-host.sh          # 2G swapfile, swappiness 10, dev-factory.slice
```

The safeguards then stack up:

| Layer | Setting | Effect |
|---|---|---|
| Per container | `docker.memory` (768m), `--memory-swap` = memory, `--oom-score-adj 1000` | A runaway test is killed before anything else on the host, and never swaps |
| Per container | `docker.cpus` (1), `docker.cpu_shares` (256 vs default 1024), `pids_limit` | Never takes all CPUs; yields to production under contention |
| Per container | HOME on disk (`.runtime/scratch`), `/tmp` tmpfs `tmp_size` (128m) | Package caches don't consume RAM |
| All factory work | `dev-factory.slice`: `CPUQuota=100%`, `CPUWeight=20`, `MemoryMax=1200M`, no swap | The poll service and all its containers together stay inside one budget |
| Before each heavy action | `runtime.min_available_memory_mb` (500), `runtime.max_load_per_cpu` (1.5) | Agent and verification runs wait for the next poll while the host is busy; label transitions still happen |
| Host | 2G swap, `vm.swappiness=10` | Memory spikes from any service degrade gracefully instead of triggering OOM kills |

A verification killed by the memory limit (exit 137) does not count as a test failure. Its
status stays pending and the checks timeout escalates it to `blocked` with a comment, so no
attempts are wasted. `dev-factory doctor` reports host capacity, swap, slice limits, oversized
container limits, and whether heavy work would currently be deferred.

Use the repository's `.dev-factory/instructions.md` (or per-component instructions) to tell
the agent about these limits (the example tells it to run only targeted tests with a small heap
and leave the full suites to CI).

On a bigger, dedicated machine, raise `docker.memory`, `docker.cpus`, the slice limits and
`min_available_memory_mb` together.

## 6. Agent isolation (Docker)

Claude runs inside the project's `agent_image`. Each container runs with your user's uid,
no capabilities, `no-new-privileges`, a read-only root filesystem, and only the task copy
(at `/workspace`, without `.git`) plus a disposable HOME mounted. Only the variables in
`claude.env_passthrough` are forwarded, by name. There is no Docker socket, no `gh`
credentials, and nothing else from the host.

```bash
docker build -t dev-factory-agent-node20:2.1.311 deploy/agent    # Node 20 + yarn + Claude Code
claude setup-token                                               # long-lived agent token
install -D -m 600 /dev/null ~/.config/dev-factory/agent.env
echo 'CLAUDE_CODE_OAUTH_TOKEN=...' > ~/.config/dev-factory/agent.env
```

The file is `claude.env_file` (default `~/.config/dev-factory/agent.env`) and must be mode
600. The systemd install below copies it into the `devfactory` account. Keep it out of
`/etc/dev-factory` on a host that also runs the [deployer](deployer.md): that directory holds
the deployer's tokens and is mounted into its container.

Set `agent_image` (and `verification_image` for container verification) in the host entry
`config/projects/<id>.yaml`. Repositories cannot choose images.
Without an agent image, agent runs are refused in mutation mode.
`claude.allow_unisolated: true` would run Claude on the host; don't use it.

## 7. Enable mutation mode

Checklist:

- [ ] `doctor --project <id>` reports no FAIL, and no resource warnings you haven't accepted.
- [ ] Observe-only decisions were reviewed on real issues.
- [ ] The agent image exists and `CLAUDE_CODE_OAUTH_TOKEN` reaches the service (doctor shows it forwarded).
- [ ] Default branch protection and environment protection are confirmed with the owner.
- [ ] `policy` limits and `claude.max_budget_usd` are acceptable.

Then set `runtime.observe_only: false`. Start with one test issue labelled `factory:spec` and
watch a few polls by hand before installing the timer.

### systemd

`deploy/host/install-factory.sh` does the whole install and is safe to run again to update:
a `devfactory` system user in the `docker` group (HOME `/var/lib/dev-factory`), a clone in
`/opt/dev-factory` with a virtualenv, your project registrations (enabled), the factory
account's `gh` login, the agent token, `validate` and `doctor`, and the systemd units.

```bash
sudo sh deploy/host/install-factory.sh \
  --project config/projects/<id>.yaml \
  --gh-user <factory-account> \
  --agent-env ~/.config/dev-factory/agent.env
# Later, after the observe-only polls look right:
sudo sh /opt/dev-factory/deploy/host/install-factory.sh --mutate
journalctl -u dev-factory-poll -f          # JSON log lines
```

`--gh-user` copies that account's token from your own `gh` login (`gh auth token --user`),
not your whole `gh` config. Without `--mutate` the timer runs observe-only. Host config edits
in `/opt/dev-factory/config` survive updates (`git pull --autostash`). To stop:
`sudo systemctl disable --now dev-factory-poll.timer`.

Polls start 3 minutes after the previous one ends, so they never overlap. A manual run while
the timer's run is active exits with `SKIP` because of the lock. The lock is per account
(`$XDG_STATE_HOME/dev-factory/factory.lock`, default `~/.local/state/dev-factory/`), so run
manual polls as the same user with the same `HOME`, e.g.
`sudo -u devfactory HOME=/var/lib/dev-factory …`.

## 8. Day-to-day

| Situation | What to do |
|---|---|
| Issue is `factory:blocked` | Read the factory's last comment and the status comment's *Last failure*. Fix the cause, then apply the work-stage label to resume. Counters reset |
| Factory asked a question | Answer in a comment (requires Write). The next poll resumes |
| Stop work on an issue | Close it, or remove its lifecycle label |
| Restart an implementation from scratch | Close the code PR, delete `factory/impl/<n>`, relabel `factory:implement` |
| Disk usage | `.runtime/` can be deleted between polls at any time |
| Status of everything | `dev-factory poll` in observe-only mode on a copy of the config, or read the status comments |
