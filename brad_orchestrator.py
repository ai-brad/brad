"""
Brad - Autonomous AI Software Engineer
Thin orchestration layer that delegates all decision-making to Claude CLI.
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

@dataclass
class IssueState:
    """Tracks the state of an issue being processed."""
    issue_key: str
    description: str
    attachments: list
    attachment_paths: list
    branch_name: str
    pr_number: Optional[int] = None
    clarification_count: int = 0
    ci_fix_count: int = 0


class BradOrchestrator:
    """
    Thin orchestrator for Brad.
    Manages state machine and delegates all engineering decisions to Claude CLI.
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
        
        self.logger.info(f"Brad orchestrator initialized with {cfg.ai_agent} agent")
    
    def run_once(self):
        """
        Main orchestration loop - process one batch of issues.
        """
        self.logger.info("=" * 80)
        self.logger.info("Brad run started")
        self.logger.info("=" * 80)
        
        try:
            # Fetch issues with BradReview label
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
            branch_name=branch_name
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
                self.logger.warning(f"{issue_key}: Branch exists but no PR found - re-implementing")
                self._handle_implementation_phase(state)
        else:
            self.logger.info(f"{issue_key}: No feature branch - starting requirements phase")
            self._handle_requirements_phase(state)
    
    def _handle_requirements_phase(self, state: IssueState):
        """
        Handle requirements analysis phase.
        """
        self.logger.info(f"{state.issue_key}: Requirements analysis phase")
        
        # Ensure we're on main branch
        self.repo.checkout_branch("main", create_if_missing=False)
        self.repo._run_git("pull", "origin", "main")
        
        # Invoke AI agent for requirements analysis
        response = self.agent.invoke_requirements_analysis(
            issue_key=state.issue_key,
            description=state.description,
            attachment_paths=state.attachment_paths,
            repo_path=str(self.repo.repo_path),
            iteration=state.clarification_count
        )
        
        action = response.get("action")
        message = response.get("message", "")
        
        self.logger.info(f"{state.issue_key}: Claude action: {action}")
        
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
        
        # Prepare feature branch
        if not self.repo.branch_exists_remote(state.branch_name):
            self.logger.info(f"{state.issue_key}: Creating new feature branch")
            self.repo.prepare_branch(state.branch_name, base_branch="main")
        else:
            self.logger.info(f"{state.issue_key}: Checking out existing feature branch")
            self.repo.checkout_branch(state.branch_name, create_if_missing=False)
            self.repo._run_git("pull", "origin", state.branch_name)
        
        # Invoke AI agent for implementation
        response = self.agent.invoke_implementation(
            issue_key=state.issue_key,
            description=state.description,
            attachment_paths=state.attachment_paths,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name,
            iteration=0
        )
        
        action = response.get("action")
        message = response.get("message", "")
        pr_number = response.get("pr_number")
        pr_url = response.get("pr_url")
        
        self.logger.info(f"{state.issue_key}: Claude action: {action}")
        
        if action == "success" and pr_number:
            # Implementation successful, PR created
            self.logger.info(f"{state.issue_key}: Implementation successful, PR #{pr_number} created")
            state.pr_number = pr_number
            
            self.jira.comment(
                state.issue_key,
                f"Brad has completed implementation and opened PR #{pr_number}:\n{pr_url}\n\nMonitoring CI/CD..."
            )
            
            # Monitor CI
            self._handle_ci_monitoring(state)
        
        elif action == "stuck":
            # Claude couldn't complete implementation
            self.logger.error(f"{state.issue_key}: AI agent is stuck: {message}")
            self.jira.comment(state.issue_key, f"Brad is stuck during implementation:\n\n{message}")
        
        else:
            # Error
            self.logger.error(f"{state.issue_key}: Implementation error: {message}")
            self.jira.comment(state.issue_key, f"Brad encountered an error during implementation:\n\n{message}\n\nBrad is stuck.")
    
    def _handle_ci_monitoring(self, state: IssueState):
        """
        Monitor CI/CD pipeline and handle failures.
        """
        if not state.pr_number:
            self.logger.error(f"{state.issue_key}: Cannot monitor CI - no PR number")
            return
        
        self.logger.info(f"{state.issue_key}: Monitoring CI for PR #{state.pr_number}")
        
        # Wait for CI to complete
        ci_result = self.ci.wait_for_pr(
            pr_number=state.pr_number,
            poll_interval=self.cfg.ci_poll_interval,
            timeout=3600
        )
        
        if ci_result.success:
            # CI passed - mark as ready for review
            self.logger.info(f"{state.issue_key}: CI passed!")
            
            try:
                self.jira.set_status(state.issue_key, "REVIEW")
            except Exception as e:
                self.logger.warning(f"Could not set status to REVIEW: {e}")
            
            self.jira.comment(state.issue_key, "Brad is done. ✅ All CI checks passed.")
        
        else:
            # CI failed - attempt to fix
            self.logger.warning(f"{state.issue_key}: CI failed")
            
            if state.ci_fix_count >= self.cfg.max_ci_fix_iterations:
                self.logger.error(f"{state.issue_key}: Max CI fix iterations reached")
                self.jira.comment(
                    state.issue_key,
                    f"Brad is stuck - CI failures could not be fixed after {state.ci_fix_count} attempts.\n\nFailed jobs: {', '.join(ci_result.failed_jobs)}"
                )
                return
            
            self.logger.info(f"{state.issue_key}: Attempting CI fix (iteration {state.ci_fix_count + 1})")
            self._handle_ci_fix(state, ci_result)
    
    def _handle_ci_fix(self, state: IssueState, ci_result):
        """
        Handle CI failure by invoking Claude to fix issues.
        """
        self.logger.info(f"{state.issue_key}: CI fix phase")
        
        # Ensure we're on the feature branch
        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)
        
        # Invoke AI agent for CI fix
        response = self.agent.invoke_ci_fix(
            issue_key=state.issue_key,
            description=state.description,
            ci_logs=ci_result.logs,
            failed_jobs=ci_result.failed_jobs,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name,
            pr_number=state.pr_number,
            iteration=state.ci_fix_count
        )
        
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
            # Claude couldn't fix the issues
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
