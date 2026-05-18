"""Brad Web GUI — read-only Flask dashboard for execution history and costs."""
import os
import re
from typing import Callable, Optional

from flask import Flask, render_template, jsonify
from brad import db
from brad.github_auth import build_github_token_provider_from_env
from brad.logging_config import get_logger

logger = get_logger(__name__)


def _looks_like_action_summary(summary: str) -> bool:
    summary_l = (summary or "").strip().lower()
    if not summary_l:
        return False
    return any(
        summary_l.startswith(prefix)
        for prefix in (
            "review-driven ci watch",
            "rebase conflict resolution",
            "fixing ci failures",
            "implementation",
            "local review",
            "local review fix",
            "review fix",
            "addressing review comments",
        )
    )


def format_execution_status_label(status: Optional[str]) -> str:
    status = (status or "").strip()
    if not status:
        return ""
    if status.lower() == "completed":
        return "DONE"
    return status.upper()


def format_execution_action(execution) -> str:
    """Return a short action label for list views."""
    execution = execution or {}
    explicit = (execution.get("action") or "").strip()
    if explicit:
        return explicit

    summary = (execution.get("summary") or "").strip()
    phase = (execution.get("current_phase") or "").strip().lower()
    summary_l = summary.lower()

    if phase in {"conflict_resolution"} or "rebase conflict resolution" in summary_l or "rebase" in summary_l:
        return "Rebase"
    if phase in {"ci_fix"} or "fixing ci failures" in summary_l or ("ci" in summary_l and "fix" in summary_l):
        return "CI Fix"
    if phase in {"ci_monitoring"} or "review-driven ci watch" in summary_l:
        return "CI Watch"
    if phase in {"local_review_fix", "review_fix", "addressing_review_comments"}:
        return "Review Fix"
    if phase in {"local_review"} or "local review" in summary_l:
        return "Review"
    if phase in {"implementation", "implementing"}:
        return "Implement"
    if _looks_like_action_summary(summary):
        return "Work"
    return "Implement"


def _make_issue_title_fetcher() -> Optional[Callable[[str], str]]:
    jira_url = os.environ.get("JIRA_URL")
    jira_user = os.environ.get("JIRA_USER")
    jira_token = os.environ.get("JIRA_TOKEN")
    if not jira_url or not jira_user or not jira_token:
        return None

    from pathlib import Path
    from brad.adapters.ticketing.jira_adapter import JiraAdapter

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.jira_url = jira_url
    cfg.jira_user = jira_user
    cfg.jira_token = jira_token
    cfg.attachments_dir = str(Path("/tmp"))
    adapter = JiraAdapter(cfg)
    cache = {}

    def fetch(issue_key: str) -> str:
        issue_key = (issue_key or "").strip()
        if not issue_key:
            return ""
        if issue_key in cache:
            return cache[issue_key]
        issue = adapter.fetch_issue(issue_key)
        title = ""
        if issue:
            fields = issue.get("fields", {}) or {}
            title = (fields.get("summary") or "").strip()
        cache[issue_key] = title
        if title:
            try:
                db.update_issue_title_for_issue(issue_key, title)
            except Exception as exc:
                logger.debug("Could not backfill issue title for %s: %s", issue_key, exc)
        return title

    return fetch


def decorate_execution(execution, issue_title_fetcher: Optional[Callable[[str], str]] = None):
    """Add display-only fields used by the list views."""
    ex = dict(execution or {})
    issue_title = (ex.get("issue_title") or "").strip()
    if not issue_title:
        if issue_title_fetcher:
            issue_title = issue_title_fetcher(ex.get("issue_key") or "")
        if not issue_title:
            summary = (ex.get("summary") or "").strip()
            issue_title = summary if summary and not _looks_like_action_summary(summary) else (ex.get("issue_key") or "")
    ex["issue_title"] = issue_title
    ex["action"] = format_execution_action(ex)
    ex["status_label"] = format_execution_status_label(ex.get("status"))
    return ex


def build_execution_cost_breakdown(execution, steps):
    """Aggregate per-step token/cost data into phase-level breakdown rows."""
    total_cost = float((execution or {}).get("total_cost") or 0.0)
    ordered = []
    by_phase = {}

    for step in steps or []:
        phase = step.get("phase") or "unknown"
        row = by_phase.get(phase)
        if row is None:
            row = {
                "phase": phase,
                "steps": 0,
                "non_cached_prompt_tokens": 0,
                "cached_prompt_tokens": 0,
                "completion_tokens": 0,
                "total_input_tokens": 0,
                "total_tokens": 0,
                "cost": 0.0,
            }
            by_phase[phase] = row
            ordered.append(row)

        prompt_tokens = int(step.get("prompt_tokens") or 0)
        cached_prompt_tokens = int(step.get("cached_prompt_tokens") or 0)
        completion_tokens = int(step.get("completion_tokens") or 0)
        non_cached_prompt_tokens = max(0, prompt_tokens - cached_prompt_tokens)

        row["steps"] += 1
        row["non_cached_prompt_tokens"] += non_cached_prompt_tokens
        row["cached_prompt_tokens"] += cached_prompt_tokens
        row["completion_tokens"] += completion_tokens
        row["total_input_tokens"] += prompt_tokens
        row["total_tokens"] += prompt_tokens + completion_tokens
        row["cost"] += float(step.get("cost") or 0.0)

    for row in ordered:
        row["cost_pct"] = (row["cost"] / total_cost * 100.0) if total_cost > 0 else 0.0

    return ordered


def build_ticket_cost_breakdown(issue_key: str):
    """Aggregate step cost data across all executions for a Jira issue."""
    rows = db.get_ticket_cost_breakdown(issue_key)
    total_cost = float(sum(float(row.get("cost") or 0.0) for row in rows))
    for row in rows:
        row["cost_pct"] = (float(row.get("cost") or 0.0) / total_cost * 100.0) if total_cost > 0 else 0.0
        row.pop("first_started_at", None)
    return rows


def build_execution_failure_context(execution, steps):
    """Surface the most actionable failure context for a stuck/failed execution."""
    execution = execution or {}
    status = execution.get("status") or ""
    if status not in {"stuck", "failed", "error"} and not execution.get("error_message"):
        return None

    last_step = steps[-1] if steps else None
    error_message = (execution.get("error_message") or "").strip()
    current_phase = execution.get("current_phase") or ""
    current_phase_detail = execution.get("current_phase_detail") or ""
    inferred_reason = ""
    related_pr_number = None
    related_pr_url = None

    summary = (last_step or {}).get("result_summary") or ""
    pr_match = re.search(r"https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)", summary)
    if pr_match:
        related_pr_number = int(pr_match.group(1))
        related_pr_url = pr_match.group(0)

    generic_pr_failure = error_message == "Brad could not create a PR"
    if generic_pr_failure and last_step and last_step.get("phase") == "implementation":
        if "Created PR #" in summary:
            inferred_reason = (
                "The implementation step reported that it created a PR, but Brad did not "
                "persist or verify that PR afterward. This usually means the post-run PR "
                "verification path disagreed with the agent output."
            )
        elif summary:
            inferred_reason = (
                "The execution-level error is generic, but the implementation step summary "
                "below may contain the more specific failure detail."
            )

    return {
        "primary_message": error_message or current_phase_detail or status,
        "current_phase": current_phase,
        "current_phase_detail": current_phase_detail,
        "last_step_phase": (last_step or {}).get("phase"),
        "last_step_status": (last_step or {}).get("status"),
        "last_step_detail": (last_step or {}).get("detail"),
        "last_step_summary": summary,
        "inferred_reason": inferred_reason,
        "related_pr_number": related_pr_number,
        "related_pr_url": related_pr_url,
    }


def create_app(db_path: str = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )

    if db_path is None:
        db_path = os.environ.get("BRAD_DB_PATH", "brad_data.db")

    db.init_db(db_path)
    issue_title_fetcher = _make_issue_title_fetcher()

    jira_url = os.environ.get("JIRA_URL", "https://jira.example.com")
    github_repo = os.environ.get("GITHUB_REPO", "")

    @app.context_processor
    def inject_globals():
        return {"jira_url": jira_url, "github_repo": github_repo}

    @app.route("/")
    def dashboard():
        running = db.get_running_execution()
        running = decorate_execution(running, issue_title_fetcher) if running else None
        recent = [decorate_execution(ex, issue_title_fetcher) for ex in db.get_all_executions(limit=10)]
        totals = db.get_total_costs()
        return render_template(
            "dashboard.html",
            running=running,
            recent=recent,
            totals=totals,
        )

    @app.route("/history")
    def history():
        executions = [decorate_execution(ex, issue_title_fetcher) for ex in db.get_all_executions(limit=200)]
        totals = db.get_total_costs()
        return render_template("history.html", executions=executions, totals=totals)

    @app.route("/execution/<int:execution_id>")
    def execution_detail(execution_id):
        execution = decorate_execution(db.get_execution(execution_id), issue_title_fetcher)
        if not execution:
            return "Execution not found", 404
        steps = db.get_execution_steps(execution_id)
        ci_runs = db.get_execution_ci_runs(execution_id)
        cost_breakdown = build_execution_cost_breakdown(execution, steps)
        ticket_cost_breakdown = build_ticket_cost_breakdown(execution.get("issue_key"))
        failure_context = build_execution_failure_context(execution, steps)
        return render_template(
            "ticket_detail.html",
            execution=execution,
            steps=steps,
            ci_runs=ci_runs,
            cost_breakdown=cost_breakdown,
            ticket_cost_breakdown=ticket_cost_breakdown,
            failure_context=failure_context,
        )

    @app.route("/api/status")
    def api_status():
        running = db.get_running_execution()
        running = decorate_execution(running, issue_title_fetcher) if running else None
        totals = db.get_total_costs()
        return jsonify({
            "backend_running": running is not None,
            "current_issue": running["issue_key"] if running else None,
            "totals": totals,
        })

    @app.route("/api/dashboard")
    def api_dashboard():
        running = db.get_running_execution()
        running = decorate_execution(running, issue_title_fetcher) if running else None
        recent = [decorate_execution(ex, issue_title_fetcher) for ex in db.get_all_executions(limit=10)]
        totals = db.get_total_costs()
        return jsonify({
            "running": running,
            "recent": recent,
            "totals": totals,
        })

    @app.route("/api/executions")
    def api_executions():
        executions = [decorate_execution(ex, issue_title_fetcher) for ex in db.get_all_executions(limit=200)]
        totals = db.get_total_costs()
        return jsonify({"executions": executions, "totals": totals})

    @app.route("/api/execution/<int:execution_id>")
    def api_execution_detail(execution_id):
        execution = decorate_execution(db.get_execution(execution_id), issue_title_fetcher)
        if not execution:
            return jsonify({"error": "not found"}), 404
        steps = db.get_execution_steps(execution_id)
        ci_runs = db.get_execution_ci_runs(execution_id)
        cost_breakdown = build_execution_cost_breakdown(execution, steps)
        ticket_cost_breakdown = build_ticket_cost_breakdown(execution.get("issue_key"))
        failure_context = build_execution_failure_context(execution, steps)
        return jsonify({
            "execution": execution,
            "steps": steps,
            "ci_runs": ci_runs,
            "cost_breakdown": cost_breakdown,
            "ticket_cost_breakdown": ticket_cost_breakdown,
            "failure_context": failure_context,
        })

    @app.route("/api/model-costs")
    def api_model_costs():
        costs = db.get_all_model_costs()
        return jsonify({"costs": costs})

    @app.route("/api/pr/<int:pr_number>")
    def api_pr_details(pr_number):
        """Fetch live PR details from GitHub (reviews, checks, status)."""
        if not github_repo:
            return jsonify({"error": "GITHUB_REPO not configured"}), 500
        try:
            adapter = _get_github_adapter()
            details = adapter.get_pr_details(pr_number)
            return jsonify(details)
        except Exception as e:
            logger.error(f"Failed to fetch PR #{pr_number} details: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/pr/<int:pr_number>/comments")
    def api_pr_review_comments(pr_number):
        """Fetch review comments with their reply threads to show response status."""
        if not github_repo:
            return jsonify({"error": "GITHUB_REPO not configured"}), 500
        try:
            adapter = _get_github_adapter()
            comments = adapter.fetch_review_comments(pr_number)
            enriched = []
            for c in comments:
                replies = adapter.get_comment_replies(pr_number, c["id"])
                brad_replied = any(adapter.is_brad_comment(r) for r in replies)
                enriched.append({
                    "id": c["id"],
                    "user": c.get("user", {}).get("login", "unknown"),
                    "body": (c.get("body") or "")[:300],
                    "path": c.get("path", ""),
                    "line": c.get("line") or c.get("original_line"),
                    "created_at": c.get("created_at", ""),
                    "brad_responded": brad_replied,
                    "reply_count": len(replies),
                    "latest_reply": replies[-1].get("body", "")[:200] if replies else None,
                    "latest_reply_user": replies[-1].get("user", {}).get("login", "") if replies else None,
                })
            return jsonify({"comments": enriched})
        except Exception as e:
            logger.error(f"Failed to fetch comments for PR #{pr_number}: {e}")
            return jsonify({"error": str(e)}), 500

    def _get_github_adapter():
        from brad.adapters.code_repository.github_adapter import GitHubAdapter

        class _MiniCfg:
            pass

        github_auth = build_github_token_provider_from_env()
        if not github_auth.is_configured():
            raise ValueError(
                "GitHub auth is not configured. Set GITHUB_TOKEN or "
                "GITHUB_APP_ID + GITHUB_APP_INSTALLATION_ID + GITHUB_APP_PRIVATE_KEY[_PATH]."
            )

        cfg = _MiniCfg()
        cfg.github_token = os.environ.get("GITHUB_TOKEN", "")
        cfg.github_repo = github_repo
        cfg.github_app_id = os.environ.get("GITHUB_APP_ID") or None
        cfg.github_app_installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID") or None
        cfg.github_app_private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
        cfg.github_app_private_key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or None
        return GitHubAdapter(cfg)

    return app
