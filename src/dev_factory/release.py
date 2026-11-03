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
        workflow = self.config.candidate_workflow or ""
        runs = [
            r for r in self.gh.workflow_runs(repo, merge_sha)
            if r.path.endswith(f"/{workflow}") or r.path == workflow or r.name == workflow
        ]
        if not runs or any(r.status != "completed" for r in runs):
            return ReleaseStatus(ReleaseState.BUILDING, candidate)
        if not any(r.conclusion == "success" for r in runs):
            return ReleaseStatus(ReleaseState.FAILED, candidate,
                                 f"{workflow} concluded {runs[0].conclusion}: {runs[0].url}")
        env = self.config.production_environment
        for deployment in self.gh.deployments(repo, env):
            if deployment.state == "success" and self.gh.is_ancestor(
                repo, merge_sha, deployment.sha
            ):
                detail = f"deployment {deployment.id} of {deployment.sha[:12]} to {env}"
                return ReleaseStatus(ReleaseState.DEPLOYED, candidate, detail)
        return ReleaseStatus(ReleaseState.READY, candidate)


def make_adapter(config: ReleaseConfig, gh: GitHubCLI) -> ReleaseAdapter:
    if config.adapter == "github-deployments":
        return GitHubDeploymentsAdapter(config, gh)
    return NoneAdapter(config)
