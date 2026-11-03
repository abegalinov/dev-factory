# Dev Factory

A GitHub-native, AI-assisted feature delivery orchestrator. It takes an issue through
**spec → spec review → implementation → verification → code review → release**. Claude Code
writes the spec and the code. A deterministic Python orchestrator enforces the workflow.
Humans approve the spec merge, the code merge and the production deployment.

- **GitHub is the only durable state:** issues, labels, branches, PRs, checks, comments and
  deployments. There is no database; local files are disposable.
- **Claude creates, the orchestrator decides.** Claude never changes labels, merges or
  deploys. The orchestrator moves an issue only after seeing evidence on GitHub.
- **Safe to crash and safe to re-run.** Every poll rebuilds its view from GitHub, reuses
  existing branches and PRs, and changes the label last.
- **Project-agnostic.** A repository describes itself in `.dev-factory/`; the host only
  registers it (repository, enabled, agent image).

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | **Start here.** How everything works: parts, the poll loop, a feature end to end, state, safety model |
| [docs/issue-contract.md](docs/issue-contract.md) | The exact rules: labels, intake, authorization, marker, every stage's evidence and exits, recovery |
| [docs/operations.md](docs/operations.md) | Install, onboard a repository, observe-only trial, agent isolation, enabling mutations, systemd, day-to-day |

## Lifecycle at a glance

```
factory:spec ─► factory:spec-review ─► factory:implement ─► factory:verify ─► factory:review ─► factory:release
   (agent)          (human merges)          (agent)            (CI checks)      (human merges)    (human deploys)

exceptions: factory:needs-input (a maintainer answers)   factory:blocked (a maintainer fixes and relabels)
metadata:   type:bug|feature|chore   priority:high|medium|low
```

A maintainer starts work by adding `factory:spec` to an issue. Labels added by users without
Write access are ignored.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # or use uv
.venv/bin/pytest -q

.venv/bin/dev-factory validate                                # offline config check
.venv/bin/dev-factory doctor --project <id>                   # tools, identity, access, labels
.venv/bin/dev-factory labels bootstrap --project <id>         # dry run; add --apply to create
.venv/bin/dev-factory poll                                    # one pass; observe-only by default
```

## Commands

| Command | Effect |
|---|---|
| `validate` | Parse and validate all config, offline |
| `doctor [--project ID]` | Read-only checks: `git`/`gh`/`claude`, identity, write permission, branch protection, labels, isolation |
| `labels bootstrap [--project ID] [--apply]` | Create missing factory labels. Never modifies existing ones |
| `poll` | One discovery/decision pass. In observe-only mode it only prints decisions |

All commands accept `--config-dir` (default: `$DEV_FACTORY_CONFIG`, else `./config`).

## Configuration

- `config/factory.yaml`: global runtime, GitHub, Claude and policy settings. Unknown keys are errors.
  - `runtime.observe_only: true` (shipped default). No GitHub writes, no Claude runs.
  - `docker`: limits for every agent/verification container (sized for a small shared host).
  - `runtime.min_available_memory_mb` / `max_load_per_cpu`: heavy work waits while the host is busy.
- `config/projects/<id>.yaml`: registers one repository: `repository`, `enabled` and the
  `agent_image` (operator-chosen, because the agent container receives the Claude token).
- `.dev-factory/` **in each repository**, read from its default branch: spec directory,
  instructions, monorepo components, required checks, release adapter, extra protected paths,
  templates. The factory stays generic. See [docs/repo-config.md](docs/repo-config.md) and the
  realistic example in [examples/dev-factory/](examples/dev-factory/); validate a copy with
  `dev-factory config check path/to/.dev-factory`.
- Human approval of spec, code and deployment is **not** configurable.
- Optional: the [deployer](docs/deployer.md) runs on a production host and applies the
  repository's release manifest once an approver merged a change to it.
- Never put tokens or secrets in YAML. `gh` and `claude` keep their own credentials.

## Status

Implemented and covered by tests (194, including full-lifecycle integration tests with
crash injection against an in-memory GitHub):
discovery, authorization, all stage decisions, spec and implementation agents, verification
evidence, release-candidate observation, label bootstrap, doctor, lock, logging, and the
production deployer.

Proven against a real monorepo: one feature taken from issue to release candidate through
every human gate, with CI failures, automated review findings and human change requests fed
back to the agents. The shipped config stays observe-only; run mutation polls deliberately
until the systemd timer is enabled.

## Development

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
```

CI runs both on every push and pull request (`.github/workflows/ci.yml`).
