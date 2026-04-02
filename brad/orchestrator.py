"""
Brad - Autonomous AI Software Engineer
Orchestrator that manages the issue lifecycle:
requirements → implementation → local review → CI monitoring → deployment health.
"""

import re
import subprocess
import time
from typing import Dict, Optional
from dataclasses import dataclass
from brad.logging_config import get_logger
from brad.config import Config
from brad.adapters.ticketing.jira_adapter import JiraAdapter
from brad.adapters.code_repository.github_adapter import GitHubAdapter
from brad.adapters.ci_cd.github_actions_adapter import GitHubActionsAdapter
from brad.adapters.observability.azure_adapter import AzureObservabilityAdapter
from brad.adapters.llm.azure_openai_adapter import AzureOpenAIAdapter
from brad.agents.interface import AIAgentInterface
from brad.repo_manager import RepoManager
from brad.adf_parser import adf_to_text
from brad.phase_cache import get_cached_phase, set_cached_phase
from brad.test_selector import extract_failed_tests_from_ci_logs
from brad import db


@dataclass
class IssueState:
    """Tracks the state of an issue being processed."""
    issue_key: str
    description: str
    attachments: list
    attachment_paths: list
    branch_name: str
    execution_id: int = 0
    jira_updated: str = ""
    last_response_id: Optional[str] = None
    pr_number: Optional[int] = None
    clarification_count: int = 0
    ci_fix_count: int = 0
    review_fix_count: int = 0
    local_review_fix_count: int = 0


class BradOrchestrator:
    """
    Brad orchestrator. Manages the issue lifecycle:
    requirements → implementation → local review → CI monitoring → deployment health.
    """

    def __init__(self, cfg: Config):
        self.logger = get_logger(__name__)
        self.cfg = cfg

        # Initialize adapters
        self.ticketing = JiraAdapter(cfg)
        self.code_repo = GitHubAdapter(cfg)
        self.ci = GitHubActionsAdapter(cfg)
        self.observability = AzureObservabilityAdapter(cfg)
        llm = AzureOpenAIAdapter(cfg)
        self.agent = AIAgentInterface(llm, cfg)
        self.repo = RepoManager(cfg)

        # Model identity for cache keys
        self._model_identity = f"{cfg.azure_openai_endpoint}|{cfg.azure_openai_model}"

        self.logger.info(f"Brad orchestrator initialized (model: {cfg.azure_openai_model})")

    def _calculate_cost(self, usage) -> float:
        """Calculate cost from LLM usage stats using DB model costs.
        Cached prompt tokens are charged at 50% of normal prompt rate."""
        if not usage:
            return 0.0
        costs = db.get_model_cost(self.cfg.azure_openai_model)
        cached = getattr(usage, "cached_tokens", 0) or 0
        non_cached_prompt = max(0, usage.prompt_tokens - cached)
        prompt_cost = (non_cached_prompt / 1000.0) * costs["prompt"]
        cached_cost = (cached / 1000.0) * costs["prompt"] * 0.5
        completion_cost = (usage.completion_tokens / 1000.0) * costs["completion"]
        return prompt_cost + cached_cost + completion_cost

    def _record_step(self, execution_id: int, phase: str, response: Dict) -> None:
        """Record a step with cost/usage data in the database."""
        usage = response.get("_usage")
        cost = self._calculate_cost(usage)
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0

        step_id = db.create_step(execution_id, phase)
        db.finish_step(
            step_id,
            status=response.get("action", "unknown"),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            result_summary=response.get("message", "")[:500],
        )
        db.update_execution_costs(execution_id, prompt_tokens, completion_tokens, cost)

    def run_once(self):
        """Main orchestration loop - process one batch of issues."""
        self.logger.info("=" * 80)
        self.logger.info("Brad run started")
        self.logger.info("=" * 80)

        try:
            # First, check for review comments on existing PRs
            self.logger.info("Checking for code review comments...")
            try:
                self._process_review_comments()
            except Exception as e:
                self.logger.error(f"Failed to process review comments: {e}")

            # Then fetch issues with BradReview label
            issues = self.ticketing.fetch_issues_with_label("BradReview")

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

                    try:
                        self.ticketing.comment(
                            issue_key,
                            f"Brad encountered an unexpected error:\n\n```\n{str(e)}\n```\n\nBrad is stuck."
                        )
                    except:
                        self.logger.error(f"Failed to post error comment to {issue_key}")

        finally:
            self.logger.info("=" * 80)
            self.logger.info("Brad run completed")
            self.logger.info("=" * 80)

    def _process_review_comments(self):
        """Check all Brad PRs for new review comments and process them."""
        brad_prs = self.code_repo.get_brad_prs()
        if not brad_prs:
            return

        for pr in brad_prs:
            pr_number = pr['number']
            branch_name = pr['head']['ref']

            comments = self.code_repo.get_review_comments_needing_response(pr_number)
            if not comments:
                continue

            self.logger.info(f"Found {len(comments)} unresponded comments on PR #{pr_number}")

            for comment in comments:
                try:
                    self._process_single_review_comment(pr_number, branch_name, comment)
                except Exception as e:
                    self.logger.error(f"Failed to process comment {comment['id']}: {e}")

    def _process_single_review_comment(self, pr_number: int, branch_name: str, comment: Dict):
        """Process a single review comment using the Code Review Reader."""
        comment_id = comment['id']

        # Reply "Brad checking" to claim it
        self.code_repo.reply_to_review_comment(pr_number, comment_id, "Brad checking")

        # Checkout branch and sync
        self.repo.checkout_branch(branch_name)
        self.repo._run_git("fetch", "origin", branch_name, check=False)
        self.repo._run_git("reset", "--hard", f"origin/{branch_name}", check=False)

        # Use the Code Review Reader to analyze and act on the comment
        result = self.agent.invoke_code_review_reader(
            pr_number=pr_number,
            branch_name=branch_name,
            comment=comment,
            repo_path=str(self.repo.repo_path),
        )

        action = result.get('action')
        reply_text = result.get('reply', '')

        if action == 'error':
            raise Exception(result.get('message', 'Unknown error'))

        if action == 'code_changed':
            # Push changes and reply
            try:
                self.repo.push(branch_name, force=False)
            except Exception:
                self.repo.push(branch_name, force=True)
            self.code_repo.reply_to_review_comment(pr_number, comment_id, reply_text or "Fixed in latest push.")
        elif action == 'replied':
            # Just reply to the comment, no code changes
            self.code_repo.reply_to_review_comment(pr_number, comment_id, reply_text or "Acknowledged.")
        else:
            self.code_repo.reply_to_review_comment(pr_number, comment_id, f"Brad could not resolve this: {reply_text}")

    def _process_issue(self, issue: Dict):
        """Process a single issue through the Brad workflow."""
        issue_key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "N/A")

        self.logger.info(f"Processing issue: {issue_key}")
        self.logger.info(f"Summary: {summary}")

        # Create execution record
        execution_id = db.create_execution(issue_key, summary)

        try:
            # Step 1: Remove BradReview label immediately
            self.ticketing.remove_label(issue_key, "BradReview")

            # Step 2: Set status to IN PROGRESS
            try:
                self.ticketing.set_status(issue_key, "IN PROGRESS")
            except Exception as e:
                self.logger.warning(f"Could not set status to IN PROGRESS: {e}")

            # Step 3: Prepare issue state
            description = fields.get("description", "")
            if not description:
                self.logger.error(f"{issue_key}: No description provided")
                self.ticketing.comment(issue_key, "Brad cannot process this issue: No description provided.")
                db.finish_execution(execution_id, status="error", error_message="No description")
                return

            description = adf_to_text(description)

            attachments = fields.get("attachment", [])
            attachment_paths = self.ticketing.download_attachments(issue_key, attachments)

            branch_name = issue_key

            state = IssueState(
                issue_key=issue_key,
                description=description,
                attachments=attachments,
                attachment_paths=attachment_paths,
                branch_name=branch_name,
                execution_id=execution_id,
                jira_updated=fields.get("updated", ""),
            )

            # Step 4: If branch/PR already exist, close the old PR and clean up (re-trigger)
            if self.repo.branch_exists_remote(branch_name):
                pr_number = self.code_repo.pr_exists_for_branch(branch_name)
                if pr_number:
                    self.logger.info(f"{issue_key}: Closing old PR #{pr_number} and deleting branch for fresh start")
                    try:
                        self.code_repo.close_pr(pr_number, f"Closing: Brad is re-processing {issue_key} with updated requirements.")
                    except Exception as e:
                        self.logger.warning(f"{issue_key}: Could not close old PR #{pr_number}: {e}")
                try:
                    self.repo._run_git("push", "origin", "--delete", branch_name, check=False)
                except Exception as e:
                    self.logger.warning(f"{issue_key}: Could not delete remote branch: {e}")

            self._handle_implementation_phase(state)

            # Finish execution with status reflecting actual outcome
            final_status = "completed" if state.pr_number else "stuck"
            db.finish_execution(
                execution_id,
                status=final_status,
                pr_number=state.pr_number,
                pr_url=f"https://github.com/{self.cfg.github_repo}/pull/{state.pr_number}" if state.pr_number else None,
                error_message="Brad could not create a PR" if not state.pr_number else None,
            )

        except Exception as e:
            db.finish_execution(execution_id, status="error", error_message=str(e)[:500])
            raise

    def _handle_requirements_phase(self, state: IssueState):
        """Handle requirements analysis phase."""
        self.logger.info(f"{state.issue_key}: Requirements analysis phase")
        self.repo.reset_to_clean_state("main")

        main_commit = self.repo.get_head_commit("main")
        cached = get_cached_phase(
            issue_key=state.issue_key, phase="requirements",
            main_commit=main_commit, jira_updated=state.jira_updated,
            model_identity=self._model_identity,
        )
        if cached:
            response = cached
        else:
            response = self.agent.invoke_requirements_analysis(
                issue_key=state.issue_key, description=state.description,
                attachment_paths=state.attachment_paths,
                repo_path=str(self.repo.repo_path),
                iteration=state.clarification_count,
                previous_response_id=state.last_response_id,
            )
            state.last_response_id = response.get("_response_id")
            self._record_step(state.execution_id, "requirements", response)
            set_cached_phase(
                issue_key=state.issue_key, phase="requirements",
                main_commit=main_commit, jira_updated=state.jira_updated,
                model_identity=self._model_identity, result=response,
            )

        action = response.get("action")
        message = response.get("message", "")

        if action == "clarify":
            self.ticketing.comment(state.issue_key, message)
            state.clarification_count += 1
            if state.clarification_count >= self.cfg.max_clarification_cycles:
                self.ticketing.comment(state.issue_key, "Brad is stuck - too many clarification cycles.")
        elif action == "propose_scenarios":
            self.ticketing.comment(state.issue_key, message)
        elif action == "ready":
            self.ticketing.comment(state.issue_key, "Requirements are clear. Brad is starting implementation.")
            self._handle_implementation_phase(state)
        else:
            self.ticketing.comment(state.issue_key, f"Brad encountered an error during requirements analysis:\n\n{message}\n\nBrad is stuck.")

    def _handle_implementation_phase(self, state: IssueState):
        """Handle implementation phase."""
        self.logger.info(f"{state.issue_key}: Implementation phase")
        self.ticketing.comment(state.issue_key, f"Brad is starting implementation for {state.issue_key}...")

        self.repo.reset_to_clean_state("main")

        if not self.repo.branch_exists_remote(state.branch_name):
            self.repo.prepare_branch(state.branch_name, base_branch="main")
        else:
            self.repo.checkout_branch(state.branch_name, create_if_missing=False)
            self.repo._run_git("reset", "--hard", f"origin/{state.branch_name}")

        response = self.agent.invoke_implementation(
            issue_key=state.issue_key, description=state.description,
            attachment_paths=state.attachment_paths,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name, iteration=0,
            previous_response_id=state.last_response_id,
        )
        state.last_response_id = response.get("_response_id")
        self._record_step(state.execution_id, "implementation", response)

        action = response.get("action")
        message = response.get("message", "")
        pr_number = response.get("pr_number")
        pr_url = response.get("pr_url")

        if action == "success":
            verified_pr = self._verify_and_ensure_pr(state, pr_number, pr_url)
            if verified_pr:
                pr_number, pr_url = verified_pr
                state.pr_number = pr_number
                db.update_execution_pr(state.execution_id, pr_number, pr_url)
                self.ticketing.comment(
                    state.issue_key,
                    f"Brad has completed implementation and opened PR #{pr_number}:\n{pr_url}\n\nRunning local code review..."
                )
                review_result = self._handle_local_review(state)
                if review_result.get("action") == "approved":
                    self.ticketing.comment(state.issue_key, "Local review passed. Monitoring CI/CD...")
                    self._handle_ci_monitoring(state)
                elif review_result.get("action") == "changes_requested":
                    self._handle_local_review_fix(state, review_result.get("message", ""))
                else:
                    self.ticketing.comment(state.issue_key, "Local review could not be completed. Proceeding to CI/CD...")
                    self._handle_ci_monitoring(state)
            else:
                self.ticketing.comment(state.issue_key, "Brad completed implementation but failed to create PR. Manual intervention needed.")
        elif action == "stuck":
            self.ticketing.comment(state.issue_key, f"Brad is stuck during implementation:\n\n{message}")
        else:
            self.ticketing.comment(state.issue_key, f"Brad encountered an error during implementation:\n\n{message}\n\nBrad is stuck.")

    def _verify_and_ensure_pr(self, state: IssueState, claimed_pr_number, claimed_pr_url):
        """Verify that PR actually exists. If not, attempt to create it."""
        issue_key = state.issue_key
        branch_name = state.branch_name

        if claimed_pr_number:
            actual_pr = self.code_repo.pr_exists_for_branch(branch_name)
            if actual_pr == claimed_pr_number:
                return (claimed_pr_number, claimed_pr_url)
            elif actual_pr:
                pr_url = f"https://github.com/{self.cfg.github_repo}/pull/{actual_pr}"
                return (actual_pr, pr_url)

        existing_pr = self.code_repo.pr_exists_for_branch(branch_name)
        if existing_pr:
            pr_url = f"https://github.com/{self.cfg.github_repo}/pull/{existing_pr}"
            return (existing_pr, pr_url)

        if not self.repo.branch_exists_remote(branch_name):
            try:
                self.repo.push(branch_name)
            except Exception as e:
                self.logger.error(f"{issue_key}: Failed to push branch: {e}")
                return None

        try:
            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--repo", self.cfg.github_repo,
                    "--base", "main",
                    "--head", branch_name,
                    "--title", f"Brad: {issue_key}",
                    "--body", f"Automated PR for {issue_key}"
                ],
                capture_output=True, text=True, timeout=30,
                cwd=str(self.repo.repo_path)
            )
            if result.returncode == 0:
                pr_url = result.stdout.strip().split('\n')[-1]
                pr_match = re.search(r'/pull/(\d+)', pr_url)
                if pr_match:
                    pr_number = int(pr_match.group(1))
                    return (pr_number, pr_url)
            self.logger.error(f"{issue_key}: Failed to create PR: {result.stderr}")
            return None
        except Exception as e:
            self.logger.error(f"{issue_key}: Exception creating PR: {e}")
            return None

    def _handle_local_review(self, state: IssueState) -> Dict:
        """Run a fresh-context local review and return its structured outcome."""
        self.logger.info(f"{state.issue_key}: Starting local code review")
        step_id = db.create_step(state.execution_id, "local_review")
        try:
            diff_result = subprocess.run(
                ["git", "diff", "origin/main...HEAD"],
                capture_output=True, text=True, timeout=30,
                cwd=str(self.repo.repo_path), encoding="utf-8", errors="replace"
            )
            diff = diff_result.stdout
            if not diff:
                db.finish_step(step_id, status="completed", result_summary="No diff to review")
                return {"action": "approved", "message": "No diff to review"}
            if len(diff) > 80_000:
                diff = diff[:80_000] + "\n... [diff truncated]"

            response = self.agent.invoke_local_review(
                issue_key=state.issue_key, description=state.description,
                diff=diff, repo_path=str(self.repo.repo_path),
                branch_name=state.branch_name,
            )

            action = response.get("action")
            message = response.get("message", "")
            usage = response.get("_usage")
            cost = self._calculate_cost(usage)
            pt = usage.prompt_tokens if usage else 0
            ct = usage.completion_tokens if usage else 0
            db.finish_step(step_id, status=action or "completed", prompt_tokens=pt, completion_tokens=ct, cost=cost, result_summary=message[:500])
            db.update_execution_costs(state.execution_id, pt, ct, cost)

            if action == "approved":
                self.ticketing.comment(state.issue_key, f"Local code review PASSED:\n\n{message}")
                return {"action": "approved", "message": message}

            self.ticketing.comment(state.issue_key, f"Local code review found issues:\n\n{message}\n\nBrad will address these.")
            return {"action": "changes_requested", "message": message}
        except Exception as e:
            self.logger.error(f"{state.issue_key}: Local review failed: {e}", exc_info=True)
            db.finish_step(step_id, status="error", result_summary=str(e)[:500])
            return {"action": "error", "message": str(e)}

    def _handle_local_review_fix(self, state: IssueState, review_feedback: str) -> bool:
        """Address local review feedback and rerun local review before CI."""
        if state.local_review_fix_count >= self.cfg.max_review_fix_iterations:
            self.ticketing.comment(
                state.issue_key,
                f"Brad is stuck - could not address local review feedback after {state.local_review_fix_count} attempts."
            )
            return False

        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)

        response = self.agent.invoke_local_review_fix(
            issue_key=state.issue_key, description=state.description,
            review_feedback=review_feedback,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name,
            iteration=state.local_review_fix_count,
            previous_response_id=state.last_response_id,
        )
        state.last_response_id = response.get("_response_id")
        self._record_step(state.execution_id, "local_review_fix", response)

        action = response.get("action")
        message = response.get("message", "")

        if action == "fixed":
            state.local_review_fix_count += 1
            self.ticketing.comment(
                state.issue_key,
                f"Brad addressed local review feedback (attempt {state.local_review_fix_count}):\n\n{message}\n\nRe-running local review..."
            )
            review_result = self._handle_local_review(state)
            if review_result.get("action") == "approved":
                self.ticketing.comment(state.issue_key, "Local review passed. Monitoring CI/CD...")
                self._handle_ci_monitoring(state)
                return True
            if review_result.get("action") == "changes_requested":
                return self._handle_local_review_fix(state, review_result.get("message", ""))

            self.ticketing.comment(state.issue_key, "Local review could not be completed after fixes. Proceeding to CI/CD...")
            self._handle_ci_monitoring(state)
            return True
        if action == "stuck":
            self.ticketing.comment(
                state.issue_key,
                f"Brad is stuck - could not address local review feedback:\n\n{message}"
            )
            return False

        self.ticketing.comment(
            state.issue_key,
            f"Brad encountered an error while addressing local review feedback:\n\n{message}\n\nBrad is stuck."
        )
        return False

    def _handle_ci_monitoring(self, state: IssueState):
        """Monitor CI/CD pipeline and check for review comments."""
        pr_number = state.pr_number
        if pr_number is None:
            self.logger.error(f"{state.issue_key}: Cannot monitor CI - no PR number")
            return

        self.logger.info(f"{state.issue_key}: Monitoring CI for PR #{pr_number}")
        step_id = db.create_step(state.execution_id, "ci_monitoring")

        deploy_info = self.ci.resolve_deployment_env(pr_number=pr_number)

        ci_result = self.ci.wait_for_pr(
            pr_number=pr_number,
            poll_interval=self.cfg.ci_poll_interval,
            timeout=3600
        )

        if ci_result.success:
            db.finish_step(step_id, status="completed", result_summary=f"CI passed for PR #{pr_number}")
        else:
            db.finish_step(step_id, status="error", result_summary=f"CI failed: {', '.join(ci_result.failed_jobs)}"[:500])

        if ci_result.success:
            deployment_summary = ""
            if self.cfg.deployment_health_check and deploy_info:
                deployment_summary = self._check_deployment_after_ci(state, deploy_info)

            # Check for review comments (allow windsurf-bot, filter other bots)
            all_review_comments = self.code_repo.fetch_review_comments(pr_number)
            review_comments = []
            for c in all_review_comments:
                user_login = c.get("user", {}).get("login", "")
                user_type = c.get("user", {}).get("type", "")
                # Allow windsurf-bot specifically, filter other bots
                if user_login == "windsurf-bot[bot]":
                    review_comments.append(c)
                elif not user_login.endswith("[bot]") and user_type != "Bot":
                    review_comments.append(c)

            if review_comments:
                self.ticketing.comment(
                    state.issue_key,
                    f"CI passed, but there are {len(review_comments)} review comments to address. Brad is working on them..."
                )
                self._handle_review_fix(state, review_comments)
            else:
                try:
                    self.ticketing.set_status(state.issue_key, "REVIEW")
                except Exception as e:
                    self.logger.warning(f"Could not set status to REVIEW: {e}")

                done_msg = "Brad is done. All CI checks passed and no review comments to address."
                if deployment_summary:
                    done_msg += f"\n\n{deployment_summary}"
                self.ticketing.comment(state.issue_key, done_msg)
        else:
            detailed_logs = self._fetch_detailed_ci_logs(state, ci_result)

            if state.ci_fix_count >= self.cfg.max_ci_fix_iterations:
                failure_msg = (
                    f"Brad is stuck - CI failures could not be fixed after {state.ci_fix_count} attempts."
                    f"\n\nFailed jobs: {', '.join(ci_result.failed_jobs)}"
                )
                if detailed_logs:
                    failure_msg += f"\n\nDetailed failure logs:\n{detailed_logs[:2000]}"
                self.ticketing.comment(state.issue_key, failure_msg)
                return

            if detailed_logs:
                ci_result = ci_result._replace(logs=ci_result.logs + "\n\n" + detailed_logs)
            self._handle_ci_fix(state, ci_result)

    def _check_deployment_after_ci(self, state: IssueState, deploy_info) -> str:
        """After CI passes, check deployment health."""
        health = self.ci.check_deployment_health(pr_number=state.pr_number)

        if health.get("healthy"):
            summary = f"Deployment to {deploy_info.environment} ({deploy_info.base_url}) is healthy."
            version_info = health.get("version", {})
            if version_info:
                summary += f" Version: {version_info}"
            return summary
        else:
            error = health.get("error", "Unknown error")
            diagnostics = self.observability.get_environment_diagnostics(
                environment=deploy_info.environment,
                tail_lines=self.cfg.deployment_log_tail_lines,
                since=self.cfg.deployment_log_since,
            )
            azure_summary = self.observability.format_diagnostics_summary(diagnostics)
            summary = f"Deployment to {deploy_info.environment} may not be healthy: {error}"
            if azure_summary:
                summary += f"\n\nDeployment diagnostics:\n{azure_summary[:2000]}"
            return summary

    def _fetch_detailed_ci_logs(self, state: IssueState, ci_result) -> str:
        """Fetch detailed job logs from CI for failed runs."""
        pr_number = state.pr_number
        if pr_number is None:
            return ""
        try:
            failed_run_ids = self.ci.get_failed_run_ids(pr_number)
            if not failed_run_ids:
                return ""
            all_logs = []
            for run_id in failed_run_ids[:3]:
                logs = self.ci.get_job_logs(run_id, failed_only=True)
                if logs:
                    all_logs.append(logs)
            combined = "\n".join(all_logs)
            if len(combined) > 15000:
                combined = combined[:15000] + "\n... [truncated]"
            return combined
        except Exception as e:
            self.logger.error(f"{state.issue_key}: Failed to fetch detailed CI logs: {e}")
            return ""

    def _handle_ci_fix(self, state: IssueState, ci_result):
        """Handle CI failure by invoking AI agent to fix issues."""
        pr_number = state.pr_number
        if pr_number is None:
            self.logger.error(f"{state.issue_key}: Cannot fix CI - no PR number")
            return

        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)

        failed_test_target = extract_failed_tests_from_ci_logs(ci_result.logs)

        response = self.agent.invoke_ci_fix(
            issue_key=state.issue_key, description=state.description,
            ci_logs=ci_result.logs, failed_jobs=ci_result.failed_jobs,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name, pr_number=pr_number,
            iteration=state.ci_fix_count,
            failed_test_target=failed_test_target,
            previous_response_id=state.last_response_id,
        )
        state.last_response_id = response.get("_response_id")
        self._record_step(state.execution_id, "ci_fix", response)

        action = response.get("action")
        message = response.get("message", "")

        if action == "fixed":
            state.ci_fix_count += 1
            self.ticketing.comment(
                state.issue_key,
                f"Brad attempted to fix CI failures (attempt {state.ci_fix_count}):\n\n{message}\n\nWaiting for CI to re-run..."
            )
            time.sleep(30)
            self._handle_ci_monitoring(state)
        elif action == "stuck":
            self.ticketing.comment(
                state.issue_key,
                f"Brad is stuck - could not fix CI failures:\n\n{message}\n\nFailed jobs: {', '.join(ci_result.failed_jobs)}"
            )
        else:
            self.ticketing.comment(
                state.issue_key,
                f"Brad encountered an error while fixing CI:\n\n{message}\n\nBrad is stuck."
            )

    def _handle_review_fix(self, state: IssueState, review_comments):
        """Handle PR review comments."""
        pr_number = state.pr_number
        if pr_number is None:
            self.logger.error(f"{state.issue_key}: Cannot address review comments - no PR number")
            return

        if state.review_fix_count >= self.cfg.max_review_fix_iterations:
            self.ticketing.comment(
                state.issue_key,
                f"Brad is stuck - could not address all review comments after {state.review_fix_count} attempts."
            )
            return

        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)

        response = self.agent.invoke_review_fix(
            issue_key=state.issue_key, description=state.description,
            review_comments=review_comments,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name, pr_number=pr_number,
            iteration=state.review_fix_count,
            previous_response_id=state.last_response_id,
        )
        state.last_response_id = response.get("_response_id")
        self._record_step(state.execution_id, "review_fix", response)

        action = response.get("action")
        message = response.get("message", "")

        if action == "fixed":
            state.review_fix_count += 1
            self.ticketing.comment(
                state.issue_key,
                f"Brad addressed review comments (attempt {state.review_fix_count}):\n\n{message}\n\nWaiting for CI to re-run..."
            )
            time.sleep(30)
            self._handle_ci_monitoring(state)
        elif action == "stuck":
            self.ticketing.comment(
                state.issue_key,
                f"Brad is stuck - could not address review comments:\n\n{message}"
            )
        else:
            self.ticketing.comment(
                state.issue_key,
                f"Brad encountered an error while addressing review comments:\n\n{message}\n\nBrad is stuck."
            )
