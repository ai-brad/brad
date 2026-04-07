"""
Brad - Autonomous AI Software Engineer
Orchestrator that manages the issue lifecycle:
requirements → implementation → local review → CI monitoring → deployment health.
"""

import re
import subprocess
import time
from typing import Dict, List, Optional
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
    cost_budget: float = 10.0


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

        # Load and cache repo dev instructions
        self._repo_dev_instructions = self._load_repo_instructions()

        self.logger.info(f"Brad orchestrator initialized (model: {cfg.azure_openai_model})")

    def _load_repo_instructions(self) -> str:
        """Read dev instructions from the target repo's well-known files and cache in DB.

        Looks for CONTRIBUTING.md, README.md, docs/DEVELOPMENT.md, etc.
        Extracts sections about development setup, running tests, environment.
        Caches the result in the DB so it persists across runs.
        """
        repo_path = self.cfg.target_repo_path
        cache_key = "dev_instructions"

        # Check if we have a cached version
        try:
            cached = db.get_repo_metadata(repo_path, cache_key)
        except Exception as e:
            # DB not initialized yet (e.g., during tests)
            self.logger.debug(f"Could not access DB for repo metadata: {e}")
            return ""

        # Also check the commit hash to invalidate cache when repo changes
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            current_commit = result.stdout.strip()[:12]
        except Exception:
            current_commit = "unknown"

        cached_commit = db.get_repo_metadata(repo_path, "dev_instructions_commit")
        if cached and cached_commit == current_commit:
            self.logger.info(f"Repo dev instructions loaded from cache ({len(cached)} chars)")
            return cached

        # Read well-known documentation files
        _DOC_FILES = [
            "CONTRIBUTING.md",
            "README.md",
            "docs/DEVELOPMENT.md",
            "docs/CONTRIBUTING.md",
            "BRAD.md",
        ]

        from pathlib import Path
        instructions_parts = []
        source_files = []
        for doc_file in _DOC_FILES:
            doc_path = Path(repo_path) / doc_file
            if doc_path.exists():
                try:
                    content = doc_path.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 5000:
                        content = content[:5000] + "\n... (truncated)"
                    instructions_parts.append(f"--- {doc_file} ---\n{content}")
                    source_files.append(doc_file)
                    self.logger.info(f"Read repo doc: {doc_file} ({len(content)} chars)")
                except Exception as e:
                    self.logger.warning(f"Failed to read {doc_file}: {e}")

        if not instructions_parts:
            self.logger.info("No dev documentation found in target repo")
            return ""

        combined = "\n\n".join(instructions_parts)

        # Cache in DB
        db.set_repo_metadata(repo_path, cache_key, combined, ", ".join(source_files))
        db.set_repo_metadata(repo_path, "dev_instructions_commit", current_commit)
        self.logger.info(f"Cached repo dev instructions ({len(combined)} chars from {source_files})")
        return combined

    def _set_phase(self, state: IssueState, phase: str, detail: str = "") -> None:
        """Update execution's current phase for live GUI display."""
        db.update_execution_phase(state.execution_id, phase, detail)
        self.logger.info(f"{state.issue_key}: Phase → {phase}" + (f" ({detail})" if detail else ""))

    def _check_cost_budget(self, state: IssueState) -> bool:
        """Check if execution has exceeded its cost budget. Returns True if over budget."""
        current_cost = db.get_execution_cost(state.execution_id)
        if current_cost >= state.cost_budget:
            self.logger.error(f"{state.issue_key}: Cost budget exceeded (${current_cost:.2f} >= ${state.cost_budget:.2f})")
            self.ticketing.comment(
                state.issue_key,
                f"Brad aborted: cost budget exceeded (${current_cost:.2f} >= ${state.cost_budget:.2f}). "
                f"This prevents runaway token spending. Please review and re-assign if needed."
            )
            db.update_execution_phase(state.execution_id, "budget_exceeded", f"${current_cost:.2f} >= ${state.cost_budget:.2f}")
            return True
        return False

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
            # Rebase any open Brad PRs that are behind main
            self.logger.info("Rebasing open Brad PRs...")
            try:
                self._rebase_open_prs()
            except Exception as e:
                self.logger.error(f"Failed to rebase PRs: {e}")

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

    def _rebase_open_prs(self):
        """Rebase all open Brad PRs that are behind main, skipping those with conflicts."""
        brad_prs = db.get_open_brad_prs()
        if not brad_prs:
            self.logger.info("No open Brad PRs to rebase")
            return

        self.logger.info(f"Checking {len(brad_prs)} Brad PRs for rebase")
        for pr_info in brad_prs:
            pr_number = pr_info["pr_number"]
            issue_key = pr_info["issue_key"]
            
            try:
                pr = self.code_repo.get_pr(pr_number)
                branch_name = pr.get('head', {}).get('ref', '')
                if not branch_name:
                    self.logger.warning(f"PR #{pr_number} ({issue_key}) has no branch name")
                    continue
                    
                result = self.repo.rebase_branch(branch_name, base_branch="main")
                if result.get("rebased"):
                    self.logger.info(f"Rebased PR #{pr_number} ({issue_key})")
                elif result.get("up_to_date"):
                    self.logger.debug(f"PR #{pr_number} ({issue_key}) already up to date")
                elif result.get("conflict"):
                    self.logger.warning(f"PR #{pr_number} ({issue_key}) has rebase conflicts, skipping")
                elif result.get("error"):
                    self.logger.warning(f"PR #{pr_number} ({issue_key}) rebase error: {result['error']}")
            except Exception as e:
                self.logger.warning(f"Failed to rebase PR #{pr_number} ({issue_key}): {e}")

    def _process_review_comments(self):
        """Check all Brad PRs for new review comments (both file-level and PR-level) and process them."""
        brad_prs = self.code_repo.get_brad_prs()
        if not brad_prs:
            return

        for pr in brad_prs:
            pr_number = pr['number']
            branch_name = pr['head']['ref']

            # Fetch all three types of comments:
            # 1. Line-specific review comments
            # 2. General review-level comments (review body, not tied to lines)
            # 3. Issue comments (PR-level general comments)
            review_comments = self.code_repo.get_review_comments_needing_response(pr_number)
            review_level_comments = self.code_repo.get_review_level_comments_needing_response(pr_number)
            issue_comments = self.code_repo.get_issue_comments_needing_response(pr_number)
            
            total_comments = len(review_comments) + len(review_level_comments) + len(issue_comments)
            if total_comments == 0:
                continue

            self.logger.info(f"Found {len(review_comments)} review comments, {len(review_level_comments)} review-level comments, and {len(issue_comments)} issue comments on PR #{pr_number}")

            # Fail-fast: check branch existence ONCE before processing any comments
            try:
                self.repo.checkout_branch(branch_name)
                self.repo._run_git("fetch", "origin", branch_name, check=False)
                self.repo._run_git("reset", "--hard", f"origin/{branch_name}", check=False)
            except Exception as e:
                self.logger.warning(f"PR #{pr_number}: Skipping all {total_comments} comments — branch '{branch_name}' unavailable: {e}")
                continue

            # Process review comments (file/line-specific)
            if review_comments:
                self._process_review_comments_batch(pr_number, branch_name, review_comments)
            
            # Process review-level comments (general review body, not tied to lines)
            if review_level_comments:
                self._process_issue_comments_batch(pr_number, branch_name, review_level_comments)
            
            # Process issue comments (PR-level general comments)
            if issue_comments:
                self._process_issue_comments_batch(pr_number, branch_name, issue_comments)

    def _process_review_comments_batch(self, pr_number: int, branch_name: str, comments: List[Dict]):
        """Process all review comments for a PR in a single LLM call."""
        # Claim all comments first
        for comment in comments:
            try:
                self.code_repo.reply_to_review_comment(pr_number, comment['id'], "Brad reaction: checking...")
            except Exception as e:
                self.logger.warning(f"Failed to claim comment {comment['id']}: {e}")

        # Enrich comments with their reply threads for full context
        enriched_comments = []
        for comment in comments:
            replies = self.code_repo.get_comment_replies(pr_number, comment['id'])
            # Filter out Brad's own "checking..." and final response messages from context
            non_brad_replies = [r for r in replies if not (
                r.get('body', '').startswith('Brad reaction: ') or 
                r.get('body', '').startswith('Brad checking')
            )]
            enriched = comment.copy()
            enriched['_thread_replies'] = non_brad_replies
            enriched_comments.append(enriched)

        comment_results, overall_code_changed = self._invoke_and_verify_batch(
            pr_number, branch_name, enriched_comments,
        )

        # Post replies for each comment
        for cr in comment_results:
            comment_id = cr.get('comment_id')
            action = cr.get('action')
            reply_text = cr.get('reply', '')

            if not comment_id:
                continue

            if action == 'code_changed':
                prefixed = f"Brad reaction: {reply_text}" if reply_text else "Brad reaction: Fixed in latest push."
                self.code_repo.reply_to_review_comment(pr_number, comment_id, prefixed)
            elif action == 'replied':
                prefixed = f"Brad reaction: {reply_text}" if reply_text else "Brad reaction: Acknowledged."
                self.code_repo.reply_to_review_comment(pr_number, comment_id, prefixed)
            elif action == 'error':
                self.logger.error(f"Comment {comment_id}: {cr.get('message', 'Unknown error')}")
            else:
                prefixed = f"Brad reaction: {reply_text}" if reply_text else "Brad reaction: Acknowledged."
                self.code_repo.reply_to_review_comment(pr_number, comment_id, prefixed)

        # Push once if verified changes exist
        if overall_code_changed:
            try:
                self.repo.push(branch_name, force=False)
            except Exception:
                try:
                    self.repo.push(branch_name, force=True)
                except Exception as e:
                    self.logger.error(f"Failed to push changes for PR #{pr_number}: {e}")

    def _get_pr_diff(self, branch_name: str) -> str:
        """Get the diff of the current branch against main."""
        try:
            result = self.repo._run_git("diff", "origin/main...HEAD", "--stat", "-p", check=False)
            return result.stdout if result.stdout else ""
        except Exception as e:
            self.logger.warning(f"Failed to get PR diff for {branch_name}: {e}")
            return ""

    def _invoke_and_verify_batch(self, pr_number: int, branch_name: str, comments: List[Dict], attempt: int = 1) -> tuple:
        """Invoke batch review reader and verify actual changes. Retry once if agent hallucinates CODE_CHANGED."""
        MAX_ATTEMPTS = 2

        # Get PR diff to give agent full context of what changed
        pr_diff = self._get_pr_diff(branch_name) if attempt == 1 else ""

        result = self.agent.invoke_code_review_reader_batch(
            pr_number=pr_number,
            branch_name=branch_name,
            comments=comments,
            repo_path=str(self.repo.repo_path),
            dev_instructions=self._repo_dev_instructions,
            pr_diff=pr_diff,
        )

        comment_results = result.get('comment_results', [])
        overall_code_changed = any(cr.get('action') == 'code_changed' for cr in comment_results)

        if overall_code_changed:
            has_actual = self._has_uncommitted_or_new_commits(branch_name)
            if not has_actual and attempt < MAX_ATTEMPTS:
                self.logger.warning(
                    f"PR #{pr_number}: Agent claimed CODE_CHANGED on attempt {attempt} but no changes found. "
                    f"Retrying with explicit instructions..."
                )
                # Inject a retry hint into comments so the prompt changes
                for c in comments:
                    c['_retry_hint'] = (
                        "PREVIOUS ATTEMPT FAILED: You said CODE_CHANGED but made NO actual file edits. "
                        "You MUST use edit_file or write_file tools to ACTUALLY modify files before returning CODE_CHANGED. "
                        "If you return CODE_CHANGED again without using edit_file/write_file, it will be detected as a failure."
                    )
                return self._invoke_and_verify_batch(pr_number, branch_name, comments, attempt + 1)
            elif not has_actual:
                self.logger.error(f"PR #{pr_number}: Agent claimed CODE_CHANGED after {attempt} attempts but still no changes!")
                overall_code_changed = False

        return comment_results, overall_code_changed

    def _has_uncommitted_or_new_commits(self, branch_name: str) -> bool:
        """Check if there are uncommitted changes or new commits not yet pushed.
        If uncommitted changes exist, auto-commit them (agent may have forgotten)."""
        try:
            # Check for uncommitted changes
            result = self.repo._run_git("status", "--porcelain")
            if result.stdout.strip():
                self.logger.info(f"Found uncommitted changes on {branch_name}, auto-committing...")
                self.repo._run_git("add", "-A")
                self.repo._run_git("commit", "-m", f"Brad: address review comments on {branch_name}")
                return True
            # Check for unpushed commits
            result = self.repo._run_git("log", f"origin/{branch_name}..HEAD", "--oneline", check=False)
            if result.stdout.strip():
                return True
            return False
        except Exception as e:
            self.logger.warning(f"Error checking for changes on {branch_name}: {e}")
            return False

    def _process_issue_comments_batch(self, pr_number: int, branch_name: str, comments: List[Dict]):
        """Process all PR-level issue comments in a single LLM call."""
        # Claim all comments first by replying
        for comment in comments:
            try:
                self.code_repo.reply_to_issue_comment(pr_number, comment['id'], "Brad checking...")
            except Exception as e:
                self.logger.warning(f"Failed to claim issue comment {comment['id']}: {e}")

        # Convert issue comments to the same format as review comments for batched processing
        # Issue comments don't have file/line context, but we can still batch them
        enriched_comments = []
        for comment in comments:
            enriched = comment.copy()
            enriched['path'] = 'N/A'
            enriched['line'] = 'N/A'
            enriched['diff_hunk'] = ''
            enriched['_thread_replies'] = []
            enriched_comments.append(enriched)

        comment_results, overall_code_changed = self._invoke_and_verify_batch(
            pr_number, branch_name, enriched_comments,
        )

        # Post replies for each comment
        for cr in comment_results:
            comment_id = cr.get('comment_id')
            action = cr.get('action')
            reply_text = cr.get('reply', '')

            if not comment_id:
                continue

            if action == 'code_changed':
                prefixed = f"Brad reaction: {reply_text}" if reply_text else "Brad reaction: Fixed in latest push."
                self.code_repo.reply_to_issue_comment(pr_number, comment_id, prefixed)
            elif action == 'replied':
                prefixed = f"Brad reaction: {reply_text}" if reply_text else "Brad reaction: Acknowledged."
                self.code_repo.reply_to_issue_comment(pr_number, comment_id, prefixed)
            elif action == 'error':
                self.logger.error(f"Issue comment {comment_id}: {cr.get('message', 'Unknown error')}")
            else:
                prefixed = f"Brad reaction: {reply_text}" if reply_text else "Brad reaction: Acknowledged."
                self.code_repo.reply_to_issue_comment(pr_number, comment_id, prefixed)

        # Push once if verified changes exist
        if overall_code_changed:
            try:
                self.repo.push(branch_name, force=False)
            except Exception:
                try:
                    self.repo.push(branch_name, force=True)
                except Exception as e:
                    self.logger.error(f"Failed to push changes for PR #{pr_number}: {e}")

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
            db.update_execution_phase(execution_id, "initializing", "Preparing to process issue")

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
                cost_budget=float(self.cfg.__dict__.get("cost_budget")),
            )

            # Step 4: Close stale PRs from previous executions of the same issue
            self._set_phase(state, "closing_stale_prs", "Checking for old PRs to close")
            self._close_stale_prs(state)

            # Step 5: If branch already exists remotely, clean up for fresh start
            if self.repo.branch_exists_remote(branch_name):
                try:
                    self.repo._run_git("push", "origin", "--delete", branch_name, check=False)
                except Exception as e:
                    self.logger.warning(f"{issue_key}: Could not delete remote branch: {e}")

            self._handle_implementation_phase(state)

            # Finish execution with status reflecting actual outcome
            final_status = "completed" if state.pr_number else "stuck"
            db.update_execution_phase(execution_id, "done" if final_status == "completed" else "stuck")
            db.finish_execution(
                execution_id,
                status=final_status,
                pr_number=state.pr_number,
                pr_url=f"https://github.com/{self.cfg.github_repo}/pull/{state.pr_number}" if state.pr_number else None,
                error_message="Brad could not create a PR" if not state.pr_number else None,
            )

        except Exception as e:
            db.update_execution_phase(execution_id, "error", str(e)[:200])
            db.finish_execution(execution_id, status="error", error_message=str(e)[:500])
            raise

    def _close_stale_prs(self, state: IssueState):
        """Close any open PRs from previous executions of the same JIRA issue."""
        try:
            existing_pr = self.code_repo.pr_exists_for_branch(state.branch_name)
            if existing_pr:
                self.logger.info(f"{state.issue_key}: Closing stale PR #{existing_pr} from previous execution")
                self.code_repo.close_pr(
                    existing_pr,
                    f"Superseded by execution #{state.execution_id}. Brad is re-processing {state.issue_key}."
                )
        except Exception as e:
            self.logger.warning(f"{state.issue_key}: Could not close stale PR: {e}")

    def _handle_requirements_phase(self, state: IssueState):
        """Handle requirements analysis phase."""
        self._set_phase(state, "reading_requirements", "Analyzing issue requirements")
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
        """Handle implementation phase — impl → local review loop → then create PR → CI."""
        self._set_phase(state, "implementing", "Writing code and tests")
        self.ticketing.comment(state.issue_key, f"Brad is starting implementation for {state.issue_key}...")

        self.repo.reset_to_clean_state("main")

        if not self.repo.branch_exists_remote(state.branch_name):
            self.repo.prepare_branch(state.branch_name, base_branch="main")
        else:
            self.repo.checkout_branch(state.branch_name, create_if_missing=False)
            self.repo._run_git("reset", "--hard", f"origin/{state.branch_name}")

        step_id = db.create_step(state.execution_id, "implementation", "Running LLM implementation agent")
        response = self.agent.invoke_implementation(
            issue_key=state.issue_key, description=state.description,
            attachment_paths=state.attachment_paths,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name, iteration=0,
            previous_response_id=state.last_response_id,
            dev_instructions=self._repo_dev_instructions,
        )
        state.last_response_id = response.get("_response_id")

        # Record step costs
        usage = response.get("_usage")
        cost = self._calculate_cost(usage)
        pt = usage.prompt_tokens if usage else 0
        ct = usage.completion_tokens if usage else 0
        db.finish_step(step_id, status=response.get("action", "unknown"), prompt_tokens=pt, completion_tokens=ct, cost=cost, result_summary=response.get("message", "")[:500])
        db.update_execution_costs(state.execution_id, pt, ct, cost)

        action = response.get("action")
        message = response.get("message", "")

        if action != "success":
            if action == "stuck":
                self._set_phase(state, "stuck", "Implementation could not complete")
                self.ticketing.comment(state.issue_key, f"Brad is stuck during implementation:\n\n{message}")
            elif action == "in_progress":
                # Agent returned without completing - this means it didn't create a PR yet
                # Treat this as stuck since we don't have multi-turn implementation support
                self._set_phase(state, "stuck", "Implementation incomplete - no PR created")
                self.ticketing.comment(state.issue_key, f"Brad did not complete the implementation. No PR was created.\n\nLast message:\n{message}\n\nThis likely means Brad finished early without actually modifying code. Please review the logs.")
            else:
                self._set_phase(state, "error", "Implementation error")
                self.ticketing.comment(state.issue_key, f"Brad encountered an error during implementation:\n\n{message}\n\nBrad is stuck.")
            return

        if self._check_cost_budget(state):
            return

        # Implementation succeeded — run local review BEFORE creating PR
        self._set_phase(state, "local_review", "Reviewing code locally before creating PR")
        self.ticketing.comment(state.issue_key, "Implementation complete. Running local code review before creating PR...")
        local_review_passed = self._run_local_review_loop(state)

        # Always verify/create PR and persist to DB (even if budget is tight)
        self._set_phase(state, "creating_pr", "Pushing code and creating pull request")
        verified_pr = self._verify_and_ensure_pr(state, response.get("pr_number"), response.get("pr_url"))
        if verified_pr:
            pr_number, pr_url = verified_pr
            state.pr_number = pr_number
            db.update_execution_pr(state.execution_id, pr_number, pr_url)

            # Update PR body with execution metadata
            self._update_pr_metadata(state, pr_number)

            self.ticketing.comment(
                state.issue_key,
                f"Brad created PR #{pr_number}:\n{pr_url}\n\n"
                f"Local review: {'PASSED' if local_review_passed else 'completed with caveats'}."
            )

            if self._check_cost_budget(state):
                return

            self._set_phase(state, "ci_monitoring", f"Waiting for CI on PR #{pr_number}")
            self._handle_ci_monitoring(state)
        else:
            self._set_phase(state, "stuck", "Could not create PR")
            self.ticketing.comment(state.issue_key, "Brad completed implementation but failed to create PR. Manual intervention needed.")

    def _update_pr_metadata(self, state: IssueState, pr_number: int):
        """Update PR body with execution metadata for traceability."""
        try:
            exec_data = db.get_execution(state.execution_id)
            body = (
                f"## Brad Execution #{state.execution_id}\n\n"
                f"- **JIRA Issue:** [{state.issue_key}]({self.cfg.jira_url}/browse/{state.issue_key})\n"
                f"- **Execution ID:** {state.execution_id}\n"
                f"- **Started:** {exec_data.get('started_at', 'N/A') if exec_data else 'N/A'}\n"
                f"- **Cost so far:** ${exec_data.get('total_cost', 0):.4f}\n"
                f"\n---\n*Automated by Brad — Execution #{state.execution_id}*"
            )
            self.code_repo.update_pr_body(pr_number, body)
        except Exception as e:
            self.logger.warning(f"{state.issue_key}: Could not update PR metadata: {e}")

    def _run_local_review_loop(self, state: IssueState) -> bool:
        """Run impl → local review → fix loop. Returns True if review passed."""
        review_result = self._handle_local_review(state)

        if review_result.get("action") == "approved":
            return True

        if review_result.get("action") == "changes_requested":
            return self._handle_local_review_fix(state, review_result.get("message", ""))

        # Review errored or unknown — proceed anyway
        return False

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
        self._set_phase(state, "local_review", "Running automated code review")
        step_id = db.create_step(state.execution_id, "local_review", "Generating diff and reviewing")
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
        self._set_phase(state, "local_review_fix", f"Addressing review feedback (attempt {state.local_review_fix_count + 1})")
        if state.local_review_fix_count >= self.cfg.max_review_fix_iterations:
            self.ticketing.comment(
                state.issue_key,
                f"Brad is stuck - could not address local review feedback after {state.local_review_fix_count} attempts."
            )
            return False

        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)

        step_id = db.create_step(state.execution_id, "local_review_fix", f"Fixing review feedback (attempt {state.local_review_fix_count + 1})", iteration=state.local_review_fix_count)
        response = self.agent.invoke_local_review_fix(
            issue_key=state.issue_key, description=state.description,
            review_feedback=review_feedback,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name,
            iteration=state.local_review_fix_count,
            previous_response_id=state.last_response_id,
            dev_instructions=self._repo_dev_instructions,
        )
        state.last_response_id = response.get("_response_id")

        # Record step costs
        usage = response.get("_usage")
        cost = self._calculate_cost(usage)
        pt = usage.prompt_tokens if usage else 0
        ct = usage.completion_tokens if usage else 0
        db.finish_step(step_id, status=response.get("action", "unknown"), prompt_tokens=pt, completion_tokens=ct, cost=cost, result_summary=response.get("message", "")[:500])
        db.update_execution_costs(state.execution_id, pt, ct, cost)

        action = response.get("action")
        message = response.get("message", "")

        if action == "fixed":
            state.local_review_fix_count += 1
            self.ticketing.comment(
                state.issue_key,
                f"Brad addressed local review feedback (attempt {state.local_review_fix_count}):\n\n{message}\n\nRe-running local review..."
            )

            if self._check_cost_budget(state):
                return False

            review_result = self._handle_local_review(state)
            if review_result.get("action") == "approved":
                return True
            if review_result.get("action") == "changes_requested":
                state.local_review_fix_count += 1
                return self._handle_local_review_fix(state, review_result.get("message", ""))

            # Review errored — treat as passed with caveats
            return False
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

        self._set_phase(state, "ci_monitoring", f"Waiting for CI on PR #{pr_number}")
        step_id = db.create_step(state.execution_id, "ci_monitoring", f"Monitoring CI for PR #{pr_number}")

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
            self._set_phase(state, "ci_passed", f"CI passed for PR #{pr_number}")
            deployment_summary = ""
            if self.cfg.deployment_health_check and deploy_info:
                self._set_phase(state, "deployment_health_check", "Checking deployment health")
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
                self._set_phase(state, "addressing_review_comments", f"{len(review_comments)} review comments to address")
                self.ticketing.comment(
                    state.issue_key,
                    f"CI passed, but there are {len(review_comments)} review comments to address. Brad is working on them..."
                )
                self._handle_review_fix(state, review_comments)
            else:
                self._set_phase(state, "done", "All CI checks passed, no review comments")
                try:
                    self.ticketing.set_status(state.issue_key, "REVIEW")
                except Exception as e:
                    self.logger.warning(f"Could not set status to REVIEW: {e}")

                done_msg = "Brad is done. All CI checks passed and no review comments to address."
                if deployment_summary:
                    done_msg += f"\n\n{deployment_summary}"
                self.ticketing.comment(state.issue_key, done_msg)
        else:
            self._set_phase(state, "ci_failed", f"CI failed: {', '.join(ci_result.failed_jobs)}"[:200])
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

        if self._check_cost_budget(state):
            return

        self._set_phase(state, "ci_fix", f"Fixing CI failures (attempt {state.ci_fix_count + 1})")

        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)

        failed_test_target = extract_failed_tests_from_ci_logs(ci_result.logs)

        step_id = db.create_step(state.execution_id, "ci_fix", f"Fixing CI: {', '.join(ci_result.failed_jobs)}"[:200], iteration=state.ci_fix_count)
        response = self.agent.invoke_ci_fix(
            issue_key=state.issue_key, description=state.description,
            ci_logs=ci_result.logs, failed_jobs=ci_result.failed_jobs,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name, pr_number=pr_number,
            iteration=state.ci_fix_count,
            failed_test_target=failed_test_target,
            previous_response_id=state.last_response_id,
            dev_instructions=self._repo_dev_instructions,
        )
        state.last_response_id = response.get("_response_id")

        # Record step costs
        usage = response.get("_usage")
        cost_val = self._calculate_cost(usage)
        pt = usage.prompt_tokens if usage else 0
        ct = usage.completion_tokens if usage else 0
        db.finish_step(step_id, status=response.get("action", "unknown"), prompt_tokens=pt, completion_tokens=ct, cost=cost_val, result_summary=response.get("message", "")[:500])
        db.update_execution_costs(state.execution_id, pt, ct, cost_val)

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

        if self._check_cost_budget(state):
            return

        self._set_phase(state, "review_fix", f"Addressing {len(review_comments)} review comments (attempt {state.review_fix_count + 1})")

        if state.review_fix_count >= self.cfg.max_review_fix_iterations:
            self.ticketing.comment(
                state.issue_key,
                f"Brad is stuck - could not address all review comments after {state.review_fix_count} attempts."
            )
            return

        self.repo.checkout_branch(state.branch_name, create_if_missing=False)
        self.repo._run_git("pull", "origin", state.branch_name)

        step_id = db.create_step(state.execution_id, "review_fix", f"Addressing {len(review_comments)} review comments", iteration=state.review_fix_count)
        response = self.agent.invoke_review_fix(
            issue_key=state.issue_key, description=state.description,
            review_comments=review_comments,
            repo_path=str(self.repo.repo_path),
            branch_name=state.branch_name, pr_number=pr_number,
            iteration=state.review_fix_count,
            previous_response_id=state.last_response_id,
            dev_instructions=self._repo_dev_instructions,
        )
        state.last_response_id = response.get("_response_id")

        # Record step costs
        usage = response.get("_usage")
        cost_val = self._calculate_cost(usage)
        pt = usage.prompt_tokens if usage else 0
        ct = usage.completion_tokens if usage else 0
        db.finish_step(step_id, status=response.get("action", "unknown"), prompt_tokens=pt, completion_tokens=ct, cost=cost_val, result_summary=response.get("message", "")[:500])
        db.update_execution_costs(state.execution_id, pt, ct, cost_val)

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
