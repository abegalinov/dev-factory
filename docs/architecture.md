# How Dev Factory Works

Dev Factory takes a GitHub issue through specification, implementation, verification, review
and release. Claude Code does the creative work. A deterministic Python orchestrator decides
everything else. Humans approve the spec, the code and the deployment.

This is the main reference. The precise rules are in [issue-contract.md](issue-contract.md);
installation and operation are in [operations.md](operations.md).

## The parts

| Part | Role | Keeps state? |
|---|---|---|
| **Humans** (maintainers) | Start work, answer questions, approve spec merge, code merge and deployment | — |
| **GitHub** | Issues, labels, branches, PRs, checks, comments, deployments | **Yes, the only durable store** |
| **Orchestrator** (Python) | Reads GitHub, decides the next step, performs it, verifies the result | No; local files are disposable |
| **Claude Code** | Writes specs and code inside a sandboxed copy of the repository | No |

> **Claude creates but never decides. The orchestrator decides but never creates.**
> Claude cannot move an issue between stages. The orchestrator moves it only after seeing
> evidence on GitHub.

## One poll

A systemd timer runs `dev-factory poll` 3 minutes after the previous run ends. Each run is
a fresh process:

```
lock ─► for each enabled project:
          check lifecycle labels exist
          discover open issues with a factory:* label
          for each issue: observe ─► decide
        ─► (observe-only? print decisions, stop)
        ─► apply cheap actions (label transitions, announcements) for all issues
        ─► run at most one agent action (highest priority, oldest)
        ─► re-observe that issue and apply its follow-up transition
unlock
```

Nothing is remembered between runs. Every run rebuilds its picture from GitHub, so a crash,
a reboot or a duplicate run is harmless: the next run looks again and carries on.

### Code map

| Module | Responsibility |
|---|---|
| `observer.py` | Reads exactly what the issue's stage needs into a `Snapshot` (labels, who applied them, marker, PRs, reviews, checks, release status) |
| `engine.py` | **Pure** `decide(snapshot) → Decision`. All workflow rules live here, without I/O |
| `executor.py` | Performs a decision. Re-reads first, writes the marker, then the label; reuses existing artifacts |
| `factory.py` | The poll loop: discovery, ordering, agent budget, isolation gate, per-issue error containment |
| `state_machine.py` | Stages, allowed transitions, label mapping |
| `marker.py` | The factory-owned status comment with hidden JSON state |
| `github_cli.py` | `gh api` client: pagination, read retries, no mutation retries |
| `claude_cli.py` | Headless Claude invocation and structured result parsing |
| `workspace.py` | Git clone, worktrees, the `.git`-free agent copy, commit and push |
| `release.py` | Release adapters (observe the candidate and production deployments) |
| `deployer.py`, `swarm.py`, `install.py` | The deployer: runs on a production host and applies approved release manifests; `install` sets a host up ([deployer.md](deployer.md)) |
| `labels.py` | Label catalog and idempotent bootstrap plan |
| `runtime.py` | Process lock, JSON logging |

## A feature from start to finish

```
 maintainer adds          factory drafts          human merges           factory implements
 factory:spec  ──► spec ───────────────► spec-review ──────────► implement ─────────────► verify
                    ▲ │                    │                        ▲  │                    │
                    │ │ questions          │ changes requested      │  │ questions          │ checks
                    │ ▼                    ▼                        │  ▼                    │ failed
                    │ needs-input ◄─ ─ ─  spec                      │ needs-input           ▼
                    │  (maintainer                                  └─────────────── implement
                    │   answers → resume)                                          (bounded attempts)
                    │                                                                     │ checks
 human closes /     ◄── release ◄──────────────── human merges ◄──────────── review ◄─────┘ passed
 deployment seen        (candidate announced;                       (changes requested → implement;
                         factory never deploys)                      new commits → verify)
```

1. **Setup (once per repository):** `labels bootstrap` creates the labels. `doctor` checks
   identity, permissions, branch protection and labels.
2. **Someone files an issue.** The factory ignores it.
3. **A maintainer adds `factory:spec`.** The observer checks the issue's events: whoever applied
   the label must have Write access. Otherwise the issue is skipped.
4. **`spec`:**
   - With `agent.triage`, a read-only run first posts its understanding, scope, assumptions,
     findings in the code, and questions with suggested answers. Questions go to `needs-input`
     until a maintainer replies; then it triages again, up to 3 rounds.
   - The orchestrator records the attempt, prepares a workspace on `factory/spec/<n>`, and runs
     Claude with the issue and maintainer comments, fenced off as untrusted data.
   - Claude may change only `specs/features/issue-<n>.md`.
   - The orchestrator commits, pushes and opens the spec PR. The follow-up check then moves the
     issue to `spec-review`.
   - If Claude needs answers, it returns questions instead. They are posted and the issue goes to
     `needs-input` until a maintainer replies.
5. **`spec-review` (human gate):** a human either requests changes (back to `spec`, where Claude
   revises the same PR using the review text) or merges. The merge counts only if it was not
   made by the factory account.
6. **`implement`:**
   - Claude implements against the approved spec in a fresh copy of the default branch, or of
     the existing code branch when reworking.
   - Changes to `.github/**`, the spec directory or other protected paths block the issue.
   - The orchestrator pushes `factory/impl/<n>` and opens the code PR. The issue moves to `verify`.
7. **`verify`:**
   - The orchestrator reads CI results for the PR's exact head commit. It never runs PR code itself.
   - Green: it posts an evidence table on the PR and moves to `review`.
   - Red: it sends the failure back to `implement` until the attempt limit, then `blocked`.
   - Checks that never arrive time out to `blocked`.
8. **`review` (human gate):** a human can request changes (→ `implement`), push their own
   commits (→ `verify` again) or merge (→ `release`, merge commit recorded).
9. **`release` (human gate):** the release adapter reports when the candidate is ready, for
   example when the image workflow succeeded on the merge commit. The factory announces it once.
   A human approves and performs the deployment. With the `github-deployments` adapter, the
   factory closes the issue when it sees a successful production deployment that contains the
   merge commit.

Every transition is visible as a label change plus a one-line comment explaining why.

## State without a database

| Where | What |
|---|---|
| **Label** | The current stage (authoritative) |
| **Marker comment** | Hidden JSON with PR numbers, attempt counter, rework base, resume stage, merge SHA. Edited in place; only trusted from the factory account |
| **Branch names** | `factory/spec/<n>`, `factory/impl/<n>` locate existing work |
| **PR reviews / checks / deployments** | Evidence for every gate |

**Crash example.** The orchestrator pushes `factory/impl/7`, then dies before opening the PR.
On the next poll the issue still says `implement` and there is no PR. Before running Claude,
the executor sees that the branch exists and is ahead of `main`, so it opens the PR. Claude is
not run again.

The general rule: **read before acting, reuse what exists, change the label last, and
replace all labels in one request.**

## Safety model

| Threat | Control |
|---|---|
| A stranger triggers an AI agent with push access | Only the actor of the latest lifecycle `labeled` event counts, and they need Write access |
| Prompt injection through comments or reviews | Only text from users with write access reaches Claude, and it is fenced as untrusted data |
| Forged factory state | Markers are trusted only from the factory account |
| Factory merges its own work | It never calls merge APIs, and merges by the factory account → `blocked` |
| Skipping a gate by relabelling | Each stage re-checks its own evidence |
| Agent leaks its own token | Every forwarded value of 8+ characters is replaced with `[redacted]` in the agent's result (summary, questions, commit message, review findings) before it reaches GitHub, and a changed file containing one blocks the issue without pushing |
| Agent steals credentials | Claude runs in a Docker container (`agent_image`, set only in the host entry) with no capabilities, a read-only root, the host uid, and only the task copy mounted. Only `claude.env_passthrough` variables are forwarded, by name. Mutation mode refuses agent runs without an image |
| Agent or test exhausts the host | Per-container memory/CPU/pid caps with swap disabled and OOM priority, a `dev-factory.slice` budget, and heavy actions deferred while the host is busy (see [operations.md](operations.md#5-host-placement-and-resources)) |
| Agent tampers with git (hooks, config) | The agent edits a copy without `.git`. Orchestrator git runs with hooks disabled and never force-pushes |
| Repository-shipped Claude settings and hooks | `--setting-sources user`, `--strict-mcp-config`, `--permission-mode dontAsk` with an explicit tool allowlist |
| Agent edits CI to exfiltrate secrets | Protected paths (`.github/**` by default) block the issue |
| Runaway loops or cost | Bounded attempts, check timeout, `max_turns`, `max_budget_usd`, one agent run per poll |
| Production credentials in PR CI | Owner's responsibility: production secrets live in a protected environment with required reviewers. The factory never deploys |
| Overlapping runs | One `flock` per account (`$XDG_STATE_HOME/dev-factory/factory.lock`, default `~/.local/state/…`), whatever config or `runtime_root` a run uses, plus systemd `OnUnitInactiveSec` |
| Humans act during a long agent run | The issue and PR are read again after the run; if the issue was closed or relabelled, or the PR closed or pushed to, the work is discarded and the next poll decides again |

**Known limitations:** containers need network egress (Anthropic API, package registries),
so code in a container can reach the internet, though it holds no credentials besides the
agent token. The factory host's `docker` group is root-equivalent, which is one more reason
to keep the factory off production hosts.

## What is deliberately not here

- No local database, queue or workflow engine. GitHub plus deterministic rules is enough for
  one orchestrator at concurrency 1.
- No automatic deployment, merge or approval, ever. The optional deployer applies only a
  manifest that one of the production host's approvers merged.
- No generic plugin system. Project differences go into project YAML and a small release-adapter
  interface. More will be added only when a second project needs it.
