"""Release adapters: report the release-candidate and deployment status for a merge commit.

Adapters only observe. The factory never builds production artifacts with privileged
credentials and never deploys; the project's own CI and a human-approved deployment do.
"""

from typing import Protocol

from dev_factory.config import ReleaseConfig
from dev_factory.engine import ReleaseState, ReleaseStatus
from dev_factory.github_cli import GitHubCLI


class ReleaseAdapter(Protocol):
    def status(self, repo: str, merge_sha: str) -> ReleaseStatus: ...


class NoneAdapter:
    """The merge commit is the candidate. Humans deploy and close the issue."""

    def __init__(self, config: ReleaseConfig):
        self.config = config

    def status(self, repo: str, merge_sha: str) -> ReleaseStatus:
        candidate = self.config.candidate_ref_template.format(sha=merge_sha)
        return ReleaseStatus(ReleaseState.READY, candidate)


class GitHubDeploymentsAdapter:
    """A workflow on the merge commit builds the candidate (e.g. a GHCR image).

    Deployment is observed through GitHub Deployments in the production environment,
    which the repository owner protects with required reviewers.
    """

    def __init__(self, config: ReleaseConfig, gh: GitHubCLI):
        self.config = config
        self.gh = gh

    def status(self, repo: str, merge_sha: str) -> ReleaseStatus:
        candidate = self.config.candidate_ref_template.format(sha=merge_sha)
        # A deployment that contains the merge commit settles it, whatever happened to the
        # build of that commit itself (a later commit's build may have shipped it).
        env = self.config.production_environment
        for deployment in self.gh.deployments(repo, env):
            if deployment.state == "success" and self.gh.is_ancestor(
                repo, merge_sha, deployment.sha
            ):
                detail = f"deployment {deployment.id} of {deployment.sha[:12]} to {env}"
                return ReleaseStatus(ReleaseState.DEPLOYED, candidate, detail)
        workflow = self.config.candidate_workflow or ""
        runs = [
            r for r in self.gh.workflow_runs(repo, merge_sha)
            if r.path.endswith(f"/{workflow}") or r.path == workflow or r.name == workflow
        ]
        if not runs or any(r.status != "completed" for r in runs):
            return ReleaseStatus(ReleaseState.BUILDING, candidate)
        if any(r.conclusion == "success" for r in runs):
            return ReleaseStatus(ReleaseState.READY, candidate)
        if all(r.conclusion == "cancelled" for r in runs):
            # Superseded: a newer commit queued in the same concurrency group cancelled it.
            # That commit's build carries this one, and its deployment closes the issue.
            return ReleaseStatus(ReleaseState.BUILDING, candidate,
                                 "its build was superseded by a later commit")
        failed = next(r for r in runs if r.conclusion != "cancelled")
        return ReleaseStatus(ReleaseState.FAILED, candidate,
                             f"{workflow} concluded {failed.conclusion}: {failed.url}")

def make_adapter(config: ReleaseConfig, gh: GitHubCLI) -> ReleaseAdapter:
    if config.adapter == "github-deployments":
        return GitHubDeploymentsAdapter(config, gh)
    return NoneAdapter(config)
