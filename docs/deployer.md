# The deployer

The deployer is the part of Dev Factory that runs on a **production host**. It applies a
repository's production manifest, the way `kubectl apply` applies a Kubernetes manifest.
Changing what runs means merging a change to that file. The factory itself still never
deploys: the deployer applies only what one of the host's approvers merged.

```
repository (main)                                production host
  .dev-factory/config.yaml  release.deploy         deployer (container, Docker socket)
  deploy/production.yml     pinned images   ◄───── every 30 s: read both files (GitHub API)
  release PR changes it ── approver merges         new manifest? merged by an approver?
                                                   docker stack deploy → verify
  Deployments → production  ◄───────────────────── success / failure
  factory closes the issues
```

- **Pull only.** GitHub holds no credentials for the host and never connects to it. The host
  reads two files through the API, never a checkout.
- **Approval on the host.** A new manifest is applied only if the commit that last changed
  it belongs to a PR into the default branch merged by one of the host's `approvers`. A
  direct push, or a PR merged by anyone else, is logged and not applied. GitHub links a merge
  to its PR a few seconds late, so a change with no PR waits 10 passes before it is rejected.
  Approvers, allowed
  images and tokens live in the host's configuration, so no repository change can loosen
  them.
- **Pinned images.** The manifest's `x-release` names the commit its images were built from;
  every image must have a tag and, if the host sets `image_prefixes`, start with one of them.
- **Once per manifest.** A failed or rejected manifest, or an apply interrupted by a crash, is
  not tried again until `deployer retry`. Side effects are never blindly repeated.
- **Recorded.** Each apply is a GitHub deployment of `x-release` to the repository's
  `release.production_environment`. The `github-deployments` release adapter reads it and
  closes the issues the release contains.
- **Drift repaired.** While the applied manifest is current, services that no longer run its
  images (a manual `docker service update`) are put back.

## Runtimes

`swarm` (the only one so far): `docker stack deploy --prune --with-registry-auth` of the
manifest as `release.deploy.stack`. The stack's own `update_config` does the rollout:
health-gated, `start-first` or `stop-first` per service, rolled back on failure. Afterwards
every service must run its pinned image, converged and not rolled back. Services whose spec
did not change are left alone, so a release restarts only what it changes, as long as image
tags change only when an image does (tag by content, not by commit).

## The repository's part

```yaml
# .dev-factory/config.yaml (read from the default branch)
release:
  adapter: github-deployments
  candidate_workflow: build.yml
  production_environment: production
  deploy:
    runtime: swarm
    manifest: deploy/production.yml
    stack: app
    url: https://app.example.com
```

The manifest is a stack file with literal images and a release marker:

```yaml
x-release: 4f1c2e9…   # 40-hex commit the images were built from; `none` = nothing yet
services:
  api:
    image: ghcr.io/acme/app/api:7e640b540bddccbc
    env_file: ./api/.env       # relative to the host's workdir
```

Keep the manifest and `.dev-factory/` protected from agents (`agent.protected_paths`), and let
CI open the PR that updates the images. Relative paths in the manifest resolve against the
host's `workdir`, where env files and other secrets stay.

## The host's part

### Install

On the production host, as root, with Docker installed:

```bash
docker login ghcr.io       # while the deployer image is private
docker run --rm -it -v /var/run/docker.sock:/var/run/docker.sock -v /etc/dev-factory:/etc/dev-factory \
  ghcr.io/abegalinov/dev-factory/deployer:<version> install acme/app --approver maintainer
```

`install` makes the host a single-node swarm (if it is none yet), adds the repository to
`/etc/dev-factory/deployer.yaml`, asks for the two tokens, checks them, and starts the
`dev-factory-deployer` container. Put the env files the manifest needs into the work directory
first: the deployer applies the current manifest on its first pass.

- The GitHub token: fine-grained, for this repository only, with Contents read, Pull requests
  read and Deployments read and write.
- The registry token, to pull private images: for GHCR a classic token with only
  `read:packages` (GHCR does not accept fine-grained tokens); leave it empty for public
  images. It is used for each apply only, in a throwaway Docker config.
- Defaults: work directory `/srv/<name>`, allowed images `ghcr.io/<owner>/<name>/`, registry
  user the first approver; see `install --help`. More approvers: repeat `--approver`.
- A new swarm listens on 127.0.0.1 only. Docker still binds the VXLAN port 4789/udp on every
  interface, so allow only what the host serves (SSH, 80, 443) in the firewall.
- **Upgrade or repair:** the same command with the new version and no repository. It keeps
  the configuration, asks only for missing tokens, checks them and replaces the container;
  a pass that is applying finishes first. A rejected token names the file to delete.

The resulting configuration, to edit by hand (then run `install` again to restart):

```yaml
interval_seconds: 30          # default 30
keep_releases: 5              # images and manifests kept for rollback; default 5
targets:
  - repository: acme/app
    workdir: /srv/app         # env files; the applied manifest and .deployer/ state go here
    approvers: [maintainer]
    token_file: /etc/dev-factory/app.token
    image_prefixes: [ghcr.io/acme/app/]      # optional: empty allows any image
    registry:                                # optional: private images
      server: ghcr.io
      username: maintainer
      password_file: /etc/dev-factory/app.registry.token
    # branch: main            # default: the repository's default branch
```

Files are mode 600. Tokens are read on every pass, so they can be replaced in place.

The container mounts each work directory at the same path: the Docker CLI reads env files
there, while bind mounts in the manifest are resolved by the daemon on the host. The Docker
socket is root on the host. The deployer is the only thing on the host that holds it besides
the operator, and it acts only on approved manifests. Never run it on the factory host or
anywhere agents run.

### Day to day

```bash
docker logs -f dev-factory-deployer                       # JSON log lines
docker exec dev-factory-deployer deployer status                   # each target's last outcome
docker exec dev-factory-deployer deployer retry acme/app           # try the current manifest again
```

`workdir/.deployer/` holds `state.json`, `history` (one line per outcome) and the manifests of
the last releases. The deployer never manages itself, so a broken release cannot take it
down; it changes version only through `install`.

### Rollback

Revert the PR that changed the manifest and merge the revert: the previous images are applied
like any release, and GitHub and the factory see what actually runs. For an emergency by hand,
stop the deployer first (`docker stop dev-factory-deployer`), or it puts the manifest back.

## Publishing

`.github/workflows/deployer-image.yml` publishes `ghcr.io/<factory repository>/deployer:X.Y.Z`
for a tag `deployer-vX.Y.Z`. There is no `latest`.
