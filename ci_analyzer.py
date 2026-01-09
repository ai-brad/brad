import requests
import time
from typing import NamedTuple, List, Optional
from logging_config import get_logger


class CIResult(NamedTuple):
    success: bool
    logs: str
    failed_jobs: List[str]


class CIAnalyzer:
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.token = cfg.github_token
        self.repo = cfg.github_repo
        self.base_url = f"https://api.github.com/repos/{self.repo}"
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
        }
        self.logger.info(f"Initialized CI analyzer for {self.repo}")

    # -------------------------
    # Wait for PR pipeline to finish
    # -------------------------
    def wait_for_pr(self, pr_number: int, poll_interval: int = 60, timeout: int = 3600) -> CIResult:
        """
        Waits for the latest workflow run for the PR to complete
        poll_interval: seconds between checks
        timeout: max seconds to wait
        """
        self.logger.info(f"Waiting for CI completion on PR #{pr_number} (timeout: {timeout}s, poll: {poll_interval}s)")
        start_time = time.time()
        iteration = 0
        
        while True:
            iteration += 1
            elapsed = time.time() - start_time
            
            self.logger.debug(f"Poll iteration {iteration}, elapsed: {elapsed:.0f}s")
            
            runs = self._get_workflow_runs(pr_number)
            if runs:
                latest = runs[0]
                status = latest["status"]  # queued, in_progress, completed
                conclusion = latest.get("conclusion")  # success, failure, cancelled, None
                workflow_name = latest.get("name", "unknown")
                
                self.logger.info(f"Workflow '{workflow_name}' status: {status}, conclusion: {conclusion}")
                
                if status == "completed":
                    self.logger.info(f"CI completed with conclusion: {conclusion}")
                    return self._analyze_run(latest["id"])
            else:
                self.logger.warning(f"No workflow runs found for PR #{pr_number} yet")
            
            if elapsed > timeout:
                self.logger.error(f"CI timeout after {elapsed:.0f}s")
                return CIResult(success=False, logs="CI timeout - workflow did not complete in time", failed_jobs=[])
            
            time.sleep(poll_interval)

    # -------------------------
    # Get workflow runs for a PR
    # -------------------------
    def _get_workflow_runs(self, pr_number: int):
        """Get workflow runs for a PR by querying the latest commit SHA."""
        try:
            # Get commits in the PR
            resp = requests.get(
                f"{self.base_url}/pulls/{pr_number}/commits",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            commits = resp.json()
            
            if not commits:
                self.logger.warning(f"No commits found for PR #{pr_number}")
                return []

            latest_sha = commits[-1]["sha"]
            self.logger.debug(f"Latest commit SHA for PR #{pr_number}: {latest_sha}")

            # Query workflow runs by commit SHA (not branch!)
            resp2 = requests.get(
                f"{self.base_url}/actions/runs",
                headers=self.headers,
                params={"head_sha": latest_sha, "per_page": 10},
                timeout=30,
            )
            resp2.raise_for_status()
            data = resp2.json()
            runs = data.get("workflow_runs", [])
            
            self.logger.debug(f"Found {len(runs)} workflow runs for SHA {latest_sha}")
            return runs
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to get workflow runs for PR #{pr_number}: {e}")
            return []

    # -------------------------
    # Analyze workflow run
    # -------------------------
    def _analyze_run(self, run_id: int) -> CIResult:
        """Analyze a completed workflow run and return detailed results."""
        self.logger.info(f"Analyzing workflow run #{run_id}")
        
        try:
            resp = requests.get(
                f"{self.base_url}/actions/runs/{run_id}/jobs",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            jobs = data.get("jobs", [])
            failed_jobs = [job["name"] for job in jobs if job.get("conclusion") not in ["success", "skipped"]]

            # Build detailed logs
            log_lines = []
            for job in jobs:
                name = job.get("name", "unknown")
                conclusion = job.get("conclusion", "unknown")
                log_lines.append(f"{name}: {conclusion}")
                
                if conclusion not in ["success", "skipped"]:
                    self.logger.warning(f"Job '{name}' failed with conclusion: {conclusion}")
            
            logs = "\n".join(log_lines)
            success = len(failed_jobs) == 0
            
            if success:
                self.logger.info(f"All jobs passed in run #{run_id}")
            else:
                self.logger.error(f"Run #{run_id} has {len(failed_jobs)} failed jobs: {failed_jobs}")
            
            return CIResult(success=success, logs=logs, failed_jobs=failed_jobs)
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to analyze run #{run_id}: {e}")
            return CIResult(success=False, logs=f"Error analyzing run: {e}", failed_jobs=[])
