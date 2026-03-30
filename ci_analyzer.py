import requests
import time
from typing import NamedTuple, List, Optional, Dict
from logging_config import get_logger


class CIResult(NamedTuple):
    success: bool
    logs: str
    failed_jobs: List[str]


class DeploymentInfo(NamedTuple):
    """Information about where a PR/branch deploys to."""
    environment: str          # e.g. "int0", "int1", "int2", "dev"
    base_url: str             # e.g. "https://int0.flaerobotics.ai"
    api_version_url: str      # e.g. "https://int0.flaerobotics.ai/api/version/v1/"
    api_config_url: str       # e.g. "https://int0.flaerobotics.ai/api/config/v1/"


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
    # Environment resolution
    # -------------------------
    @staticmethod
    def resolve_deployment_env(pr_number: Optional[int] = None, branch: Optional[str] = None) -> Optional[DeploymentInfo]:
        """
        Determine which environment a PR or branch deploys to.
        
        Based on bea build.yml:
        - PRs deploy to int{PR_number % 3} (int0, int1, int2)
        - Push to main deploys to dev
        """
        env_name = None
        if pr_number is not None:
            env_name = f"int{pr_number % 3}"
        elif branch == "main":
            env_name = "dev"
        
        if env_name is None:
            return None
        
        base_url = f"https://{env_name}.flaerobotics.ai"
        return DeploymentInfo(
            environment=env_name,
            base_url=base_url,
            api_version_url=f"{base_url}/api/version/v1/",
            api_config_url=f"{base_url}/api/config/v1/",
        )

    # -------------------------
    # Wait for PR pipeline to finish (all workflows)
    # -------------------------
    def wait_for_pr(self, pr_number: int, poll_interval: int = 60, timeout: int = 3600) -> CIResult:
        """
        Waits for ALL workflow runs for the PR to complete.
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
                # Check if ALL runs are completed
                all_completed = all(r["status"] == "completed" for r in runs)
                in_progress = [r for r in runs if r["status"] != "completed"]
                
                for run in runs:
                    workflow_name = run.get("name", "unknown")
                    status = run["status"]
                    conclusion = run.get("conclusion")
                    self.logger.info(f"Workflow '{workflow_name}' status: {status}, conclusion: {conclusion}")
                
                if in_progress:
                    self.logger.info(
                        f"{len(in_progress)} workflow(s) still running: "
                        f"{', '.join(r.get('name', 'unknown') for r in in_progress)}"
                    )
                
                if all_completed:
                    self.logger.info(f"All {len(runs)} workflow runs completed")
                    return self._analyze_all_runs(runs)
            else:
                self.logger.warning(f"No workflow runs found for PR #{pr_number} yet")
            
            if elapsed > timeout:
                self.logger.error(f"CI timeout after {elapsed:.0f}s")
                return CIResult(success=False, logs="CI timeout - workflow did not complete in time", failed_jobs=[])
            
            time.sleep(poll_interval)

    # -------------------------
    # Wait for deployment to complete
    # -------------------------
    def wait_for_deployment(self, pr_number: int, poll_interval: int = 60, timeout: int = 3600) -> CIResult:
        """
        Wait specifically for the deployment workflow to complete for a PR.
        The deploy-int job is part of the build workflow but runs after tests pass.
        """
        self.logger.info(f"Waiting for deployment completion on PR #{pr_number}")
        start_time = time.time()
        
        while True:
            elapsed = time.time() - start_time
            runs = self._get_workflow_runs(pr_number)
            
            if runs:
                # Look for the main build workflow run
                for run in runs:
                    if run["status"] == "completed":
                        # Check if it has deploy jobs
                        result = self._analyze_run(run["id"])
                        deploy_jobs = [j for j in result.logs.split("\n") if "deploy" in j.lower()]
                        if deploy_jobs:
                            self.logger.info(f"Deployment jobs found and completed: {deploy_jobs}")
                            return result
            
            if elapsed > timeout:
                self.logger.error(f"Deployment timeout after {elapsed:.0f}s")
                return CIResult(success=False, logs="Deployment timeout", failed_jobs=[])
            
            time.sleep(poll_interval)

    # -------------------------
    # Check deployment health
    # -------------------------
    def check_deployment_health(
        self,
        pr_number: Optional[int] = None,
        branch: Optional[str] = None,
        max_attempts: int = 30,
        wait_seconds: int = 10
    ) -> Dict:
        """
        Check if the deployed environment is healthy by querying its public endpoints.
        Returns dict with health status and details.
        """
        deploy_info = self.resolve_deployment_env(pr_number=pr_number, branch=branch)
        if not deploy_info:
            return {"healthy": False, "error": "Could not resolve deployment environment"}
        
        self.logger.info(
            f"Checking deployment health for {deploy_info.environment} "
            f"at {deploy_info.base_url}"
        )
        
        for attempt in range(1, max_attempts + 1):
            try:
                version_resp = requests.get(deploy_info.api_version_url, timeout=10)
                config_resp = requests.get(deploy_info.api_config_url, timeout=10)
                
                self.logger.info(
                    f"Attempt {attempt}/{max_attempts}: "
                    f"version={version_resp.status_code}, config={config_resp.status_code}"
                )
                
                if version_resp.status_code == 200 and config_resp.status_code == 200:
                    version_data = {}
                    try:
                        version_data = version_resp.json()
                    except Exception:
                        pass
                    
                    self.logger.info(f"Deployment healthy! Version: {version_data}")
                    return {
                        "healthy": True,
                        "environment": deploy_info.environment,
                        "base_url": deploy_info.base_url,
                        "version": version_data,
                    }
            except requests.exceptions.RequestException as e:
                self.logger.debug(f"Attempt {attempt}/{max_attempts} failed: {e}")
            
            if attempt < max_attempts:
                time.sleep(wait_seconds)
        
        self.logger.error(f"Deployment not healthy after {max_attempts} attempts")
        return {
            "healthy": False,
            "environment": deploy_info.environment,
            "base_url": deploy_info.base_url,
            "error": f"Deployment not ready after {max_attempts * wait_seconds}s",
        }

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
    # Analyze all workflow runs
    # -------------------------
    def _analyze_all_runs(self, runs: List[Dict]) -> CIResult:
        """Analyze all completed workflow runs and return aggregated results."""
        all_logs = []
        all_failed_jobs = []
        
        for run in runs:
            run_id = run["id"]
            workflow_name = run.get("name", "unknown")
            conclusion = run.get("conclusion", "unknown")
            
            all_logs.append(f"=== Workflow: {workflow_name} (conclusion: {conclusion}) ===")
            
            result = self._analyze_run(run_id)
            all_logs.append(result.logs)
            all_failed_jobs.extend(result.failed_jobs)
        
        combined_logs = "\n".join(all_logs)
        overall_success = len(all_failed_jobs) == 0
        
        if overall_success:
            self.logger.info(f"All {len(runs)} workflows passed")
        else:
            self.logger.error(f"Failed jobs across all workflows: {all_failed_jobs}")
        
        return CIResult(success=overall_success, logs=combined_logs, failed_jobs=all_failed_jobs)

    # -------------------------
    # Analyze single workflow run
    # -------------------------
    def _analyze_run(self, run_id: int) -> CIResult:
        """Analyze a completed workflow run and return detailed results."""
        self.logger.info(f"Analyzing workflow run #{run_id}")
        
        try:
            resp = requests.get(
                f"{self.base_url}/actions/runs/{run_id}/jobs",
                headers=self.headers,
                params={"per_page": 100},
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

    # -------------------------
    # Fetch detailed job logs
    # -------------------------
    def get_job_logs(self, run_id: int, failed_only: bool = True) -> str:
        """
        Fetch detailed logs for jobs in a workflow run.
        If failed_only=True, only fetches logs for failed jobs.
        Returns combined log text.
        """
        self.logger.info(f"Fetching job logs for run #{run_id} (failed_only={failed_only})")
        
        try:
            resp = requests.get(
                f"{self.base_url}/actions/runs/{run_id}/jobs",
                headers=self.headers,
                params={"per_page": 100},
                timeout=30,
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
            
            combined_logs = []
            
            for job in jobs:
                job_name = job.get("name", "unknown")
                conclusion = job.get("conclusion", "unknown")
                
                if failed_only and conclusion in ["success", "skipped"]:
                    continue
                
                self.logger.info(f"Fetching logs for job '{job_name}' (id={job['id']})")
                
                try:
                    log_resp = requests.get(
                        f"{self.base_url}/actions/jobs/{job['id']}/logs",
                        headers=self.headers,
                        timeout=60,
                        allow_redirects=True,
                    )
                    
                    if log_resp.status_code == 200:
                        log_text = log_resp.text
                        # Truncate very long logs
                        max_len = 10000
                        if len(log_text) > max_len:
                            log_text = log_text[:max_len] + f"\n... [truncated, {len(log_resp.text)} total chars]"
                        
                        combined_logs.append(f"\n=== Job: {job_name} ({conclusion}) ===\n{log_text}")
                    else:
                        combined_logs.append(
                            f"\n=== Job: {job_name} ({conclusion}) ===\n"
                            f"[Could not fetch logs: HTTP {log_resp.status_code}]"
                        )
                except Exception as e:
                    combined_logs.append(
                        f"\n=== Job: {job_name} ({conclusion}) ===\n"
                        f"[Error fetching logs: {e}]"
                    )
            
            result = "\n".join(combined_logs)
            self.logger.info(f"Fetched logs for {len(combined_logs)} jobs ({len(result)} chars)")
            return result
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to fetch job logs for run #{run_id}: {e}")
            return f"Error fetching job logs: {e}"

    # -------------------------
    # Get failed run IDs
    # -------------------------
    def get_failed_run_ids(self, pr_number: int) -> List[int]:
        """Get IDs of failed workflow runs for a PR."""
        runs = self._get_workflow_runs(pr_number)
        failed = [
            r["id"] for r in runs
            if r.get("status") == "completed" and r.get("conclusion") != "success"
        ]
        self.logger.info(f"Found {len(failed)} failed runs for PR #{pr_number}")
        return failed
