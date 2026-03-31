"""Abstract base class for CI/CD pipeline adapters."""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, NamedTuple


class CIResult(NamedTuple):
    success: bool
    logs: str
    failed_jobs: List[str]


class DeploymentInfo(NamedTuple):
    """Information about where a PR/branch deploys to."""
    environment: str
    base_url: str
    api_version_url: str
    api_config_url: str


class CICDAdapter(ABC):
    """
    Abstract interface for CI/CD pipeline systems (e.g. GitHub Actions, GitLab CI, Jenkins).

    To add a new CI/CD adapter:
    1. Create a new file in brad/adapters/ci_cd/
    2. Subclass CICDAdapter and implement all abstract methods
    3. Register it in brad/config.py so it can be selected via configuration
    See CONTRIBUTING.md for detailed instructions.
    """

    @abstractmethod
    def wait_for_pr(self, pr_number: int, poll_interval: int = 60, timeout: int = 3600) -> CIResult:
        """Wait for all CI workflows to complete for a PR. Returns aggregated result."""
        ...

    @abstractmethod
    def resolve_deployment_env(self, pr_number: Optional[int] = None, branch: Optional[str] = None) -> Optional[DeploymentInfo]:
        """Determine which environment a PR or branch deploys to."""
        ...

    @abstractmethod
    def check_deployment_health(self, pr_number: Optional[int] = None, branch: Optional[str] = None, max_attempts: int = 30, wait_seconds: int = 10) -> Dict:
        """Check if the deployed environment is healthy."""
        ...

    @abstractmethod
    def get_job_logs(self, run_id: int, failed_only: bool = True) -> str:
        """Fetch detailed logs for jobs in a workflow run."""
        ...

    @abstractmethod
    def get_failed_run_ids(self, pr_number: int) -> List[int]:
        """Get IDs of failed workflow runs for a PR."""
        ...
