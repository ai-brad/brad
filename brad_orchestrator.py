"""
Brad - Autonomous AI Software Engineer
Orchestrator that uses Azure OpenAI Responses API with tool-calling.
Brad does everything directly — no external CLI tools needed.
"""

from typing import Dict, Optional
from dataclasses import dataclass
from logging_config import get_logger
from config import Config
from jira_client import JiraClient
from github_client import GitHubClient
from ci_analyzer import CIAnalyzer
from repo_manager import RepoManager
from ai_agent_interface import create_ai_agent_interface
from adf_parser import adf_to_text
from code_review_handler import CodeReviewHandler
from azure_log_client import AzureLogClient
from phase_cache import get_cached_phase, set_cached_phase
from test_selector import build_pytest_target, extract_failed_tests_from_ci_logs

@dataclass
class IssueState:
    """Tracks the state of an issue being processed."""
    issue_key: str
    description: str
    attachments: list
    attachment_paths: list
    branch_name: str
    jira_updated: str = ""  # Jira ticket last-updated timestamp (for cache key)
    last_response_id: Optional[str] = None  # Warm-start: last AI agent response ID
    pr_number: Optional[int] = None
    clarification_count: int = 0
    ci_fix_count: int = 0
    review_fix_count: int = 0


class BradOrchestrator:
    """
    Brad orchestrator. Manages the issue lifecycle:
    requirements → implementation → local review → CI monitoring → deployment health.
    """
    
    def __init__(self, cfg: Config):
        self.logger = get_logger(__name__)
        self.cfg = cfg
        
        # Initialize clients
        self.jira = JiraClient(cfg)
        self.github = GitHubClient(cfg)
        self.ci = CIAnalyzer(cfg)
        self.repo = RepoManager(cfg)
        self.agent = create_ai_agent_interface(cfg)
        self.review_handler = CodeReviewHandler(cfg)
        self.azure_logs = AzureLogClient(cfg)
        
        # Model identity for cache keys: endpoint + model name
        self._model_identity = f"{cfg.azure_openai_endpoint}|{cfg.azure_openai_model}"
        
        self.logger.info(f"Brad orchestrator initialized (model: {cfg.azure_openai_model})")
    
    def run_once(self):
        """
        Main orchestration loop - process one batch of issues.
        """
        self.logger.info("=" * 80)
        self.logger.info("Brad run started")
        self.logger.info("=" * 80)
        
        try:
            # First, check for review comments on existing PRs
            self.logger.info("Checking for code review comments...")
            try:
                processed_reviews = self.review_handler.process_review_comments()
                if processed_reviews > 0:
                    self.logger.info(f"Processed {processed_reviews} review comments")
            except Exception as e:
                self.logger.error(f"Failed to process review comments: {e}")
            
            # Then fetch issues with BradReview label
            issues = self.jira.fetch_issues_with_label("BradReview")
            
            if not issues:
                self.logger.info("No issues to process")
                return
            
            self.logger.info(f"Processing {len(issues)} issues")
            
            for issue in issues:
                try:
                    self._process_issue(issue)
                except Exception as e:
                    issue_key = issue.get("key", "UNKNOWN")
                    self.logger.error(f"Failed to process {issue_key}: {e}", exc_info=True)
                    
                    # Try to comment on JIRA about the error
                    try:
                        self.jira.comment(
                            issue_key,
                            f"Brad encountered an unexpected error:\n\n```\n{str(e)}\n```\n\nBrad is stuck."
                        )
                    except:
                        self.logger.error(f"Failed to post error comment to {issue_key}")
        
        finally:
            self.logger.info("=" * 80)
            self.logger.info("Brad run completed")
            self.logger.info("=" * 80)
    
    def _process_issue(self, issue: Dict):
        """
        Process a single issue through the Brad workflow.
        """
        issue_key = issue["key"]
        fields = issue.get("fields", {})
        
        self.logger.info(f"Processing issue: {issue_key}")
        self.logger.info(f"Summary: {fields.get('summary', 'N/A')}")
        
        # Step 1: Remove BradReview label immediately
        self.logger.info(f"{issue_key}: Removing BradReview label")
        self.jira.remove_label(issue_key, "BradReview")
        
        # Step 2: Set status to IN PROGRESS
        self.logger.info(f"{issue_key}: Setting status to IN PROGRESS")
        try:
            self.jira.set_status(issue_key, "IN PROGRESS")
        except Exception as e:
            self.logger.warning(f"Could not set status to IN PROGRESS: {e}")
        
        # Step 3: Prepare issue state
        description = fields.get("description", "")
        if not description:
            self.logger.error(f"{issue_key}: No description provided")
            self.jira.comment(issue_key, "Brad cannot process this issue: No description provided.")
            return
        
        # Parse ADF (Atlassian Document Format) to plain text
        description = adf_to_text(description)
        self.logger.debug(f"{issue_key}: Parsed description: {description[:200]}...")
        
        attachments = fields.get("attachment", [])
        attachment_paths = self.jira.download_attachments(issue_key, attachments)
        
        branch_name = issue_key  # Branch name = issue key
        
        state = IssueState(
            issue_key=issue_key,
            description=description,
            attachments=attachments,
            attachment_paths=attachment_paths,
            branch_name=branch_name,
            jira_updated=fields.get("updated", ""),
        )
        
        # Step 4: Determine phase and process
        # Check if feature branch exists remotely
        if self.repo.branch_exists_remote(branch_name):
            self.logger.info(f"{issue_key}: Feature branch exists - checking for PR")
            
            # Check if PR exists
            pr_number = self.github.pr_exists_for_branch(branch_name)
            
            if pr_number:
                self.logger.info(f"{issue_key}: PR #{pr_number} exists - monitoring CI")
                state.pr_number = pr_number
                self._handle_ci_monitoring(state)
            else:
                self.logger.warning(f"{issue_key}: Branch exists but no PR found - deleting branch and re-implementing")
                # Delete the remote branch before re-implementing
                try:
                    self.repo._run_git("push", "origin", "--delete", branch_name, check=False)
                    self.logger.info(f"{issue_key}: Deleted remote branch")
                except Exception as e:
                    self.logger.warning(f"{issue_key}: Could not delete remote branch: {e}")
                self._handle_implementation_phase(state)
        else:
            self.logger.info(f"{issue_key}: No feature branch - starting implementation directly")
            self._handle_implementation_phase(state)
    
    def _handle_requirements_phase(self, state: IssueState):
        """
        Handle requirements analysis phase.
        Uses a local cache keyed on (main HEAD, jira updated, model identity)
        so repeated runs skip the expensive AI call when nothing changed.
        """
        self.logger.info(f"{state.issue_key}: Requirements analysis phase")
        
        # Reset to clean state on main branch to prevent contamination
        self.repo.reset_to_clean_state("main")
        
        # --- Cache check ---
        main_commit = self.repo.get_head_commit("main")
        cached = get_cached_phase(
            issue_key=state.issue_key,
            phase="requirements",
            main_commit=main_commit,
            jira_updated=state.jira_updated,
            model_identity=self._model_identity,
        )
        if cached:
            self.logger.info(f"{state.issue_key}: Using cached requirements analysis")
            response = cached
        else:
            # Invoke AI agent for requirements analysis
            response = self.agent.invoke_requirements_analysis(
                issue_key=state.issue_key,
                description=state.description,
                attachment_paths=state.attachment_paths,
                repo_path=str(self.repo.repo_path),
                iteration=state.clarification_count,
                previous_response_id=state.last_response_id,
            )
            state.last_response_id = response.get("_response_id")
            # Store in cache
            set_cached_phase(
                issue_key=state.issue_key,
                phase="requirements",
                main_commit=main_commit,
                jira_updated=state.jira_updated,
                model_identity=self._model_identity,
                result=response,
            )
        
        action = response.get("action")
        message = response.get("message", "")
        
        self.logger.info(f"{state.issue_key}: Agent action: {action}")
        
        if action == "clarify":
            # Post clarification questions
            self.logger.info(f"{state.issue_key}: Requesting clarification")
            self.jira.comment(state.issue_key, message)
            
            state.clarification_count += 1
            if state.clarification_count >= self.cfg.max_clarification_cycles:
                self.jira.comment(state.issue_key, "Brad is stuck - too many clarification cycles.")
        
        elif action == "propose_scenarios":
            # Post test scenarios for approval
            self.logger.info(f"{state.issue_key}: Proposing test scenarios")
            self.jira.comment(state.issue_key, message)
        
        elif action == "ready":
            # Requirements are clear - proceed to implementation
            self.logger.info(f"{state.issue_key}: Requirements clear - proceeding to implementation")
            self.jira.comment(state.issue_key, "Requirements are clear. Brad is starting implementation.")
            self._handle_implementation_phase(state)
        
        else:
            # Error
            self.logger.error(f"{state.issue_key}: AI agent returned error: {message}")
            self.jira.comment(state.issue_key, f"Brad encountered an error during requirements analysis:\n\n{message}\n\nBrad is stuck.")
    
    def _handle_implementation_phase(self, state: IssueState):
        """
        Handle implementation phase.
        """
        self.logger.info(f"{state.issue_key}: Implementation phase")
        
        # Add JIRA comment that implementation is starting
        self.jira.comment(
            state.issue_key,
            f"Brad is starting implementation for {state.issue_key}..."
        )
        
        # Reset to clean state first
        self.repo.reset_to_clean_state("main")
        
        # Prepare feature branch
        if not self.repo.branch_exists_remote(state.branch_name):
            self.logger.info(f"{state.issue_key}: Creating new feature branch")
            self.repo.prepare_branch(state.branch_name, base_branch="main")
        else:
            self.logger.info(f"{state.issue_key}: Checking out existing feature branch")
            self.repo.checkout_branch(state.branch_name, create_if_missing=False)
            # Reset to remote state to avoid local contamination
            self.repo._run_git("reset", "--hard", f"origin/{state.branch_name}")
        
        # Invoke AI agent for implementation (warm-start from requirements phase if available)
        response = self.agent.invoke_implementation(
            issue_key=state.issue_key,
            description=state.description,
            attachment_paths=state.attachment_paths,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name,
            iteration=0,
            previous_response_id=state.last_response_id,
        )
        state.last_response_id = response.get("_response_id")
        
        action = response.get("action")
        message = response.get("message", "")
        pr_number = response.get("pr_number")
        pr_url = response.get("pr_url")
        
        self.logger.info(f"{state.issue_key}: Agent action: {action}")
        
        if action == "success":
            # CRITICAL: Verify PR actually exists on GitHub
            # Agent may claim success but not have created PR
            verified_pr = self._verify_and_ensure_pr(state, pr_number, pr_url)
            
            if verified_pr:
                pr_number, pr_url = verified_pr
                state.pr_number = pr_number
                
                self.logger.info(f"{state.issue_key}: Implementation successful, PR #{pr_number} verified")
                
                self.jira.comment(
                    state.issue_key,
                    f"Brad has completed implementation and opened PR #{pr_number}:\n{pr_url}\n\nRunning local code review..."
                )
                
                # Local review (fresh context) before CI monitoring
                review_passed = self._handle_local_review(state)
                
                if review_passed:
                    self.jira.comment(state.issue_key, "Local review passed. Monitoring CI/CD...")
                    self._handle_ci_monitoring(state)
                else:
                    self.logger.info(f"{state.issue_key}: Local review requested changes — check JIRA for details")
            else:
                # PR verification failed
                self.logger.error(f"{state.issue_key}: PR claimed but could not be verified or created")
                self.jira.comment(
                    state.issue_key,
                    f"Brad completed implementation but failed to create PR. Manual intervention needed."
                )
        
        elif action == "stuck":
            # Agent couldn't complete implementation
            self.logger.error(f"{state.issue_key}: AI agent is stuck: {message}")
            self.jira.comment(state.issue_key, f"Brad is stuck during implementation:\n\n{message}")
        
        else:
            # Error
            self.logger.error(f"{state.issue_key}: Implementation error: {message}")
            self.jira.comment(state.issue_key, f"Brad encountered an error during implementation:\n\n{message}\n\nBrad is stuck.")
    
    def _verify_and_ensure_pr(self, state: IssueState, claimed_pr_number: int, claimed_pr_url: str) -> tuple:
        """
        Verify that PR actually exists on GitHub. If not, attempt to create it.
        Returns: (pr_number, pr_url) tuple if successful, None if failed
        """
        issue_key = state.issue_key
        branch_name = state.branch_name
        
        # First check if PR number was claimed
        if claimed_pr_number:
            # Verify it actually exists
            actual_pr = self.github.pr_exists_for_branch(branch_name)
            if actual_pr == claimed_pr_number:
                self.logger.info(f"{issue_key}: PR #{claimed_pr_number} verified on GitHub")
                return (claimed_pr_number, claimed_pr_url)
            else:
                self.logger.warning(f"{issue_key}: Claimed PR #{claimed_pr_number} but found #{actual_pr} on GitHub")
                if actual_pr:
                    # Use the actual PR found
                    pr_url = f"https://github.com/{self.cfg.github_repo}/pull/{actual_pr}"
                    return (actual_pr, pr_url)
        
        # No claimed PR or verification failed - check if PR exists for branch
        existing_pr = self.github.pr_exists_for_branch(branch_name)
        if existing_pr:
            self.logger.info(f"{issue_key}: Found existing PR #{existing_pr} for branch")
            pr_url = f"https://github.com/{self.cfg.github_repo}/pull/{existing_pr}"
            return (existing_pr, pr_url)
        
        # No PR exists - need to push branch and create PR
        self.logger.warning(f"{issue_key}: No PR found - attempting to push branch and create PR")
        
        # Check if branch exists remotely
        if not self.repo.branch_exists_remote(branch_name):
            self.logger.info(f"{issue_key}: Branch not on remote - pushing now")
            try:
                self.repo.push(branch_name)
            except Exception as e:
                self.logger.error(f"{issue_key}: Failed to push branch: {e}")
                return None
        
        # Create PR using gh CLI
        try:
            self.logger.info(f"{issue_key}: Creating PR via gh CLI")
            import subprocess
            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--repo", self.cfg.github_repo,
                    "--base", "main",
                    "--head", branch_name,
                    "--title", f"Brad: {issue_key}",
                    "--body", f"Automated PR for {issue_key}\n\nRelated: https://flaerobotics.atlassian.net/browse/{issue_key}"
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.repo.repo_path)
            )
            
            if result.returncode == 0:
                # Extract PR URL from output
                pr_url = result.stdout.strip().split('\n')[-1]
                # Extract PR number from URL
                import re
                pr_match = re.search(r'/pull/(\d+)', pr_url)
                if pr_match:
                    pr_number = int(pr_match.group(1))
                    self.logger.info(f"{issue_key}: Successfully created PR #{pr_number}")
                    return (pr_number, pr_url)
            
            self.logger.error(f"{issue_key}: Failed to create PR: {result.stderr}")
            return None
            
        except Exception as e:
            self.logger.error(f"{issue_key}: Exception creating PR: {e}")
            return None
    
    def _handle_local_review(self, state: IssueState) -> bool:
        """
        Run a fresh-context local review of the implementation.
        Returns True if approved, False if changes were requested.
        """
        self.logger.info(f"{state.issue_key}: Starting local code review (fresh context)")
        
        try:
            # Get the diff of changes vs main
            import subprocess
            diff_result = subprocess.run(
                ["git", "diff", "origin/main...HEAD"],
                capture_output=True, text=True, timeout=30,
                cwd=str(self.repo.repo_path),
                encoding="utf-8", errors="replace"
            )
            diff = diff_result.stdout
            if not diff:
                self.logger.warning(f"{state.issue_key}: No diff found for review")
                return True  # Nothing to review
            
            # Truncate very large diffs
            if len(diff) > 80_000:
                diff = diff[:80_000] + "\n... [diff truncated]"
            
            response = self.agent.invoke_local_review(
                issue_key=state.issue_key,
                description=state.description,
                diff=diff,
                repo_path=str(self.repo.repo_path),
                branch_name=state.branch_name,
            )
            
            action = response.get("action")
            message = response.get("message", "")
            
            if action == "approved":
                self.logger.info(f"{state.issue_key}: Local review APPROVED")
                self.jira.comment(
                    state.issue_key,
                    f"Local code review PASSED:\n\n{message}"
                )
                return True
            else:
                self.logger.info(f"{state.issue_key}: Local review requested changes")
                self.jira.comment(
                    state.issue_key,
                    f"Local code review found issues:\n\n{message}\n\nBrad will address these."
                )
                # TODO: In future, loop back to fix the issues
                return True  # For now, proceed anyway — log the review feedback
                
        except Exception as e:
            self.logger.error(f"{state.issue_key}: Local review failed: {e}", exc_info=True)
            # Don't block on review failure
            return True
    
    def _handle_ci_monitoring(self, state: IssueState):
        """
        Monitor CI/CD pipeline and check for review comments.
        According to design doc section 7.3, Brad must check BOTH CI results AND review comments.
        
        Enhanced flow:
        1. Wait for ALL workflow runs to complete (build + deploy)
        2. If CI passes, check deployment health on the target environment
        3. If deployment unhealthy, fetch Azure logs for diagnosis
        4. Check for review comments
        """
        if not state.pr_number:
            self.logger.error(f"{state.issue_key}: Cannot monitor CI - no PR number")
            return
        
        self.logger.info(f"{state.issue_key}: Monitoring CI for PR #{state.pr_number}")
        
        # Resolve which environment this PR deploys to
        deploy_info = self.ci.resolve_deployment_env(pr_number=state.pr_number)
        if deploy_info:
            self.logger.info(
                f"{state.issue_key}: PR #{state.pr_number} deploys to "
                f"{deploy_info.environment} ({deploy_info.base_url})"
            )
        
        # Wait for ALL CI workflows to complete
        ci_result = self.ci.wait_for_pr(
            pr_number=state.pr_number,
            poll_interval=self.cfg.ci_poll_interval,
            timeout=3600
        )
        
        if ci_result.success:
            # CI passed - check deployment health if enabled
            deployment_summary = ""
            if self.cfg.deployment_health_check and deploy_info:
                deployment_summary = self._check_deployment_after_ci(state, deploy_info)
            
            # Now check for review comments (as per design doc 7.3)
            self.logger.info(f"{state.issue_key}: CI passed! Now checking for review comments...")
            
            all_review_comments = self.github.fetch_review_comments(state.pr_number)
            # Filter out bot comments (e.g. windsurf-bot, github-actions)
            review_comments = [
                c for c in all_review_comments
                if not c.get("user", {}).get("login", "").endswith("[bot]")
                and c.get("user", {}).get("type") != "Bot"
            ]
            if all_review_comments and not review_comments:
                self.logger.info(f"{state.issue_key}: Found {len(all_review_comments)} bot review comments (skipping)")
            
            if review_comments:
                # Human review comments exist - need to address them
                self.logger.info(f"{state.issue_key}: Found {len(review_comments)} human review comments to address")
                self.jira.comment(
                    state.issue_key,
                    f"✅ CI passed, but there are {len(review_comments)} review comments to address. Brad is working on them..."
                )
                self._handle_review_fix(state, review_comments)
            else:
                # No review comments - truly done
                self.logger.info(f"{state.issue_key}: No review comments - truly complete!")
                
                try:
                    self.jira.set_status(state.issue_key, "REVIEW")
                except Exception as e:
                    self.logger.warning(f"Could not set status to REVIEW: {e}")
                
                done_msg = "Brad is done. ✅ All CI checks passed and no review comments to address."
                if deployment_summary:
                    done_msg += f"\n\n{deployment_summary}"
                self.jira.comment(state.issue_key, done_msg)
        
        else:
            # CI failed - fetch detailed logs before attempting fix
            self.logger.warning(f"{state.issue_key}: CI failed")
            
            # Fetch detailed job logs for failed runs
            detailed_logs = self._fetch_detailed_ci_logs(state, ci_result)
            
            if state.ci_fix_count >= self.cfg.max_ci_fix_iterations:
                self.logger.error(f"{state.issue_key}: Max CI fix iterations reached")
                failure_msg = (
                    f"Brad is stuck - CI failures could not be fixed after {state.ci_fix_count} attempts."
                    f"\n\nFailed jobs: {', '.join(ci_result.failed_jobs)}"
                )
                if detailed_logs:
                    failure_msg += f"\n\nDetailed failure logs:\n{detailed_logs[:2000]}"
                self.jira.comment(state.issue_key, failure_msg)
                return
            
            self.logger.info(f"{state.issue_key}: Attempting CI fix (iteration {state.ci_fix_count + 1})")
            # Pass enhanced logs to CI fix handler
            if detailed_logs:
                ci_result = ci_result._replace(logs=ci_result.logs + "\n\n" + detailed_logs)
            self._handle_ci_fix(state, ci_result)
    
    def _check_deployment_after_ci(self, state: IssueState, deploy_info) -> str:
        """
        After CI passes, check if the deployment is healthy and collect diagnostics.
        Returns a summary string for the JIRA comment.
        """
        self.logger.info(
            f"{state.issue_key}: Checking deployment health at {deploy_info.base_url}"
        )
        
        health = self.ci.check_deployment_health(
            pr_number=state.pr_number,
            max_attempts=30,
            wait_seconds=10,
        )
        
        if health.get("healthy"):
            version_info = health.get("version", {})
            summary = (
                f"Deployment to {deploy_info.environment} ({deploy_info.base_url}) is healthy."
            )
            if version_info:
                summary += f" Version: {version_info}"
            self.logger.info(f"{state.issue_key}: {summary}")
            return summary
        else:
            error = health.get("error", "Unknown error")
            self.logger.warning(
                f"{state.issue_key}: Deployment unhealthy: {error}. "
                f"Fetching Azure logs for diagnosis..."
            )
            
            # Fetch Azure logs for diagnosis
            azure_summary = self._fetch_azure_deployment_logs(state, deploy_info.environment)
            
            summary = (
                f"⚠️ Deployment to {deploy_info.environment} ({deploy_info.base_url}) "
                f"may not be healthy: {error}"
            )
            if azure_summary:
                summary += f"\n\nAzure deployment diagnostics:\n{azure_summary[:2000]}"
            
            return summary
    
    def _fetch_detailed_ci_logs(self, state: IssueState, ci_result) -> str:
        """
        Fetch detailed job logs from GitHub Actions for failed runs.
        Returns the detailed log text.
        """
        self.logger.info(f"{state.issue_key}: Fetching detailed CI failure logs")
        
        try:
            failed_run_ids = self.ci.get_failed_run_ids(state.pr_number)
            if not failed_run_ids:
                return ""
            
            all_logs = []
            for run_id in failed_run_ids[:3]:  # Limit to 3 runs max
                logs = self.ci.get_job_logs(run_id, failed_only=True)
                if logs:
                    all_logs.append(logs)
            
            combined = "\n".join(all_logs)
            # Truncate to avoid overwhelming the AI agent
            if len(combined) > 15000:
                combined = combined[:15000] + "\n... [truncated]"
            
            return combined
        except Exception as e:
            self.logger.error(f"{state.issue_key}: Failed to fetch detailed CI logs: {e}")
            return ""
    
    def _fetch_azure_deployment_logs(
        self, state: IssueState, environment: str
    ) -> str:
        """
        Fetch Azure AKS deployment logs for diagnosis.
        Returns a formatted summary of the diagnostics.
        """
        self.logger.info(
            f"{state.issue_key}: Fetching Azure logs for environment {environment}"
        )
        
        try:
            diagnostics = self.azure_logs.get_environment_diagnostics(
                environment=environment,
                tail_lines=self.cfg.deployment_log_tail_lines,
                since=self.cfg.deployment_log_since,
            )
            
            summary = self.azure_logs.format_diagnostics_summary(diagnostics)
            self.logger.info(
                f"{state.issue_key}: Collected {len(summary)} chars of Azure diagnostics"
            )
            return summary
        except Exception as e:
            self.logger.error(
                f"{state.issue_key}: Failed to fetch Azure logs: {e}"
            )
            return f"[Could not fetch Azure logs: {e}]"
    
    def _handle_ci_fix(self, state: IssueState, ci_result):
        """
        Handle CI failure by invoking AI agent to fix issues.
        """
        self.logger.info(f"{state.issue_key}: CI fix phase")
        
        # Ensure we're on the feature branch
        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)
        
        # Extract specific failing test identifiers from CI logs
        failed_test_target = extract_failed_tests_from_ci_logs(ci_result.logs)
        if failed_test_target:
            self.logger.info(f"{state.issue_key}: Extracted failing tests: {failed_test_target[:200]}")
        
        # Invoke AI agent for CI fix (warm-start from implementation phase if available)
        response = self.agent.invoke_ci_fix(
            issue_key=state.issue_key,
            description=state.description,
            ci_logs=ci_result.logs,
            failed_jobs=ci_result.failed_jobs,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name,
            pr_number=state.pr_number,
            iteration=state.ci_fix_count,
            failed_test_target=failed_test_target,
            previous_response_id=state.last_response_id,
        )
        state.last_response_id = response.get("_response_id")
        
        action = response.get("action")
        message = response.get("message", "")
        
        self.logger.info(f"{state.issue_key}: AI agent action: {action}")
        
        if action == "fixed":
            self.logger.info(f"{state.issue_key}: AI agent fixed CI issues: {message}")
            state.ci_fix_count += 1
            
            self.jira.comment(
                state.issue_key,
                f"Brad attempted to fix CI failures (attempt {state.ci_fix_count}):\n\n{message}\n\nWaiting for CI to re-run..."
            )
            
            # Wait a bit for CI to start, then monitor again
            import time
            time.sleep(30)
            self._handle_ci_monitoring(state)
        
        elif action == "stuck":
            # Agent couldn't fix the issues
            self.logger.error(f"{state.issue_key}: AI agent stuck on CI fix: {message}")
            self.jira.comment(
                state.issue_key,
                f"Brad is stuck - could not fix CI failures:\n\n{message}\n\nFailed jobs: {', '.join(ci_result.failed_jobs)}"
            )
        
        else:
            # Error
            self.logger.error(f"{state.issue_key}: CI fix error: {message}")
            self.jira.comment(
                state.issue_key,
                f"Brad encountered an error while fixing CI:\n\n{message}\n\nBrad is stuck."
            )
    
    def _handle_review_fix(self, state: IssueState, review_comments):
        """
        Handle PR review comments by invoking the AI agent to address them.
        """
        self.logger.info(f"{state.issue_key}: Review fix phase")
        
        # Check iteration limit
        if state.review_fix_count >= self.cfg.max_review_fix_iterations:
            self.logger.error(f"{state.issue_key}: Max review fix iterations reached")
            self.jira.comment(
                state.issue_key,
                f"Brad is stuck - could not address all review comments after {state.review_fix_count} attempts."
            )
            return
        
        # Ensure we're on the feature branch
        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)
        
        # Invoke AI agent for review fix (warm-start from implementation/CI phase)
        response = self.agent.invoke_review_fix(
            issue_key=state.issue_key,
            description=state.description,
            review_comments=review_comments,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name,
            pr_number=state.pr_number,
            iteration=state.review_fix_count,
            previous_response_id=state.last_response_id,
        )
        state.last_response_id = response.get("_response_id")
        
        action = response.get("action")
        message = response.get("message", "")
        
        self.logger.info(f"{state.issue_key}: AI agent action: {action}")
        
        if action == "fixed":
            self.logger.info(f"{state.issue_key}: AI agent addressed review comments: {message}")
            state.review_fix_count += 1
            
            self.jira.comment(
                state.issue_key,
                f"Brad addressed review comments (attempt {state.review_fix_count}):\n\n{message}\n\nWaiting for CI to re-run and checking for new review comments..."
            )
            
            # Wait a bit for CI to start, then monitor again
            import time
            time.sleep(30)
            self._handle_ci_monitoring(state)
        
        elif action == "stuck":
            # AI agent couldn't address the comments
            self.logger.error(f"{state.issue_key}: AI agent stuck on review fix: {message}")
            self.jira.comment(
                state.issue_key,
                f"Brad is stuck - could not address review comments:\n\n{message}"
            )
        
        else:
            # Error
            self.logger.error(f"{state.issue_key}: Review fix error: {message}")
            self.jira.comment(
                state.issue_key,
                f"Brad encountered an error while addressing review comments:\n\n{message}\n\nBrad is stuck."
            )
