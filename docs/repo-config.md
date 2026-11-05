# Project configuration in the repository (`.dev-factory/`)

The factory is generic. What it knows about a project comes from that project's repository,
from the `.dev-factory/` directory on the **default branch**. The factory host keeps only what
the operator must control: which repositories, which container images, credentials and limits.

A repository is one project. Monorepos describe their parts as `components`.

## One poll

```
systemd timer → dev-factory poll  (one at a time, file lock)
│
├─ 1. Load the host config (trusted)
│     factory.yaml: credentials, budgets, Docker limits, attempt caps
│     projects/*.yaml: one entry per repository: id, repository, enabled, images
│
├─ 2. For each enabled repository (read-only)
│     a. Look up the default branch and its head sha S
│     b. Read .dev-factory/ at S through the GitHub API: config.yaml, instructions.md,
│        templates/*. Never from a PR branch or an agent worktree.
│     c. Validate and merge with the host entry. Invalid or missing: report, skip the repo.
│     d. Check the lifecycle labels exist
│     e. Observe the labelled issues → decide one action per issue
│
├─ 3. Act: cheap actions everywhere; agent runs within the per-poll budget, round-robin
│     across repositories within each priority. Each run uses the config read at S.
│
└─ 4. Report: per repository, the config sha used, decisions, outcomes, errors
```

## Who controls what

| Setting | Where | Why |
|---|---|---|
| Spec directory, instructions, components, review on/off | repository | Project knowledge |
| Required checks, verification commands, release adapter | repository | The project's CI and deploy |
| Protected paths | repository, **added to** the baseline | A repository can protect more, never less |
| Attempt limits, review rounds, checks timeout | repository, **at most** the host's | Can tighten, never loosen |
| Lifecycle label names | repository | Optional; defaults to the standard set |
| Repository list, enabled | host `projects/*.yaml` | The operator decides where the factory acts |
| Agent and verification images | host `projects/*.yaml` | The agent container receives the Claude token; a repository-chosen image could exfiltrate it |
| Tools, credentials, budgets, Docker limits | host `factory.yaml` | Operator policy |
| Base prompts (`prompts/*.md`) | factory | They carry the safety rules; repository text is added as clearly marked project data |

Baseline protected paths, always enforced: `.github/**`, `.dev-factory/**`, and the spec
directory (for implementation runs). An agent change to them blocks the issue. Changes to
`.dev-factory/` therefore reach the factory only through a human-merged PR.

## Repository layout

```
.dev-factory/
  config.yaml          # required
  instructions.md      # optional: project notes appended to every agent prompt
  templates/
    spec.md            # optional: skeleton the spec agent follows
    review.md          # optional: project checklist added to the review prompt
    pr-body.md         # optional: code PR description; {{issue}}, {{spec}}, {{summary}}
```

`config.yaml` (all sections optional except `version`):

```yaml
version: 1
specification:
  directory: docs/specs
labels: {}                 # stage -> label name overrides
agent:
  review: true
  triage: true             # discuss the issue in its comments before the spec
  protected_paths: ["docker-compose*.yml"]
components:
  - name: mobile
    paths: ["apps/mobile/**"]
    instructions: "…"
verification:
  mode: github-checks      # or container (image from the host entry)
  required_checks: [unit, e2e]
  commands: []             # container mode only
release:
  adapter: none
  deploy: null             # or the deployer's settings (docs/deployer.md)
policy:
  max_implementation_attempts: 3
```

A complete, commented example lives in `examples/dev-factory/`. Validate a local copy before
committing it: `dev-factory config check path/to/.dev-factory`.

## Host entry (`config/projects/<id>.yaml`)

```yaml
project_id: example-project
repository: your-org/your-repo
enabled: true
agent_image: dev-factory-agent-node20:2.1.311
# verification_image: …    # only for verification.mode: container
# default_branch: main     # default: the repository's default branch
```

## Traceability

The config sha is recorded in the issue's state marker on every write, in each agent commit
(`Dev-Factory-Config: <sha>` trailer), and in the poll report.

## Failure modes

- No `.dev-factory/config.yaml` on the default branch, or invalid YAML/schema: the repository
  is skipped and the poll reports the error. Nothing falls back to defaults.
- A config change mid-issue applies from the next poll. Already-pushed work is unaffected.
