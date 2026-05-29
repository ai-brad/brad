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
from brad.conflict_context import extract_jira_key
from brad.adapters.ticketing import build_ticketing_adapter
from brad.adapters.code_repository import build_code_repo_adapter
from brad.adapters.ci_cd import build_ci_adapter
from brad.adapters.observability.azure_adapter import AzureObservabilityAdapter
from brad.adapters.harness import build_harness
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
    continuation_summary: str = ""
    pr_number: Optional[int] = None
    clarification_count: int = 0
    ci_fix_count: int = 0
    review_fix_count: int = 0
    local_review_fix_count: int = 0
    cost_budget: float = 150.0
    last_failure_detail: str = ""
    # Resolved model name for this execution (from BradLight/BradHeavy label).
    model: Optional[str] = None


RESULT_SUMMARY_LIMIT = 4000


def _clip_summary(text: object, limit: int = RESULT_SUMMARY_LIMIT) -> str:
    return str(text or "")[:limit]


class BradOrchestrator:
    """
    Brad orchestrator. Manages the issue lifecycle:
    requirements → implementation → local review → CI monitoring → deployment health.
    """

    def __init__(self, cfg: Config):
        self.logger = get_logger(__name__)
        self.cfg = cfg

        # Initialize adapters
        self.ticketing = build_ticketing_adapter(cfg)
        self.code_repo = build_code_repo_adapter(cfg)
        self.ci = build_ci_adapter(cfg)
        self.observability = AzureObservabilityAdapter(cfg)
        harness = build_harness(cfg)
        self.agent = AIAgentInterface(harness, cfg)
        self.repo = RepoManager(cfg)

        # Model identity for cache keys
        self._model_identity = f"{cfg.azure_openai_endpoint}|{cfg.azure_openai_model}"

        # Load and cache repo dev instructions
        self._repo_dev_instructions = self._load_repo_instructions()

        self.logger.info(f"Brad orchestrator initialized (model: {cfg.azure_openai_model})")

    def _execution_model_name(self) -> str:
        return getattr(self.agent.harness, "model_name", None) or self.cfg.azure_openai_model

    @staticmethod
    def _maybe_text(value) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _canonical_issue_key(issue_key: str) -> str:
        extracted = extract_jira_key(issue_key or "")
        return extracted or (issue_key or "").strip()

    def _summarization_model_name(self) -> str:
        harness = self.agent.harness
        return (
            getattr(harness, "_resolved_summarization_model_name", None)
            or getattr(harness, "model_name", None)
            or self.cfg.azure_openai_model
        )

    def _summary_usage_metrics(self, usage) -> Dict[str, float]:
        if not usage:
            return {
                "prompt_tokens": 0,
                "cached_prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost": 0.0,
            }

        model_name = self._summarization_model_name()
        try:
            costs = db.get_model_cost(model_name)
        except Exception as e:
            self.logger.debug(f"Could not load model cost for summary accounting: {e}")
            costs = {"prompt": 0.0, "cached_prompt": 0.0, "completion": 0.0}

        def _coerce_int(value) -> int:
            if isinstance(value, bool) or value is None:
                return 0
            if isinstance(value, (int, float)):
                return int(value)
            try:
                return int(value)
            except Exception:
                return 0

        cached = _coerce_int(getattr(usage, "cached_tokens", 0))
        prompt_tokens = _coerce_int(getattr(usage, "prompt_tokens", 0))
        completion_tokens = _coerce_int(getattr(usage, "completion_tokens", 0))
        non_cached_prompt = max(0, prompt_tokens - cached)
        prompt_cost = (non_cached_prompt / 1000.0) * float(costs.get("prompt", 0.0) or 0.0)
        cached_rate = float(costs.get("cached_prompt", (costs.get("prompt", 0.0) or 0.0) * 0.5) or 0.0)
        cached_cost = (cached / 1000.0) * cached_rate
        completion_cost = (completion_tokens / 1000.0) * float(costs.get("completion", 0.0) or 0.0)
        return {
            "prompt_tokens": prompt_tokens,
            "cached_prompt_tokens": cached,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost": prompt_cost + cached_cost + completion_cost,
        }

    def _persist_issue_continuation_summary(
        self,
        issue_key: str,
        summary: str,
        *,
        source: str,
        execution_id: Optional[int] = None,
        usage=None,
        metrics: Optional[Dict] = None,
        model_name: Optional[str] = None,
    ) -> None:
        issue_key = self._canonical_issue_key(issue_key)
        metrics = metrics or self._summary_usage_metrics(usage)
        model_name = (model_name or self._summarization_model_name()).strip()
        try:
            db.set_issue_context_summary(
                str(self.repo.repo_path),
                issue_key,
                summary,
                source=source,
                model_name=model_name,
                execution_id=execution_id,
                prompt_tokens=int(metrics["prompt_tokens"]),
                cached_prompt_tokens=int(metrics["cached_prompt_tokens"]),
                completion_tokens=int(metrics["completion_tokens"]),
                cost=float(metrics["cost"]),
            )
        except Exception as e:
            self.logger.debug(f"Could not persist continuation summary for {issue_key}: {e}")

        if execution_id:
            try:
                db.update_execution_continuation_summary(
                    execution_id,
                    summary,
                    source=source,
                    model_name=model_name,
                    prompt_tokens=int(metrics["prompt_tokens"]),
                    cached_prompt_tokens=int(metrics["cached_prompt_tokens"]),
                    completion_tokens=int(metrics["completion_tokens"]),
                    cost=float(metrics["cost"]),
                )
            except Exception as e:
                self.logger.debug(f"Could not persist execution summary metadata for {issue_key}: {e}")

    def _get_issue_continuation_summary_record(self, issue_key: str) -> Dict:
        issue_key = self._canonical_issue_key(issue_key)
        try:
            stored = db.get_issue_context_summary(str(self.repo.repo_path), issue_key)
        except Exception as e:
            self.logger.debug(f"Could not read continuation summary for {issue_key}: {e}")
            stored = None
        return stored or {}

    def _load_issue_continuation_summary(self, issue_key: str) -> str:
        issue_key = self._canonical_issue_key(issue_key)
        stored = self._get_issue_continuation_summary_record(issue_key)
        summary = self._maybe_text(stored.get("summary"))
        return summary

    def _compact_issue_context(
        self,
        issue_key: str,
        seed_text: str,
        source: str,
        execution_id: Optional[int] = None,
    ) -> str:
        issue_key = self._canonical_issue_key(issue_key)
        seed_text = (seed_text or "").strip()
        if not seed_text:
            return ""

        summary = ""
        harness = self.agent.harness
        summarize = getattr(harness, "summarize_context", None)
        if callable(summarize):
            try:
                summary = summarize(seed_text, str(self.repo.repo_path), subject=issue_key).strip()
            except Exception as e:
                self.logger.warning(f"{issue_key}: continuation summarization failed: {e}")

        if not summary:
            summary = seed_text[:3000]

        usage = getattr(harness, "_last_summary_usage", None)
        self._persist_issue_continuation_summary(
            issue_key,
            summary,
            source=source,
            execution_id=execution_id,
            usage=usage,
        )
        return summary

    def _build_issue_continuation_summary(
        self,
        issue_key: str,
        execution_id: Optional[int] = None,
    ) -> str:
        issue_key = self._canonical_issue_key(issue_key)
        stored_record = self._get_issue_continuation_summary_record(issue_key)
        stored = self._maybe_text(stored_record.get("summary"))
        if stored:
            if execution_id:
                self._persist_issue_continuation_summary(
                    issue_key,
                    stored,
                    source=self._maybe_text(stored_record.get("source")) or "stored",
                    execution_id=execution_id,
                    model_name=self._maybe_text(stored_record.get("model_name")) or None,
                    metrics={
                        "prompt_tokens": int(stored_record.get("prompt_tokens") or 0),
                        "cached_prompt_tokens": int(stored_record.get("cached_prompt_tokens") or 0),
                        "completion_tokens": int(stored_record.get("completion_tokens") or 0),
                        "total_tokens": int(stored_record.get("total_tokens") or 0),
                        "cost": float(stored_record.get("cost") or 0.0),
                    },
                )
            return stored
        seed = self._summarize_prior_activity(issue_key)
        if not seed:
            return ""
        return self._compact_issue_context(issue_key, seed, source="prior_activity", execution_id=execution_id)

    def _refresh_issue_continuation_summary(
        self,
        issue_key: str,
        response: Dict,
        source: str,
        execution_id: Optional[int] = None,
    ) -> str:
        issue_key = self._canonical_issue_key(issue_key)
        current = self._load_issue_continuation_summary(issue_key)
        note = self._maybe_text(response.get("summary")) or self._maybe_text(response.get("message"))
        seed_parts = [part for part in (current, note) if part]
        seed = "\n\n".join(seed_parts).strip()
        if not seed:
            if current and execution_id:
                stored_record = self._get_issue_continuation_summary_record(issue_key)
                self._persist_issue_continuation_summary(
                    issue_key,
                    current,
                    source=self._maybe_text(stored_record.get("source")) or source,
                    execution_id=execution_id,
                    model_name=self._maybe_text(stored_record.get("model_name")) or None,
                    metrics={
                        "prompt_tokens": int(stored_record.get("prompt_tokens") or 0),
                        "cached_prompt_tokens": int(stored_record.get("cached_prompt_tokens") or 0),
                        "completion_tokens": int(stored_record.get("completion_tokens") or 0),
                        "total_tokens": int(stored_record.get("total_tokens") or 0),
                        "cost": float(stored_record.get("cost") or 0.0),
                    },
                )
            return current
        return self._compact_issue_context(issue_key, seed, source=source, execution_id=execution_id)

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

    def _stop_requested(self, issue_key: Optional[str] = None) -> bool:
        try:
            result = db.is_brad_stopped(issue_key)
            return result if isinstance(result, bool) else False
        except Exception as e:
            self.logger.debug(f"Could not read Brad control state: {e}")
            return False

    def _raise_if_stopped(self) -> None:
        if self._stop_requested():
            self.logger.info("Brad stop requested; aborting current work")
            raise KeyboardInterrupt()

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
        Cached prompt tokens use a separate rate when known."""
        if not usage:
            return 0.0
        costs = db.get_model_cost(self.agent.harness.model_name)
        cached = getattr(usage, "cached_tokens", 0) or 0
        non_cached_prompt = max(0, usage.prompt_tokens - cached)
        prompt_cost = (non_cached_prompt / 1000.0) * costs["prompt"]
        cached_rate = costs.get("cached_prompt", costs["prompt"] * 0.5)
        cached_cost = (cached / 1000.0) * cached_rate
        completion_cost = (usage.completion_tokens / 1000.0) * costs["completion"]
        return prompt_cost + cached_cost + completion_cost

    @staticmethod
    def _usage_totals(usage):
        if not usage:
            return 0, 0, 0
        return (
            usage.prompt_tokens or 0,
            usage.completion_tokens or 0,
            getattr(usage, "cached_tokens", 0) or 0,
        )

    def _record_step(self, execution_id: int, phase: str, response: Dict) -> None:
        """Record a step with cost/usage data in the database."""
        usage = response.get("_usage")
        cost = self._calculate_cost(usage)
        prompt_tokens, completion_tokens, cached_prompt_tokens = self._usage_totals(usage)

        step_id = db.create_step(execution_id, phase)
        db.finish_step(
            step_id,
            status=response.get("action", "unknown"),
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            result_summary=_clip_summary(response.get("message", "")),
        )
        db.update_execution_costs(
            execution_id,
            prompt_tokens,
            completion_tokens,
            cost,
            cached_prompt_tokens=cached_prompt_tokens,
        )

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

            # Then fetch issues with BradLight or BradHeavy label
            seen_keys: set = set()
            issues = []
            for label in ("BradLight", "BradHeavy"):
                for issue in self.ticketing.fetch_issues_with_label(label):
                    if issue["key"] not in seen_keys:
                        seen_keys.add(issue["key"])
                        issues.append(issue)

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
        brad_prs = self.code_repo.get_brad_prs()
        if not brad_prs:
            self.logger.info("No open Brad PRs to rebase")
            return

        # Ensure the workspace is clean before iterating. Leftover edits from a
        # previous run/agent invocation will otherwise make `git checkout` abort
        # with "local changes would be overwritten", silently skipping rebases
        # for every PR after the first dirty one.
        try:
            self.repo.reset_to_clean_state("main")
        except Exception as e:
            self.logger.warning(f"Could not pre-clean workspace before rebase pass: {e}")

        self.logger.info(f"Checking {len(brad_prs)} Brad PRs for rebase")
        for pr in brad_prs:
            pr_number = pr['number']
            branch_name = pr.get('head', {}).get('ref', '')

            if db.is_brad_stopped(branch_name):
                self.logger.info(f"PR #{pr_number} ({branch_name}) is stopped; skipping rebase")
                continue

            try:
                if not branch_name:
                    raise ValueError(f"PR #{pr_number} has no branch name")

                # Re-check PR state right before rebasing. The brad_prs list is
                # a snapshot from the start of the rebase pass, but a long
                # rebase loop (especially with AI conflict resolution) can take
                # many minutes during which an earlier PR may get merged or
                # closed. Without this guard, brad happily rebases a merged
                # branch against the new main (which now contains the merged
                # work), producing huge spurious conflicts and burning tokens.
                try:
                    fresh = self.code_repo.get_pr(pr_number)
                    if fresh.get("state") != "open" or fresh.get("merged_at"):
                        self.logger.info(
                            f"PR #{pr_number} ({branch_name}) is no longer open "
                            f"(state={fresh.get('state')}, merged_at={fresh.get('merged_at')}); "
                            f"skipping rebase"
                        )
                        continue
                except Exception as e:
                    self.logger.warning(
                        f"PR #{pr_number}: could not refresh state before rebase: {e}; "
                        f"proceeding with stale snapshot"
                    )

                # Defensive: if a prior iteration left the tree dirty (e.g. a
                # half-applied rebase the abort didn't fully undo), reset before
                # trying the next branch so one bad PR can't block the others.
                if not self.repo.is_clean_working_tree():
                    self.logger.warning(
                        f"Workspace dirty before rebasing PR #{pr_number}, resetting to main"
                    )
                    self.repo.reset_to_clean_state("main")

                result = self.repo.rebase_branch(
                    branch_name, base_branch="main", auto_abort_on_conflict=False,
                )
                if result.get("rebased"):
                    self.logger.info(f"Rebased PR #{pr_number} ({branch_name})")
                    # Push happened inside rebase_branch on the no-conflict
                    # path; treat the post-push CI run the same as any other
                    # change so test failures auto-route into _handle_ci_fix.
                    try:
                        self._watch_ci_after_push(pr_number, branch_name)
                    except Exception as e:
                        self.logger.warning(
                            f"PR #{pr_number}: post-rebase CI watch failed: {e}"
                        )
                elif result.get("up_to_date"):
                    self.logger.debug(f"PR #{pr_number} ({branch_name}) already up to date")
                elif result.get("conflict"):
                    conflicted = result.get("conflicted_files") or []
                    self.logger.warning(
                        f"PR #{pr_number} ({branch_name}) has rebase conflicts in "
                        f"{len(conflicted)} file(s); attempting AI resolution"
                    )
                    self._resolve_rebase_conflicts(
                        pr_number=pr_number,
                        branch_name=branch_name,
                        base_branch=result.get("base_branch", "main"),
                        conflicted_files=conflicted,
                    )
                elif result.get("error"):
                    self.logger.warning(f"PR #{pr_number} ({branch_name}) rebase error: {result['error']}")
            except Exception as e:
                self.logger.warning(f"Failed to rebase PR #{pr_number} ({branch_name}): {e}")
                # Make sure we don't leave a half-applied rebase behind for
                # the next PR.
                try:
                    self.repo.abort_rebase()
                except Exception:
                    pass

    # -------------------------
    # Rebase conflict resolution
    # -------------------------

    MAX_CONFLICT_FILES = 20
    MAX_CONFLICT_ITERATIONS = 5

    def _resolve_rebase_conflicts(
        self,
        pr_number: int,
        branch_name: str,
        base_branch: str,
        conflicted_files: list,
    ) -> None:
        """Drive AI-only resolution of an in-progress rebase conflict.

        Loops up to ``MAX_CONFLICT_ITERATIONS`` times: gathers context, asks
        the agent to resolve, verifies markers are gone, runs ``git rebase
        --continue``. On success, force-pushes (with lease) to the feature
        branch and hands the PR off to the existing CI fix loop. On failure,
        aborts the rebase and posts a diagnostic comment on the PR.
        """
        from brad import conflict_context as cc

        issue_key = self._canonical_issue_key(branch_name)

        if self._stop_requested(issue_key):
            self.logger.info(f"PR #{pr_number}: stop requested before rebase conflict resolution")
            self.repo.abort_rebase()
            return

        if len(conflicted_files) > self.MAX_CONFLICT_FILES:
            self.logger.warning(
                f"PR #{pr_number}: {len(conflicted_files)} conflicted files exceeds "
                f"limit of {self.MAX_CONFLICT_FILES}; aborting rebase"
            )
            self.repo.abort_rebase()
            self._comment_on_pr(
                pr_number,
                f"Brad attempted to rebase this PR but found {len(conflicted_files)} "
                f"conflicted files (limit: {self.MAX_CONFLICT_FILES}). Skipping "
                f"automatic resolution — please rebase manually.",
            )
            return

        try:
            issue_title = self._fetch_issue_title(issue_key)
            execution_id = db.create_execution(
                issue_key,
                f"Rebase conflict resolution for PR #{pr_number}",
                cost_budget=float(self.cfg.__dict__.get("cost_budget") or 150.0),
                issue_title=issue_title,
                action="Rebase",
                model_name=self._execution_model_name(),
            )
        except Exception as e:
            self.logger.warning(f"Could not create execution for conflict resolution: {e}")
            execution_id = 0

        current_files = list(conflicted_files)
        continuation_summary = self._build_issue_continuation_summary(issue_key, execution_id=execution_id)

        for iteration in range(1, self.MAX_CONFLICT_ITERATIONS + 1):
            if self._stop_requested(issue_key):
                self.logger.info(f"PR #{pr_number}: stop requested during rebase conflict resolution")
                self.repo.abort_rebase()
                return
            ctx = cc.gather_context(
                repo=self.repo,
                branch_name=branch_name,
                base_branch=base_branch,
                conflicted_files=current_files,
                code_repo=self.code_repo,
                ticketing=self.ticketing,
                logger=self.logger,
            )
            context_section = ctx.to_prompt_section()

            step_id = 0
            if execution_id:
                try:
                    step_id = db.create_step(
                        execution_id, "conflict_resolution",
                        f"AI conflict resolution iteration {iteration}",
                        iteration=iteration,
                    )
                except Exception:
                    pass

            response = self.agent.invoke_conflict_resolution(
                branch_name=branch_name,
                base_branch=base_branch,
                context_section=context_section,
                repo_path=str(self.repo.repo_path),
                iteration=iteration,
                dev_instructions=self._repo_dev_instructions,
                continuation_context=continuation_summary,
                model=self._execution_model_name(),
            )

            usage = response.get("_usage")
            cost = self._calculate_cost(usage)
            pt, ct, cached_pt = self._usage_totals(usage)
            if step_id:
                try:
                    db.finish_step(
                        step_id,
                        status=response.get("action", "unknown"),
                        prompt_tokens=pt,
                        cached_prompt_tokens=cached_pt,
                        completion_tokens=ct,
                        cost=cost,
                        result_summary=_clip_summary(response.get("summary", "")),
                    )
                    db.update_execution_costs(execution_id, pt, ct, cost, cached_prompt_tokens=cached_pt)
                except Exception:
                    pass

            summary_source = f"rebase_conflict_resolution iteration {iteration}"
            state_summary = self._refresh_issue_continuation_summary(
                issue_key,
                response,
                summary_source,
                execution_id=execution_id,
            )
            if state_summary:
                continuation_summary = state_summary
                self.logger.debug(f"Updated continuation summary for {issue_key} after conflict iteration {iteration}")

            action = response.get("action")
            if action == "stuck" or action == "error":
                self.logger.warning(
                    f"PR #{pr_number}: agent returned {action} on conflict "
                    f"resolution iteration {iteration}: {response.get('summary','')}"
                )
                self.repo.abort_rebase()
                self._comment_on_pr(
                    pr_number,
                    "Brad attempted to auto-resolve rebase conflicts but is stuck:\n\n"
                    f"```\n{(response.get('summary') or response.get('message',''))[:1500]}\n```\n\n"
                    "Please rebase manually.",
                )
                if execution_id:
                    try:
                        db.finish_execution(execution_id, status="error",
                                            error_message=_clip_summary(response.get("summary","")))
                    except Exception:
                        pass
                return

            # Verify the agent did its job before continuing the rebase.
            if self._stop_requested(issue_key):
                self.logger.info(f"PR #{pr_number}: stop requested before continuing rebase")
                self.repo.abort_rebase()
                return
            still_marked = self._files_still_have_conflict_markers(current_files)
            if still_marked:
                self.logger.warning(
                    f"PR #{pr_number}: agent claimed RESOLVED but conflict markers "
                    f"remain in: {still_marked}"
                )
                self.repo.abort_rebase()
                self._comment_on_pr(
                    pr_number,
                    "Brad attempted to auto-resolve rebase conflicts but the agent "
                    "left conflict markers in: `"
                    + ", ".join(still_marked)
                    + "`. Aborting rebase. Please rebase manually.",
                )
                if execution_id:
                    try:
                        db.finish_execution(execution_id, status="error",
                                            error_message="agent left conflict markers")
                    except Exception:
                        pass
                return

            cont = self.repo.continue_rebase()
            if cont.get("rebased"):
                self.logger.info(f"PR #{pr_number}: rebase completed after AI resolution")
                push = self.repo.force_push_with_lease(branch_name)
                if not push.get("pushed"):
                    self.logger.warning(
                        f"PR #{pr_number}: rebase done but force-push failed: "
                        f"{push.get('error','')}"
                    )
                    self._comment_on_pr(
                        pr_number,
                        "Brad resolved rebase conflicts locally but failed to push: "
                        f"`{push.get('error','')[:500]}`. Please push manually.",
                    )
                    if execution_id:
                        try:
                            db.finish_execution(execution_id, status="error",
                                                error_message="push failed")
                        except Exception:
                            pass
                    return

                self._comment_on_pr(
                    pr_number,
                    "Brad auto-resolved rebase conflicts and force-pushed the rebased "
                    "branch. Watching CI — any failures will be addressed automatically.",
                )
                if execution_id:
                    try:
                        db.finish_execution(execution_id, status="completed", pr_number=pr_number)
                    except Exception:
                        pass
                # Hand off to the existing CI-fix loop. Test failures from the
                # rebase get treated like any other change.
                try:
                    self._watch_ci_after_push(pr_number, branch_name)
                except Exception as e:
                    self.logger.warning(
                        f"PR #{pr_number}: post-rebase CI watch failed: {e}"
                    )
                return

            if cont.get("conflict"):
                # New conflicts on the next picked commit — loop with the new
                # set of files.
                current_files = cont.get("conflicted_files") or []
                if not current_files:
                    self.logger.warning(
                        f"PR #{pr_number}: continue_rebase reported conflict with "
                        "no unmerged files; aborting"
                    )
                    self.repo.abort_rebase()
                    return
                self.logger.info(
                    f"PR #{pr_number}: more conflicts after --continue, "
                    f"iterating ({len(current_files)} files)"
                )
                continue

            # Anything else (error, in-progress with no work) — bail out.
            err = cont.get("error", "unknown error continuing rebase")
            self.logger.warning(f"PR #{pr_number}: continue_rebase failed: {err}")
            self.repo.abort_rebase()
            self._comment_on_pr(
                pr_number,
                f"Brad failed to continue rebase after resolving conflicts: `{err[:500]}`. "
                "Aborted. Please rebase manually.",
            )
            if execution_id:
                try:
                    db.finish_execution(execution_id, status="error", error_message=err[:500])
                except Exception:
                    pass
            return

        # Iteration cap hit
        self.logger.warning(
            f"PR #{pr_number}: hit conflict-resolution iteration cap "
            f"({self.MAX_CONFLICT_ITERATIONS}); aborting rebase"
        )
        self.repo.abort_rebase()
        self._comment_on_pr(
            pr_number,
            "Brad hit the conflict-resolution iteration cap "
            f"({self.MAX_CONFLICT_ITERATIONS}). Aborted rebase. Please rebase manually.",
        )
        if execution_id:
            try:
                db.finish_execution(execution_id, status="error",
                                    error_message="iteration cap exceeded")
            except Exception:
                pass

    def _files_still_have_conflict_markers(self, paths) -> list:
        """Return paths that still contain ``<<<<<<<`` / ``=======`` / ``>>>>>>>``."""
        bad = []
        for p in paths:
            content = self.repo.read_conflicted_file(p)
            if (
                "<<<<<<<" in content
                or "=======" in content
                or ">>>>>>>" in content
            ):
                bad.append(p)
        return bad

    def _comment_on_pr(self, pr_number: int, body: str) -> None:
        """Best-effort PR comment. Logs and continues on failure."""
        try:
            # GitHub adapter exposes reply_to_issue_comment; for fresh comments
            # we fall back to creating an issue comment via a generic helper if
            # available. Most adapters provide an `issues/{n}/comments` route;
            # use it if exposed, else use the existing reply path with id=0
            # (which most adapters reject), in which case we just log.
            adapter = self.code_repo
            if hasattr(adapter, "_request_with_retry") and hasattr(adapter, "base_url"):
                resp = adapter._request_with_retry(
                    "post",
                    f"{adapter.base_url}/issues/{pr_number}/comments",
                    json={"body": body},
                )
                if resp is not None:
                    resp.raise_for_status()
                    return
            # No supported path — log only.
            self.logger.info(f"(no-op) PR #{pr_number} comment: {body[:200]}")
        except Exception as e:
            self.logger.warning(f"Could not post PR #{pr_number} comment: {e}")

    def _scrap_existing_pr(self, issue_key: str, branch_name: str) -> bool:
        """Scrap existing PR by renaming branch and closing it.
        
        Returns True if successful, False otherwise.
        """
        existing_pr = self.code_repo.pr_exists_for_branch(branch_name)
        if not existing_pr:
            self.logger.info(f"{issue_key}: No existing PR to scrap")
            return True

        self.logger.info(f"{issue_key}: Scrapping existing PR #{existing_pr}")

        # Rename the branch
        old_branch_name = branch_name
        new_branch_name = f"brad/old-{int(time.time())}-{branch_name}"

        try:
            # Ensure workspace is clean before git operations
            self.repo.reset_to_clean_state("main")

            # Fetch and checkout the branch
            self.repo._run_git("fetch", "origin", old_branch_name)
            self.repo._run_git("checkout", old_branch_name)

            # Rename locally
            self.repo._run_git("branch", "-m", old_branch_name, new_branch_name)

            # Push renamed branch
            self.repo._run_git("push", "-u", "origin", new_branch_name, "--force")

            self.logger.info(f"{issue_key}: Renamed branch {old_branch_name} to {new_branch_name}")
        except Exception as e:
            self.logger.error(f"{issue_key}: Failed to rename branch: {e}")
            self.ticketing.comment(
                issue_key,
                f"Brad failed to rename the existing branch due to an error: {str(e)}. "
                f"Please manually rename `{old_branch_name}` to `{new_branch_name}` and close PR #{existing_pr}."
            )
            try:
                self.repo.reset_to_clean_state("main")
            except Exception:
                pass
            return False

        # Close the PR with a comment
        try:
            self.code_repo.close_pr(
                existing_pr,
                comment=f"Scrapping this PR per BradScrapExisting label. Branch renamed to `{new_branch_name}` for recovery."
            )
            self.logger.info(f"{issue_key}: Closed PR #{existing_pr}")
        except Exception as e:
            self.logger.error(f"{issue_key}: Failed to close PR: {e}")
            try:
                self.repo.reset_to_clean_state("main")
            except Exception:
                pass
            return False

        try:
            self.repo._run_git("push", "origin", "--delete", old_branch_name)
            self.repo._run_git("branch", "-D", old_branch_name, check=False)
        except Exception as e:
            self.logger.warning(f"{issue_key}: Failed to delete old branch after closing PR: {e}")

        # Comment on Jira
        self.ticketing.comment(
            issue_key,
            f"Brad has scrapped the existing PR #{existing_pr} per the BradScrapExisting label. "
            f"The branch has been renamed to `{new_branch_name}` for recovery. "
            f"Starting fresh implementation."
        )

        # Restore workspace to clean state
        try:
            self.repo.reset_to_clean_state("main")
        except Exception as e:
            self.logger.warning(f"{issue_key}: Failed to reset workspace after scrap: {e}")

        return True

    def _process_review_comments(self):
        """Check all Brad PRs for new review comments (both file-level and PR-level) and process them."""
        brad_prs = self.code_repo.get_brad_prs()
        if not brad_prs:
            return

        for pr in brad_prs:
            pr_number = pr['number']
            branch_name = pr['head']['ref']

            if db.is_brad_stopped(branch_name):
                self.logger.info(f"PR #{pr_number} ({branch_name}) is stopped; skipping comment processing")
                continue

            # Fetch all three types of comments:
            # 1. Line-specific review comments
            # 2. General review-level comments (review body, not tied to lines)
            # 3. Issue comments (PR-level general comments)
            review_comments = self.code_repo.get_review_comments_needing_response(pr_number)
            review_level_comments = self.code_repo.get_review_level_comments_needing_response(pr_number)
            issue_comments = self.code_repo.get_issue_comments_needing_response(pr_number)

            # Persistent dedupe: skip anything Brad has already acted on in a
            # previous cycle. The adapter heuristics above are chat-history
            # based and have proven fragile (prefix-matching on comment
            # bodies); this DB layer is authoritative per (pr, kind, id).
            # Without it, brad re-feeds the same "already addressed" comment
            # set to the review-fix agent every ci_passed cycle and burns
            # attempts 1..N against the iteration cap.
            pre_filter_counts = (
                len(review_comments), len(review_level_comments), len(issue_comments)
            )
            review_comments = db.filter_unprocessed_comments(pr_number, "review", review_comments)
            review_level_comments = db.filter_unprocessed_comments(pr_number, "review_level", review_level_comments)
            issue_comments = db.filter_unprocessed_comments(pr_number, "issue", issue_comments)
            post_filter_counts = (
                len(review_comments), len(review_level_comments), len(issue_comments)
            )
            if pre_filter_counts != post_filter_counts:
                self.logger.info(
                    f"PR #{pr_number}: dedupe filtered "
                    f"review {pre_filter_counts[0]}→{post_filter_counts[0]}, "
                    f"review_level {pre_filter_counts[1]}→{post_filter_counts[1]}, "
                    f"issue {pre_filter_counts[2]}→{post_filter_counts[2]}"
                )

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
                db.mark_comments_processed(pr_number, "review", review_comments)

            # Process review-level comments (general review body, not tied to lines)
            if review_level_comments:
                self._process_issue_comments_batch(pr_number, branch_name, review_level_comments)
                db.mark_comments_processed(pr_number, "review_level", review_level_comments)

            # Process issue comments (PR-level general comments)
            if issue_comments:
                self._process_issue_comments_batch(pr_number, branch_name, issue_comments)
                db.mark_comments_processed(pr_number, "issue", issue_comments)

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
                self.code_repo.is_brad_comment(r)
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
        pushed = False
        if overall_code_changed:
            try:
                self.repo.push(branch_name, force=False)
                pushed = True
            except Exception:
                try:
                    self.repo.push(branch_name, force=True)
                    pushed = True
                except Exception as e:
                    self.logger.error(f"Failed to push changes for PR #{pr_number}: {e}")

        # If we pushed code changes in response to review comments, follow the
        # PR through CI and auto-fix failures — otherwise brad walks away after
        # replying and never reacts to a red pipeline.
        if pushed:
            self._watch_ci_after_push(pr_number, branch_name)

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
            continuation_context=self._build_issue_continuation_summary(branch_name),
        )

        comment_results = result.get('comment_results', [])
        overall_code_changed = any(cr.get('action') == 'code_changed' for cr in comment_results)

        if overall_code_changed:
            comment_ids = [c.get('id') for c in comments if c.get('id')]
            has_actual = self._has_uncommitted_or_new_commits(branch_name, comment_ids)
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
                # Fallback: force all CODE_CHANGED results to 'replied' to prevent misleading responses
                for cr in comment_results:
                    if cr.get('action') == 'code_changed':
                        cr['action'] = 'replied'
                        self.logger.warning(f"Forced comment result to 'replied' due to no actual code changes")
                overall_code_changed = False

        return comment_results, overall_code_changed

    def _has_uncommitted_or_new_commits(self, branch_name: str, comment_ids: List[int] = None) -> bool:
        """Check if there are uncommitted changes or new commits not yet pushed.
        If uncommitted changes exist, auto-commit them (agent may have forgotten)."""
        try:
            # Check for uncommitted changes
            result = self.repo._run_git("status", "--porcelain")
            if result.stdout.strip():
                self.logger.info(f"Found uncommitted changes on {branch_name}, auto-committing...")
                self.repo._run_git("add", "-A")
                # Include comment IDs in commit message for traceability
                if comment_ids:
                    comment_ids_str = ", ".join(f"#{cid}" for cid in comment_ids[:5])  # Limit to first 5
                    if len(comment_ids) > 5:
                        comment_ids_str += f" (+{len(comment_ids) - 5} more)"
                    commit_msg = f"Brad: address review comments {comment_ids_str} on {branch_name}"
                else:
                    commit_msg = f"Brad: address review comments on {branch_name}"
                self.repo._run_git("commit", "-m", commit_msg)
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
        pushed = False
        if overall_code_changed:
            try:
                self.repo.push(branch_name, force=False)
                pushed = True
            except Exception:
                try:
                    self.repo.push(branch_name, force=True)
                    pushed = True
                except Exception as e:
                    self.logger.error(f"Failed to push changes for PR #{pr_number}: {e}")

        if pushed:
            self._watch_ci_after_push(pr_number, branch_name)

    def _fetch_issue_goal(self, issue_key: str) -> str:
        """Best-effort: pull summary + description from Jira as plain text.

        Returns an empty string on any failure — we never want CI follow-up to
        fall over because Jira is flaky or the branch name doesn't map to a
        real issue.
        """
        issue_key = self._canonical_issue_key(issue_key)
        try:
            issue = self.ticketing.fetch_issue(issue_key)
        except Exception as e:
            self.logger.warning(f"fetch_issue({issue_key}) raised: {e}")
            return ""
        if not issue:
            return ""
        fields = issue.get("fields", {}) or {}
        summary = (fields.get("summary") or "").strip()
        raw_desc = fields.get("description")
        try:
            desc_text = adf_to_text(raw_desc) if raw_desc else ""
        except Exception as e:
            self.logger.warning(f"Could not render description for {issue_key}: {e}")
            desc_text = ""
        parts = []
        if summary:
            parts.append(f"## Original goal: {issue_key}\n{summary}")
        if desc_text.strip():
            parts.append(desc_text.strip())
        return "\n\n".join(parts)

    def _fetch_issue_title(self, issue_key: str) -> str:
        """Best-effort: fetch just the Jira issue title for UI traceability."""
        issue_key = self._canonical_issue_key(issue_key)
        try:
            issue = self.ticketing.fetch_issue(issue_key)
        except Exception as e:
            self.logger.warning(f"fetch_issue({issue_key}) raised while fetching title: {e}")
            return ""
        if not issue:
            return ""
        fields = issue.get("fields", {}) or {}
        return (fields.get("summary") or "").strip()

    def _summarize_prior_activity(self, issue_key: str, max_chars: int = 1500) -> str:
        """Build a short bullet-list digest of prior brad executions/steps for this issue."""
        issue_key = self._canonical_issue_key(issue_key)
        try:
            executions = db.get_executions_by_issue(issue_key)
        except Exception as e:
            self.logger.debug(f"Could not fetch executions for {issue_key}: {e}")
            return ""
        if not executions:
            return ""

        lines: List[str] = []
        latest = executions[0]
        issue_title = (latest.get("issue_title") or "").strip()
        issue_summary = (latest.get("summary") or "").strip()
        latest_steps = []
        try:
            latest_steps = db.get_execution_steps(latest.get("id")) if latest.get("id") else []
        except Exception:
            latest_steps = []
        latest_step = latest_steps[-1] if latest_steps else None
        latest_step_summary = (latest_step.get("result_summary") or "").strip().replace("\n", " ") if latest_step else ""
        if len(latest_step_summary) > 220:
            latest_step_summary = latest_step_summary[:220] + "…"

        if issue_title:
            lines.append(f"- Issue title: {issue_title}")
        if issue_title or issue_summary:
            lines.append(f"- Goal: {issue_title or issue_summary}")
        lines.append(
            "- Current state: "
            f"latest execution #{latest.get('id')} status={latest.get('status', '')} "
            f"phase={latest.get('current_phase') or 'unknown'}"
        )
        current_detail = (latest.get("current_phase_detail") or "").strip()
        if current_detail:
            lines.append(f"  - Current detail: {current_detail}")
        error_message = (latest.get("error_message") or "").strip()
        if error_message:
            lines.append(f"  - Last error: {error_message}")
        if latest_step:
            latest_step_phase = (latest_step.get("phase") or "").strip()
            latest_step_status = (latest_step.get("status") or "").strip()
            step_label = latest_step_phase or "unknown"
            if latest_step_status:
                step_label += f" ({latest_step_status})"
            lines.append(f"- Latest step: {step_label}")
            if latest_step_summary:
                lines.append(f"  - Latest step result: {latest_step_summary}")

        # Walk from oldest to newest so the agent reads chronologically.
        for execution in reversed(executions):
            exec_id = execution.get("id")
            started = execution.get("started_at", "")
            status = execution.get("status", "")
            action = (execution.get("action") or "").strip()
            phase = (execution.get("current_phase") or "").strip()
            phase_detail = (execution.get("current_phase_detail") or "").strip()
            lines.append(
                f"- Execution #{exec_id} [{started}] status={status}"
                + (f" action={action}" if action else "")
                + (f" phase={phase}" if phase else "")
            )
            if phase_detail:
                lines.append(f"    • Phase detail: {phase_detail}")
            try:
                steps = db.get_execution_steps(exec_id) if exec_id else []
            except Exception:
                steps = []
            for step in steps:
                phase = step.get("phase", "?")
                step_status = step.get("status", "?")
                summary = (step.get("result_summary") or "").strip().replace("\n", " ")
                if len(summary) > 200:
                    summary = summary[:200] + "…"
                lines.append(f"    • {phase} ({step_status}): {summary}" if summary else f"    • {phase} ({step_status})")

        digest = "\n".join(lines)
        if len(digest) > max_chars:
            digest = digest[: max_chars - 1] + "…"
        return digest

    def _watch_ci_after_push(self, pr_number: int, branch_name: str) -> None:
        """Watch CI on a PR after a review-driven push and auto-fix failures.

        The review-comments path runs without a Jira-driven `IssueState`/execution,
        so we synthesize one here and reuse the existing CI monitoring + fix loop
        (`_handle_ci_monitoring` → `_handle_ci_fix`). Without this, brad replies
        to comments, pushes a fix, and then walks away even if the new commit
        breaks the pipeline.
        """
        issue_key = self._canonical_issue_key(branch_name)
        if self._stop_requested(issue_key):
            self.logger.info(f"PR #{pr_number}: stop requested before CI watch")
            return
        # Use the embedded Jira key when branches are slugged, e.g.
        # DEV-3837-add-bea-repo-agents-and-bootstrap-guidance.

        # Recover the original goal so the CI-fix agent isn't blind to intent.
        goal_text = self._fetch_issue_goal(issue_key)

        # Surface what brad has already tried on this issue, so retries don't
        # blindly redo earlier failed strategies.
        history_text = self._summarize_prior_activity(issue_key)

        description_parts = []
        if goal_text:
            description_parts.append(goal_text)
        if history_text:
            description_parts.append("## Prior brad activity on this issue\n" + history_text)
        synthesized_description = "\n\n".join(description_parts)

        try:
            issue_title = self._fetch_issue_title(issue_key)
            execution_id = db.create_execution(
                issue_key,
                f"Review-driven CI watch on PR #{pr_number}",
                cost_budget=float(self.cfg.__dict__.get("cost_budget") or 150.0),
                issue_title=issue_title,
                action="CI Fix",
                model_name=self._execution_model_name(),
            )
        except Exception as e:
            self.logger.warning(f"Could not create execution for CI watch on PR #{pr_number}: {e}")
            execution_id = 0

        state = IssueState(
            issue_key=issue_key,
            description=synthesized_description,
            attachments=[],
            attachment_paths=[],
            branch_name=branch_name,
            execution_id=execution_id,
            pr_number=pr_number,
            cost_budget=float(self.cfg.__dict__.get("cost_budget") or 150.0),
        )

        try:
            self._handle_ci_monitoring(state)
            if self._stop_requested(state.issue_key):
                db.finish_execution(execution_id, status="stopped", error_message="Brad stopped this ticket")
                return
            if execution_id:
                db.finish_execution(execution_id, status="completed", pr_number=pr_number)
        except Exception as e:
            self.logger.error(f"CI watch failed for PR #{pr_number}: {e}", exc_info=True)
            if execution_id:
                try:
                    db.finish_execution(execution_id, status="error", error_message=str(e)[:500])
                except Exception:
                    pass

    def _process_issue(self, issue: Dict):
        """Process a single issue through the Brad workflow."""
        issue_key = issue["key"]
        if self._stop_requested(issue_key):
            self.logger.info(f"{issue_key}: issue is stopped; skipping issue processing")
            return
        fields = issue.get("fields", {})
        summary = fields.get("summary", "N/A")

        self.logger.info(f"Processing issue: {issue_key}")
        self.logger.info(f"Summary: {summary}")

        # Resolve model from BradLight / BradHeavy label before creating the execution record.
        raw_labels = [lbl.get("name", lbl) if isinstance(lbl, dict) else lbl
                      for lbl in fields.get("labels", [])]
        if "BradLight" in raw_labels:
            resolved_model = self.cfg.codex_model_light or self._execution_model_name()
            trigger_label = "BradLight"
        elif "BradHeavy" in raw_labels:
            resolved_model = self.cfg.codex_model_heavy or self._execution_model_name()
            trigger_label = "BradHeavy"
        else:
            resolved_model = self._execution_model_name()
            trigger_label = None
        self.logger.info(f"{issue_key}: using model={resolved_model} (trigger={trigger_label or 'default'})")

        # Create execution record
        issue_title = summary
        execution_id = db.create_execution(
            issue_key,
            summary,
            cost_budget=float(self.cfg.__dict__.get("cost_budget") or 150.0),
            issue_title=issue_title,
            action="Implement",
            model_name=resolved_model,
        )

        try:
            db.update_execution_phase(execution_id, "initializing", "Preparing to process issue")

            # Step 1: Remove trigger label immediately
            if trigger_label:
                self.ticketing.remove_label(issue_key, trigger_label)

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

            prior_grooming_cycles = sum(
                1 for ex in db.get_executions_by_issue(issue_key)
                if ex.get("status") == "grooming" and ex.get("id") != execution_id
            )
            state = IssueState(
                issue_key=issue_key,
                description=description,
                attachments=attachments,
                attachment_paths=attachment_paths,
                branch_name=branch_name,
                execution_id=execution_id,
                jira_updated=fields.get("updated", ""),
                cost_budget=float(self.cfg.__dict__.get("cost_budget")),
                model=resolved_model,
                clarification_count=prior_grooming_cycles,
            )
            state.continuation_summary = self._build_issue_continuation_summary(issue_key, execution_id=execution_id)

            # Step 4: Check for BradScrapExisting label — scrap existing work if present
            labels = raw_labels
            has_scrap_label = "BradScrapExisting" in labels
            if has_scrap_label:
                self._set_phase(state, "scrapping_existing", "Scrapping existing PR and branch")
                scrap_success = self._scrap_existing_pr(issue_key, branch_name)
                if scrap_success:
                    try:
                        self.ticketing.remove_label(issue_key, "BradScrapExisting")
                    except Exception as e:
                        self.logger.warning(f"{issue_key}: Failed to remove BradScrapExisting label: {e}")
                else:
                    self._set_phase(state, "stuck", "Could not scrap existing PR")
                    db.finish_execution(
                        execution_id,
                        status="error",
                        error_message="Brad could not safely scrap the existing PR",
                    )
                    return

            # Step 5: Detect existing PR — continue on it instead of restarting.
            # To force a fresh start, use BradScrapExisting label instead of manual intervention.
            self._set_phase(state, "checking_existing_pr", "Checking for existing PR to continue")
            if self._stop_requested(state.issue_key):
                self.logger.info(f"{issue_key}: stop requested before PR lookup")
                return
            existing_pr = self.code_repo.pr_exists_for_branch(branch_name)
            if existing_pr:
                self.logger.info(f"{issue_key}: Continuing work on existing PR #{existing_pr}")
                self.ticketing.comment(
                    issue_key,
                    f"Brad is continuing work on existing PR #{existing_pr}. "
                    f"To restart from scratch, add the BradScrapExisting label before BradLight/BradHeavy."
                )

            # Step 6: Requirements grooming — only for fresh tickets (no existing PR).
            # Skip if: an existing PR was found (already past grooming), or BradSkipGrooming label present.
            skip_grooming = existing_pr or "BradSkipGrooming" in labels
            if "BradSkipGrooming" in labels:
                self.logger.info(f"{issue_key}: BradSkipGrooming label present — skipping requirements phase")
                try:
                    self.ticketing.remove_label(issue_key, "BradSkipGrooming")
                except Exception as e:
                    self.logger.warning(f"{issue_key}: Failed to remove BradSkipGrooming label: {e}")

            if not skip_grooming:
                grooming_done = self._handle_requirements_phase(state)
                if not grooming_done:
                    # Grooming posted questions to Jira and stopped — execution finished inside the method.
                    return

            self._handle_implementation_phase(state, existing_pr)
            if self._stop_requested(issue_key):
                db.finish_execution(
                    execution_id,
                    status="stopped",
                    error_message="Brad stopped this ticket",
                    pr_number=state.pr_number,
                    pr_url=f"https://github.com/{self.cfg.github_repo}/pull/{state.pr_number}" if state.pr_number else None,
                )
                return

            # Finish execution with status reflecting actual outcome
            final_status = "completed" if state.pr_number else "stuck"
            db.update_execution_phase(execution_id, "done" if final_status == "completed" else "stuck")
            db.finish_execution(
                execution_id,
                status=final_status,
                pr_number=state.pr_number,
                pr_url=f"https://github.com/{self.cfg.github_repo}/pull/{state.pr_number}" if state.pr_number else None,
                error_message=(state.last_failure_detail or "Brad could not create a PR") if not state.pr_number else None,
            )

        except Exception as e:
            db.update_execution_phase(execution_id, "error", str(e)[:200])
            db.finish_execution(execution_id, status="error", error_message=_clip_summary(e))
            raise

    def _handle_requirements_phase(self, state: IssueState) -> bool:
        """Handle requirements grooming phase.

        Returns True if requirements are satisfied and implementation should proceed.
        Returns False if Brad posted questions to Jira and the execution is now parked
        (caller should return immediately; this method has already finished the execution record).
        """
        self._set_phase(state, "reading_requirements", "Analyzing issue requirements")
        if self._stop_requested(state.issue_key):
            db.finish_execution(state.execution_id, status="stopped", error_message="Brad stopped this ticket")
            return False
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
                continuation_context=state.continuation_summary,
                model=state.model,
            )
            self._record_step(state.execution_id, "requirements", response)
            state.continuation_summary = self._refresh_issue_continuation_summary(
                state.issue_key,
                response,
                "requirements",
                execution_id=state.execution_id,
            )
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
                self.ticketing.comment(
                    state.issue_key,
                    "Brad has asked too many clarification rounds without a resolution. "
                    "Please rewrite the ticket description with complete requirements and re-add BradLight or BradHeavy. "
                    "Add BradSkipGrooming alongside BradLight/BradHeavy to bypass the grooming step entirely."
                )
                db.update_execution_phase(state.execution_id, "stuck", "Too many clarification cycles")
                db.finish_execution(state.execution_id, status="stuck",
                                    error_message="Too many clarification cycles")
            else:
                db.update_execution_phase(
                    state.execution_id, "awaiting_clarification",
                    f"Waiting for PM to answer clarification questions (round {state.clarification_count})"
                )
                db.finish_execution(state.execution_id, status="grooming")
            return False

        elif action == "propose_scenarios":
            # Scenarios posted for PM review — park until re-triggered
            self.ticketing.comment(state.issue_key, message)
            db.update_execution_phase(
                state.execution_id, "awaiting_clarification",
                "Waiting for PM to review proposed acceptance criteria"
            )
            db.finish_execution(state.execution_id, status="grooming")
            return False

        elif action == "ready":
            self.logger.info(f"{state.issue_key}: Requirements grooming complete — proceeding to implementation")
            return True

        else:
            self.ticketing.comment(
                state.issue_key,
                f"Brad encountered an error during requirements analysis:\n\n{message}\n\nBrad is stuck."
            )
            db.update_execution_phase(state.execution_id, "stuck", "Error in requirements analysis")
            db.finish_execution(state.execution_id, status="error",
                                error_message=f"Requirements analysis error: {message[:200]}")
            return False

    def _handle_implementation_phase(self, state: IssueState, existing_pr: Optional[int] = None):
        """Handle implementation phase — impl → local review loop → then create PR → CI."""
        if self._stop_requested(state.issue_key):
            self.logger.info(f"{state.issue_key}: stop requested before implementation")
            return
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
            dev_instructions=self._repo_dev_instructions,
            existing_pr=existing_pr,
            continuation_context=state.continuation_summary,
            model=state.model,
        )
        state.continuation_summary = self._refresh_issue_continuation_summary(
            state.issue_key,
            response,
            "implementation",
            execution_id=state.execution_id,
        )

        # Record step costs
        usage = response.get("_usage")
        cost = self._calculate_cost(usage)
        pt, ct, cached_pt = self._usage_totals(usage)
        db.finish_step(step_id, status=response.get("action", "unknown"), prompt_tokens=pt, cached_prompt_tokens=cached_pt, completion_tokens=ct, cost=cost, result_summary=response.get("message", "")[:500])
        db.update_execution_costs(state.execution_id, pt, ct, cost, cached_prompt_tokens=cached_pt)
        if self._stop_requested(state.issue_key):
            return

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
        if self._stop_requested(state.issue_key):
            self.logger.info(f"{state.issue_key}: stop requested before local review")
            return
        self.ticketing.comment(state.issue_key, "Implementation complete. Running local code review before creating PR...")
        local_review_passed = self._run_local_review_loop(state)
        if self._stop_requested(state.issue_key):
            return

        # Always verify/create PR and persist to DB (even if budget is tight)
        if self._stop_requested(state.issue_key):
            self.logger.info(f"{state.issue_key}: stop requested before PR creation")
            return
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
            if self._stop_requested(state.issue_key):
                return
        else:
            self._set_phase(state, "stuck", "Could not create PR")
            detail = state.last_failure_detail or "Brad could not create a PR."
            self.ticketing.comment(
                state.issue_key,
                "Brad completed implementation but failed to create PR.\n\n"
                f"Reason:\n{detail}\n\nManual intervention needed."
            )

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
        if self._stop_requested(state.issue_key):
            return False
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
        state.last_failure_detail = ""

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
                state.last_failure_detail = f"Failed to push branch before PR creation: {_clip_summary(e, 1500)}"
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
            detail = (result.stderr or result.stdout or "gh pr create returned non-zero").strip()
            self.logger.error(f"{issue_key}: Failed to create PR: {detail}")
            state.last_failure_detail = f"Failed to create PR via gh: {_clip_summary(detail, 1500)}"
            return None
        except Exception as e:
            self.logger.error(f"{issue_key}: Exception creating PR: {e}")
            state.last_failure_detail = f"Exception while creating PR: {_clip_summary(e, 1500)}"
            return None

    def _handle_local_review(self, state: IssueState) -> Dict:
        """Run a fresh-context local review and return its structured outcome."""
        if self._stop_requested(state.issue_key):
            return {"action": "stuck", "message": "Brad stopped by operator"}
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
                continuation_context=state.continuation_summary,
                model=state.model,
            )

            action = response.get("action")
            message = response.get("message", "")
            usage = response.get("_usage")
            cost = self._calculate_cost(usage)
            pt, ct, cached_pt = self._usage_totals(usage)
            db.finish_step(step_id, status=action or "completed", prompt_tokens=pt, cached_prompt_tokens=cached_pt, completion_tokens=ct, cost=cost, result_summary=_clip_summary(message))
            db.update_execution_costs(state.execution_id, pt, ct, cost, cached_prompt_tokens=cached_pt)
            state.continuation_summary = self._refresh_issue_continuation_summary(
                state.issue_key,
                response,
                "local_review",
                execution_id=state.execution_id,
            )

            if action == "approved":
                self.ticketing.comment(state.issue_key, f"Local code review PASSED:\n\n{message}")
                return {"action": "approved", "message": message}

            self.ticketing.comment(state.issue_key, f"Local code review found issues:\n\n{message}\n\nBrad will address these.")
            return {"action": "changes_requested", "message": message}
        except Exception as e:
            self.logger.error(f"{state.issue_key}: Local review failed: {e}", exc_info=True)
            db.finish_step(step_id, status="error", result_summary=_clip_summary(e))
            return {"action": "error", "message": str(e)}

    def _handle_local_review_fix(self, state: IssueState, review_feedback: str) -> bool:
        """Address local review feedback and rerun local review before CI."""
        if self._stop_requested(state.issue_key):
            return False
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
            dev_instructions=self._repo_dev_instructions,
            continuation_context=state.continuation_summary,
            model=state.model,
        )
        state.continuation_summary = self._refresh_issue_continuation_summary(
            state.issue_key,
            response,
            "local_review_fix",
            execution_id=state.execution_id,
        )

        # Record step costs
        usage = response.get("_usage")
        cost = self._calculate_cost(usage)
        pt, ct, cached_pt = self._usage_totals(usage)
        db.finish_step(step_id, status=response.get("action", "unknown"), prompt_tokens=pt, cached_prompt_tokens=cached_pt, completion_tokens=ct, cost=cost, result_summary=_clip_summary(response.get("message", "")))
        db.update_execution_costs(state.execution_id, pt, ct, cost, cached_prompt_tokens=cached_pt)

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
        if self._stop_requested(state.issue_key):
            self.logger.info(f"{state.issue_key}: stop requested before CI monitoring")
            return
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
            timeout=3600,
            issue_key=state.issue_key,
        )

        if getattr(ci_result, "stopped", False):
            self.logger.info(f"{state.issue_key}: CI monitoring stopped by operator")
            db.finish_step(step_id, status="stopped", result_summary="Brad stopped this ticket")
            return

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

            if self._stop_requested(state.issue_key):
                self.logger.info(f"{state.issue_key}: stop requested after CI passed")
                return

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

            # Persistent dedupe: drop anything Brad has already addressed in a
            # previous cycle. Without this the agent runs against the same
            # "already addressed" set on every ci_passed, incrementing
            # state.review_fix_count up to max_review_fix_iterations on a
            # no-op (observed today: attempts 1→4 on PR #2363 in 4 minutes).
            pre = len(review_comments)
            review_comments = db.filter_unprocessed_comments(pr_number, "review", review_comments)
            if pre != len(review_comments):
                self.logger.info(
                    f"PR #{pr_number}: dedupe filtered ci-passed review comments "
                    f"{pre}→{len(review_comments)}"
                )

            if review_comments:
                self._set_phase(state, "addressing_review_comments", f"{len(review_comments)} review comments to address")
                self.ticketing.comment(
                    state.issue_key,
                    f"CI passed, but there are {len(review_comments)} review comments to address. Brad is working on them..."
                )
                self._handle_review_fix(state, review_comments)
                if self._stop_requested(state.issue_key):
                    return
                # Mark processed even if the agent said "already done" — that
                # is precisely the case we must not re-enter. New review
                # comments posted after this point will have fresh IDs.
                db.mark_comments_processed(pr_number, "review", review_comments)
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
        health = self.ci.check_deployment_health(pr_number=state.pr_number, issue_key=state.issue_key)
        if health.get("stopped"):
            return ""

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
        if self._stop_requested(state.issue_key):
            return
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
            dev_instructions=self._repo_dev_instructions,
            continuation_context=state.continuation_summary,
            model=state.model,
        )
        state.continuation_summary = self._refresh_issue_continuation_summary(
            state.issue_key,
            response,
            "ci_fix",
            execution_id=state.execution_id,
        )

        # Record step costs
        usage = response.get("_usage")
        cost_val = self._calculate_cost(usage)
        pt, ct, cached_pt = self._usage_totals(usage)
        db.finish_step(step_id, status=response.get("action", "unknown"), prompt_tokens=pt, cached_prompt_tokens=cached_pt, completion_tokens=ct, cost=cost_val, result_summary=_clip_summary(response.get("message", "")))
        db.update_execution_costs(state.execution_id, pt, ct, cost_val, cached_prompt_tokens=cached_pt)

        action = response.get("action")
        message = response.get("message", "")

        if action == "fixed":
            state.ci_fix_count += 1
            self.ticketing.comment(
                state.issue_key,
                f"Brad attempted to fix CI failures (attempt {state.ci_fix_count}):\n\n{message}\n\nWaiting for CI to re-run..."
            )
            if self._stop_requested(state.issue_key):
                return
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
        if self._stop_requested(state.issue_key):
            return
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
            dev_instructions=self._repo_dev_instructions,
            continuation_context=state.continuation_summary,
            model=state.model,
        )
        state.continuation_summary = self._refresh_issue_continuation_summary(
            state.issue_key,
            response,
            "review_fix",
            execution_id=state.execution_id,
        )

        # Record step costs
        usage = response.get("_usage")
        cost_val = self._calculate_cost(usage)
        pt, ct, cached_pt = self._usage_totals(usage)
        db.finish_step(step_id, status=response.get("action", "unknown"), prompt_tokens=pt, cached_prompt_tokens=cached_pt, completion_tokens=ct, cost=cost_val, result_summary=_clip_summary(response.get("message", "")))
        db.update_execution_costs(state.execution_id, pt, ct, cost_val, cached_prompt_tokens=cached_pt)

        action = response.get("action")
        message = response.get("message", "")

        if action == "fixed":
            state.review_fix_count += 1
            self.ticketing.comment(
                state.issue_key,
                f"Brad addressed review comments (attempt {state.review_fix_count}):\n\n{message}\n\nWaiting for CI to re-run..."
            )
            if self._stop_requested(state.issue_key):
                return
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
