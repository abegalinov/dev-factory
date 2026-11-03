# GitHub Issue Contract

The contract between humans, the orchestrator, and GitHub. GitHub is the only durable
store: if a fact cannot be recovered from GitHub, the orchestrator does not rely on it.

The rules below are implemented in `src/dev_factory/engine.py` (decisions) and
`src/dev_factory/executor.py` (side effects), and exercised end to end in
`tests/test_integration.py`.

## Labels

### Lifecycle (exactly one per open factory issue)

| Label                 | Next step by | Meaning                                                  |
|-----------------------|--------------|----------------------------------------------------------|
| `factory:spec`        | factory      | Draft or revise the specification                        |
| `factory:spec-review` | human        | Spec PR open; waiting for merge or change request        |
| `factory:implement`   | factory      | Implement the approved spec on the code branch           |
| `factory:verify`      | factory      | Collect check results for the code PR head commit        |
| `factory:review`      | human        | Code PR verified; waiting for merge or change request    |
| `factory:release`     | human        | Release candidate ready; waiting for approved deployment |
| `factory:needs-input` | human        | Factory asked a question; waiting for a maintainer       |
| `factory:blocked`     | human        | Factory cannot proceed safely; needs intervention        |

`needs-input` and `blocked` are *exception* states. They replace the stage label. The stage to
return to is recorded in the state marker.

Label names can be overridden per project (`workflow.labels`). The defaults are shown here.

### Metadata (optional, never drive transitions)

`type:bug`, `type:feature`, `type:chore`; `priority:high`, `priority:medium`, `priority:low`.
When more than one issue needs an agent run, the highest priority goes first, then the oldest update.

### Anomalies

| Situation                           | Handling                                         |
|-------------------------------------|--------------------------------------------------|
| No lifecycle label                  | Not a factory issue; never listed                |
| More than one lifecycle label       | → `blocked` (only reported in observe-only mode) |
| Lifecycle label on a pull request   | Ignored                                          |
| Closed issue                        | Ignored. Closing is the human "stop" signal      |
| Lifecycle labels missing in repo    | Whole project skipped and reported until bootstrapped |

## Intake: how an issue enters the factory

1. **One-time per repository:** an operator runs `dev-factory labels bootstrap --project <id>`
   (dry run), then again with `--apply`. It creates the 8 lifecycle and 6 metadata labels.
   Existing labels are never modified; differences are reported.
2. **Anyone files an issue** in the normal way. Without a lifecycle label the factory ignores it.
   Issue templates may apply `type:*` labels but must **never** apply `factory:*` labels.
3. **A maintainer (Write or higher) adds `factory:spec`.** This is the only start signal.

The factory never creates issues.

## Factory identity

The repository has another owner. The factory runs as a **collaborator** account. Its login
is discovered at runtime (`gh api user`) and never configured by hand.

- **Required role: Write.** Needed to push `factory/*` branches, open PRs, create labels and edit
  its own comments. `doctor` warns about Admin, since it is more than needed.
- **Prefer a dedicated machine account.** Everything the factory identity does counts as a
  factory action (marker trust, authorization), so a human sharing it is indistinguishable
  from the factory.
- **Outside the factory's control:** branch protection, environments, secrets, repository
  settings. `doctor` reports what a collaborator can see, such as whether the default branch is protected.

### Human gates are enforced by the orchestrator

A Write collaborator could merge PRs if branch protection were missing. Therefore:

- The orchestrator **never** calls merge, approve, or deploy APIs.
- A spec or code PR counts as approved only if it is merged **and** `merged_by` is not the factory
  identity. A PR merged by the factory identity → `blocked`.
- A human cannot skip a gate by relabelling: each stage re-checks its own evidence
  (e.g. `factory:release` without a merged code PR → `blocked`).

## Authorization

A label alone does not authorize anything. Before acting on an issue, the orchestrator checks
that the **most recent `labeled` event for the current lifecycle label** was made by the
factory identity or by a user whose repository permission is `write` or `admin` (Maintain counts
as write). Otherwise the issue is skipped and reported, and nothing is written.

Comments are filtered the same way: only comments by users with write access are given to Claude
or count as answers. This applies to issue comments, PR reviews and inline review comments.
Even authorized text is untrusted data. It is fenced in the prompt and never interpreted as an
orchestrator command.

## State marker

The factory keeps one comment per issue, authored by the factory identity and edited in place:

```
<!-- dev-factory:state {"v":1,"stage":"implement","attempt":1,"code_pr":12,...} -->
**Dev Factory status** (table for humans)
```

| Field              | Meaning                                                                 |
|--------------------|-------------------------------------------------------------------------|
| `stage`            | Stage the factory last wrote                                            |
| `attempt`          | Agent attempts in the current spec/implement cycle                      |
| `rework_base`      | PR head SHA when rework was requested; rework is due while head == this |
| `last_failure`     | Failure summary fed into the next attempt                               |
| `resume_stage`     | Where `needs-input` returns to (`spec` or `implement`)                  |
| `asked_at`         | Time of the needs-input question; only later answers count              |
| `spec_pr`, `code_pr` | PR numbers (also rediscoverable from branch names)                    |
| `verified_sha`     | Code PR head that passed verification                                   |
| `merge_sha`        | Merge commit of the code PR (the release candidate source)              |
| `candidate_posted` | Release candidate announcement done                                     |
| `updated_at`       | Last write time, used to tell human relabels from interrupted transitions |

Rules:

- **The label decides the stage.** The marker only holds pointers and counters.
- **Only markers written by the factory identity are trusted.** Anything else is ignored.
- **A missing marker is recoverable.** PRs are found from branch names, and counters start at zero.
- **Label and marker disagree:**
  - If the label was applied by a human **after** the marker was last written, it is a
    **human relabel**. Counters, rework and resume state are reset; PR pointers are kept.
  - Otherwise it is an **interrupted factory transition**. The marker is kept and the issue is
    decided again from the label.

## Deterministic names (idempotency)

| Artifact     | Name                                             |
|--------------|--------------------------------------------------|
| Spec branch  | `factory/spec/<issue>`                           |
| Spec file    | `<specification.directory>/issue-<issue>.md`     |
| Code branch  | `factory/impl/<issue>`                           |
| PRs          | Found by head branch; body contains `Refs #<issue>` |
| Evidence     | PR comment tagged `<!-- dev-factory:evidence <sha> -->` |

The spec file is keyed only by issue number, so renaming the issue cannot orphan it. PRs use
`Refs`, never `Closes`, so merging the code PR does not close the issue before release.

## Rework and attempts

- An agent attempt is counted (marker written) **before** Claude is invoked, so a crash still
  counts. Limits: `policy.max_spec_attempts` and `policy.max_implementation_attempts`.
- **Changes requested** means the latest decisive review (approved, changes requested or
  dismissed) is `CHANGES_REQUESTED` **on the current head commit**. A new push supersedes it.
- `verify → implement` on failed checks keeps the attempt count, so CI loops are bounded.
- Human-driven rework (changes requested at `spec-review` or `review`) resets the count, because
  human reviews are already bounded by the human.
- Agent commits carry the agent's own message: what changed in the subject line and why in the
  body (the problem, the approach, the failure or review it addresses), and the git trailers
  `Refs: #<n>` and `Dev-Factory-Attempt: <k>` (joined to the agent's own trailers, if any).
  Agents run with Claude Code's attribution turned off, and the factory drops any
  `Co-Authored-By` line; the trailers already say where a commit came from.

## Automated review (`agent.review: true`)

Before each human gate, a separate Claude run reviews the PR: the spec PR before `spec-review`,
the code PR after its checks pass and before `review`. The reviewer is **read-only**
(Read/Glob/Grep; repositories cannot change any agent's tool list) and
sees the issue, maintainer comments, the diff and the checked-out PR head.

- The result is posted as a `COMMENT` review on the PR head commit, tagged
  `<!-- dev-factory:ai-review <sha> {"blocking": n, "suggestions": m} -->`. The tag on the
  current head is how the next poll knows the review is done; a new push needs a new review.
- **Blocking** findings go back to the authoring agent as review feedback on that commit
  (spec: revise; code: `verify → implement`). This uses an agent attempt.
- **Advisory, bounded:** at most `policy.max_review_rounds` (default 2) reviews per cycle. When
  rounds or attempts are exhausted, or the reviewer fails, the PR goes to the human gate anyway,
  and the transition comment says what the review found. The counter resets when a human
  requests changes.
- The review never approves, requests changes or merges: GitHub forbids a PR's author from
  approving it, and the human gates stay human.

## Stages

### `factory:spec`
- **Run agent** when there is no spec PR, or rework is due on it. Claude may change **only** the spec file.
- **Review** (with `agent.review`): the open spec PR head is reviewed first; blocking findings
  start a revision (see Automated review).
- **→ `spec-review`:** spec PR open with no rework due (or already merged), and reviewed.
- **→ `needs-input`:** Claude returned questions. They are posted as an issue comment.
- **→ `blocked`:** attempts exhausted; spec PR closed without merging; Claude changed other files.

### `factory:spec-review` (human gate)
- **→ `implement`:** spec PR merged by a human, and the spec file is on the default branch.
- **→ `spec`:** changes requested on the spec PR head.
- **→ `blocked`:** spec PR missing, closed without merging, merged by the factory, or merged
  without the spec file reaching the default branch.

### `factory:implement`
- **Precondition:** the approved spec is on the default branch, otherwise → `blocked`.
- **Run agent** when there is no code PR, or rework is due. The worktree continues the existing code branch.
- **Protected paths** (baseline `.github/**` and `.dev-factory/**`, plus the spec directory and
  the repository's `agent.protected_paths`): any change to them → `blocked`. CI definitions can
  expose secrets and the factory config must change only through human PRs.
- **→ `verify`:** code PR open with no rework due (or already merged).
- **→ `needs-input`:** Claude returned questions.
- **→ `blocked`:** attempts exhausted; code PR closed without merging; protected path changed.

### `factory:verify`
- Reads check runs and commit statuses for the code PR head. **PR code never runs in the
  orchestrator.** With `verification.required_checks` set, only those checks count. Otherwise
  every reported check counts and at least one is required.
- With `verification.mode: container` (for repositories without PR CI), the factory first runs
  `verification.commands` on the PR head in the locked-down verification container. It posts the
  commit status `dev-factory/verify` (pending first, then the result) and an output comment on the PR.
  That status is then required like any other check. A run killed by the memory limit leaves the
  status pending, so the checks timeout escalates it without consuming an attempt.
- **Review** (with `agent.review`): once all checks pass, the head is reviewed; blocking findings
  → `implement` with the findings as feedback.
- **→ `review`:** all considered checks passed (and, with `agent.review`, the automated review
  is done). A verification-evidence comment is posted on the PR.
- **→ `implement`:** a check failed and attempts remain. The failure is fed to the next attempt.
  For a failed GitHub Actions job, that includes an excerpt of the job log: the failing step,
  with each group printed inside it (one per failed test, a device log) cut to its first lines.
- **→ `blocked`:** a check failed with no attempts left; checks missing or pending
  `policy.checks_timeout_minutes` after the head commit.

### `factory:review` (human gate)
- **→ `release`:** code PR merged by a human. The merge commit is recorded.
- **→ `verify`:** head changed since verification (e.g. a human pushed commits).
- **→ `implement`:** changes requested on the head.
- **→ `blocked`:** code PR missing, closed without merging, or merged by the factory.

### `factory:release` (human gate)
The release adapter only **observes**. The factory never builds production artifacts with
privileged credentials and never deploys.

| Adapter              | Candidate                                    | Done when                         |
|----------------------|----------------------------------------------|-----------------------------------|
| `none`               | The merge commit                             | A human deploys and closes the issue |
| `github-deployments` | `candidate_workflow` succeeded on the merge commit | A successful deployment to `production_environment` contains the merge commit; the factory then closes the issue |

- Candidate ready → announced once on the issue.
- Candidate workflow failed → `blocked`.
- The deployment approval gate belongs to the repository owner, e.g. a GitHub Environment with
  required reviewers.

### `factory:needs-input`
- **→ `resume_stage`:** a maintainer (write+) commented after the question. Rework is then due,
  so the agent runs again with the answer.
- **→ `blocked`:** the marker has no valid `resume_stage`.
- A maintainer may also relabel directly. This is treated as a human relabel.

### `factory:blocked`
- No automatic exit. A maintainer fixes the cause and applies a work-stage label. The
  relabel resets counters, and the stage's own evidence is re-checked before anything happens.

## How a transition is written

1. Re-read the issue. If it is closed or its lifecycle labels changed since observation, skip
   and leave it to the next poll.
2. Post artifacts that justify the move, e.g. the verification evidence (idempotent by tag).
3. Write the marker (`stage` = target).
4. Replace the issue's labels in **one** request (`PUT /issues/{n}/labels`), keeping
   non-lifecycle labels. This avoids both zero-label and two-label intermediate states.
5. Post a one-line comment: `` `from` → `to`: reason``.

## Recovery rules

The process may die between any two calls. Each case below is covered by a test.

| Crash point                         | Next poll                                                 |
|-------------------------------------|-----------------------------------------------------------|
| After push, before PR creation      | Branch ahead of default with no PR → PR opened, agent not rerun |
| After PR creation, before marker update | Open PR found by branch → transition, no new marker comment |
| After marker write, before label change | Interrupted transition detected → same transition completed |
| During a poll (lock held)           | Kernel releases the `flock`; the next run starts normally |

General rules: read before every action; find artifacts by deterministic name before
creating; change the label last; mutations are never retried blindly; retries are bounded
and end in `blocked` with a reason; local workspaces can always be deleted.
