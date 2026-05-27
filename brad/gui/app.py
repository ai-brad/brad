"""Brad Web GUI — read-only Flask dashboard for execution history and costs."""
import os
import re
import signal
from typing import Callable, Optional

from flask import Flask, render_template, jsonify, request, redirect, url_for
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
    if status.lower() == "stopped":
        return "STOPPED"
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


def _make_pr_details_fetcher() -> Optional[Callable[[int], dict]]:
    github_repo = os.environ.get("GITHUB_REPO", "")
    if not github_repo:
        return None

    github_auth = build_github_token_provider_from_env()
    if not github_auth.is_configured():
        return None

    from pathlib import Path
    from brad.adapters.code_repository.github_adapter import GitHubAdapter

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.github_token = os.environ.get("GITHUB_TOKEN", "")
    cfg.github_repo = github_repo
    cfg.github_app_id = os.environ.get("GITHUB_APP_ID") or None
    cfg.github_app_installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID") or None
    cfg.github_app_private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
    cfg.github_app_private_key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or None
    cfg.attachments_dir = str(Path("/tmp"))
    adapter = GitHubAdapter(cfg)
    cache: dict[int, dict] = {}

    def fetch(pr_number: int) -> dict:
        pr_number = int(pr_number or 0)
        if pr_number <= 0:
            return {}
        if pr_number in cache:
            return cache[pr_number]
        try:
            details = adapter.get_pr_details(pr_number) or {}
        except Exception as exc:
            logger.debug("Could not backfill PR details for #%s: %s", pr_number, exc)
            details = {}
        cache[pr_number] = details
        return details

    return fetch


def format_pr_state_label(state: Optional[str]) -> str:
    state = (state or "").strip().lower()
    if state == "merged":
        return "MERGED"
    if state == "open":
        return "OPEN"
    if state == "closed":
        return "CLOSED"
    return ""


def format_pr_state_class(state: Optional[str]) -> str:
    state = (state or "").strip().lower()
    if state == "merged":
        return "badge-merged"
    if state == "open":
        return "badge-running"
    if state == "closed":
        return "badge-error"
    return ""


def decorate_execution(execution, issue_title_fetcher: Optional[Callable[[str], str]] = None):
    """Add display-only fields used by the list views."""
    ex = dict(execution or {})
    ex["issue_key"] = (ex.get("issue_key") or "").strip()
    issue_title = (ex.get("issue_title") or "").strip()
    ex["model_name"] = (ex.get("model_name") or "").strip()
    if not issue_title:
        if issue_title_fetcher:
            issue_title = issue_title_fetcher(ex.get("issue_key") or "")
        if not issue_title:
            summary = (ex.get("summary") or "").strip()
            issue_title = summary if summary and not _looks_like_action_summary(summary) else (ex.get("issue_key") or "")
    ex["issue_title"] = issue_title
    ex["action"] = format_execution_action(ex)
    ex["status_label"] = format_execution_status_label(ex.get("status"))
    ex["ticket_href"] = f"/ticket/{ex['issue_key']}" if ex["issue_key"] else ""
    return ex


def decorate_ticket(
    ticket,
    issue_title_fetcher: Optional[Callable[[str], str]] = None,
    pr_details_fetcher: Optional[Callable[[int], dict]] = None,
):
    """Add display-only fields used by the ticket overview."""
    tk = dict(ticket or {})
    tk["issue_key"] = (tk.get("issue_key") or "").strip()
    tk["latest_execution_model_name"] = (tk.get("latest_execution_model_name") or "").strip()
    tk["latest_execution_action"] = (tk.get("latest_execution_action") or "").strip()
    tk["latest_execution_phase"] = (tk.get("latest_execution_phase") or "").strip()
    tk["latest_execution_phase_detail"] = (tk.get("latest_execution_phase_detail") or "").strip()
    tk["issue_control_state"] = (tk.get("issue_control_state") or "running").strip().lower() or "running"
    tk["issue_control_reason"] = (tk.get("issue_control_reason") or "").strip()
    tk["issue_control_updated_at"] = (tk.get("issue_control_updated_at") or "").strip()
    tk["issue_control_updated_by"] = (tk.get("issue_control_updated_by") or "").strip()
    tk["latest_pr_state"] = (tk.get("latest_pr_state") or "").strip().lower()
    tk["latest_pr_merged_at"] = (tk.get("latest_pr_merged_at") or "").strip()
    issue_title = (tk.get("issue_title") or "").strip()
    if not issue_title:
        if issue_title_fetcher:
            issue_title = issue_title_fetcher(tk.get("issue_key") or "")
        if not issue_title:
            issue_title = tk.get("issue_key") or ""
    tk["issue_title"] = issue_title
    tk["latest_execution_status"] = (tk.get("latest_execution_status") or "running").strip().lower() or "running"
    latest_pr_number = int(tk.get("latest_pr_number") or 0)
    if latest_pr_number and pr_details_fetcher:
        try:
            pr_details = pr_details_fetcher(latest_pr_number) or {}
        except Exception as exc:
            logger.debug("Could not decorate PR #%s for ticket %s: %s", latest_pr_number, tk["issue_key"], exc)
            pr_details = {}
        latest_pr_state = (pr_details.get("state") or "").strip().lower()
        latest_pr_merged_at = (pr_details.get("merged_at") or "").strip()
        if latest_pr_merged_at:
            latest_pr_state = "merged"
        if latest_pr_state:
            tk["latest_pr_state"] = latest_pr_state
        if latest_pr_merged_at:
            tk["latest_pr_merged_at"] = latest_pr_merged_at
        tk["latest_pr_title"] = (pr_details.get("title") or tk.get("latest_pr_title") or "").strip()
        tk["latest_pr_url"] = (pr_details.get("html_url") or tk.get("latest_pr_url") or "").strip()
    tk["latest_pr_state_label"] = format_pr_state_label(tk["latest_pr_state"])
    tk["latest_pr_state_class"] = format_pr_state_class(tk["latest_pr_state"])
    is_running = bool(int(tk.get("is_running") or 0))
    if tk["issue_control_state"] == "stopped":
        display_status = "stopped"
    elif is_running:
        display_status = "running"
    else:
        display_status = tk["latest_execution_status"]
    tk["status"] = display_status
    tk["status_label"] = format_execution_status_label(display_status)
    tk["status_class"] = display_status
    tk["issue_control_state_label"] = tk["issue_control_state"].upper()
    tk["issue_href"] = f"/ticket/{tk['issue_key']}" if tk["issue_key"] else ""
    tk["latest_execution_href"] = f"/execution/{tk['latest_execution_id']}" if tk.get("latest_execution_id") else ""
    tk["total_prompt_tokens"] = int(tk.get("total_prompt_tokens") or 0)
    tk["total_cached_prompt_tokens"] = int(tk.get("total_cached_prompt_tokens") or 0)
    tk["total_completion_tokens"] = int(tk.get("total_completion_tokens") or 0)
    tk["total_cost"] = float(tk.get("total_cost") or 0.0)
    tk["execution_count"] = int(tk.get("execution_count") or 0)
    tk["is_running"] = is_running
    tk["latest_pr_badge_label"] = tk["latest_pr_state_label"]
    tk["latest_pr_badge_class"] = tk["latest_pr_state_class"]
    return tk


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


def build_execution_continuation_summary(execution):
    """Return restart-summary metadata for display on the execution page."""
    execution = execution or {}
    summary = (execution.get("continuation_summary") or "").strip()
    if not summary:
        return None
    return {
        "summary": summary,
        "source": (execution.get("continuation_summary_source") or "").strip(),
        "model_name": (execution.get("continuation_summary_model_name") or "").strip(),
        "prompt_tokens": int(execution.get("continuation_summary_prompt_tokens") or 0),
        "cached_prompt_tokens": int(execution.get("continuation_summary_cached_prompt_tokens") or 0),
        "completion_tokens": int(execution.get("continuation_summary_completion_tokens") or 0),
        "total_tokens": int(execution.get("continuation_summary_total_tokens") or 0),
        "cost": float(execution.get("continuation_summary_cost") or 0.0),
        "updated_at": (execution.get("continuation_summary_updated_at") or "").strip(),
    }


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
    pr_details_fetcher = _make_pr_details_fetcher()

    jira_url = os.environ.get("JIRA_URL", "https://jira.example.com")
    github_repo = os.environ.get("GITHUB_REPO", "")

    @app.context_processor
    def inject_globals():
        return {"jira_url": jira_url, "github_repo": github_repo}

    def _render_dashboard_redirect():
        return redirect(request.referrer or url_for("dashboard"))

    def _render_ticket_redirect(issue_key: str):
        issue_key = (issue_key or "").strip()
        return redirect(request.referrer or url_for("ticket_detail", issue_key=issue_key))

    def _maybe_kill_worker():
        running = db.get_running_execution()
        if not running:
            return
        worker_pid = running.get("worker_pid")
        if not worker_pid:
            return
        try:
            os.kill(int(worker_pid), signal.SIGTERM)
        except ProcessLookupError:
            logger.info("Worker PID %s no longer exists", worker_pid)
        except Exception as exc:
            logger.warning("Could not signal worker PID %s: %s", worker_pid, exc)

    @app.route("/")
    def dashboard():
        tickets = [decorate_ticket(t, issue_title_fetcher, pr_details_fetcher) for t in db.get_ticket_overview(limit=10)]
        running = tickets[0] if tickets and tickets[0].get("is_running") else None
        totals = db.get_total_ticket_stats()
        return render_template(
            "dashboard.html",
            running=running,
            tickets=tickets,
            totals=totals,
        )

    @app.route("/ticket/<issue_key>")
    def ticket_detail(issue_key):
        issue_key = (issue_key or "").strip()
        executions = [decorate_execution(ex, issue_title_fetcher) for ex in db.get_executions_by_issue(issue_key)]
        if not executions:
            return "Ticket not found", 404
        executions.sort(key=lambda ex: (ex.get("started_at") or ""), reverse=True)
        latest = executions[0]
        issue_control = db.get_issue_control_state(issue_key)
        ticket = decorate_ticket(
            {
                "issue_key": issue_key,
                "issue_title": latest.get("issue_title") or latest.get("summary") or issue_key,
                "execution_count": len(executions),
                "total_prompt_tokens": sum(int(ex.get("total_prompt_tokens") or 0) for ex in executions),
                "total_cached_prompt_tokens": sum(int(ex.get("total_cached_prompt_tokens") or 0) for ex in executions),
                "total_completion_tokens": sum(int(ex.get("total_completion_tokens") or 0) for ex in executions),
                "total_cost": sum(float(ex.get("total_cost") or 0.0) for ex in executions),
                "last_update": max((ex.get("last_update") or ex.get("started_at") or "") for ex in executions),
                "latest_execution_id": latest.get("id"),
                "latest_execution_status": latest.get("status"),
                "latest_execution_model_name": latest.get("model_name"),
                "latest_execution_action": latest.get("action"),
                "latest_execution_phase": latest.get("current_phase"),
                "latest_execution_phase_detail": latest.get("current_phase_detail"),
                "latest_pr_number": latest.get("pr_number"),
                "latest_pr_url": latest.get("pr_url"),
                "latest_started_at": latest.get("started_at"),
                "latest_finished_at": latest.get("finished_at"),
                "is_running": 1 if any((ex.get("status") == "running") for ex in executions) else 0,
                "issue_control_state": issue_control.get("state") or "running",
                "issue_control_reason": issue_control.get("reason") or "",
                "issue_control_updated_at": issue_control.get("updated_at") or "",
                "issue_control_updated_by": issue_control.get("updated_by") or "",
            },
            issue_title_fetcher,
            pr_details_fetcher,
        )
        ticket_cost_breakdown = build_ticket_cost_breakdown(issue_key)
        return render_template(
            "ticket_overview.html",
            ticket=ticket,
            executions=executions,
            ticket_cost_breakdown=ticket_cost_breakdown,
            issue_control=issue_control,
        )

    @app.route("/api/ticket/<issue_key>")
    def api_ticket_detail(issue_key):
        issue_key = (issue_key or "").strip()
        executions = [decorate_execution(ex, issue_title_fetcher) for ex in db.get_executions_by_issue(issue_key)]
        if not executions:
            return jsonify({"error": "not found"}), 404
        executions.sort(key=lambda ex: (ex.get("started_at") or ""), reverse=True)
        latest = executions[0]
        issue_control = db.get_issue_control_state(issue_key)
        ticket = decorate_ticket(
            {
                "issue_key": issue_key,
                "issue_title": latest.get("issue_title") or latest.get("summary") or issue_key,
                "execution_count": len(executions),
                "total_prompt_tokens": sum(int(ex.get("total_prompt_tokens") or 0) for ex in executions),
                "total_cached_prompt_tokens": sum(int(ex.get("total_cached_prompt_tokens") or 0) for ex in executions),
                "total_completion_tokens": sum(int(ex.get("total_completion_tokens") or 0) for ex in executions),
                "total_cost": sum(float(ex.get("total_cost") or 0.0) for ex in executions),
                "last_update": max((ex.get("last_update") or ex.get("started_at") or "") for ex in executions),
                "latest_execution_id": latest.get("id"),
                "latest_execution_status": latest.get("status"),
                "latest_execution_model_name": latest.get("model_name"),
                "latest_execution_action": latest.get("action"),
                "latest_execution_phase": latest.get("current_phase"),
                "latest_execution_phase_detail": latest.get("current_phase_detail"),
                "latest_pr_number": latest.get("pr_number"),
                "latest_pr_url": latest.get("pr_url"),
                "latest_started_at": latest.get("started_at"),
                "latest_finished_at": latest.get("finished_at"),
                "is_running": 1 if any((ex.get("status") == "running") for ex in executions) else 0,
                "issue_control_state": issue_control.get("state") or "running",
                "issue_control_reason": issue_control.get("reason") or "",
                "issue_control_updated_at": issue_control.get("updated_at") or "",
                "issue_control_updated_by": issue_control.get("updated_by") or "",
            },
            issue_title_fetcher,
            pr_details_fetcher,
        )
        ticket_cost_breakdown = build_ticket_cost_breakdown(issue_key)
        return jsonify({
            "ticket": ticket,
            "executions": executions,
            "ticket_cost_breakdown": ticket_cost_breakdown,
            "issue_control": issue_control,
        })

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
        continuation_summary = build_execution_continuation_summary(execution)
        failure_context = build_execution_failure_context(execution, steps)
        issue_control = db.get_issue_control_state(execution.get("issue_key"))
        return render_template(
            "ticket_detail.html",
            execution=execution,
            steps=steps,
            ci_runs=ci_runs,
            cost_breakdown=cost_breakdown,
            ticket_cost_breakdown=ticket_cost_breakdown,
            continuation_summary=continuation_summary,
            failure_context=failure_context,
            issue_control=issue_control,
        )

    @app.route("/api/status")
    def api_status():
        running = db.get_running_execution()
        running = decorate_execution(running, issue_title_fetcher) if running else None
        totals = db.get_total_costs()
        return jsonify({
            "backend_running": running is not None,
            "current_issue": running["issue_key"] if running else None,
            "current_ticket": running["issue_key"] if running else None,
            "totals": totals,
            "ticket_totals": db.get_total_ticket_stats(),
        })

    @app.route("/api/tickets")
    def api_tickets():
        tickets = [decorate_ticket(t, issue_title_fetcher, pr_details_fetcher) for t in db.get_ticket_overview(limit=200)]
        running = tickets[0] if tickets and tickets[0].get("is_running") else None
        totals = db.get_total_ticket_stats()
        return jsonify({
            "running": running,
            "tickets": tickets,
            "totals": totals,
        })

    @app.route("/api/dashboard")
    def api_dashboard():
        running = db.get_running_execution()
        running = decorate_execution(running, issue_title_fetcher) if running else None
        recent = [decorate_execution(ex, issue_title_fetcher) for ex in db.get_all_executions(limit=10)]
        tickets = [decorate_ticket(t, issue_title_fetcher, pr_details_fetcher) for t in db.get_ticket_overview(limit=10)]
        totals = db.get_total_costs()
        return jsonify({
            "running": running,
            "recent": recent,
            "tickets": tickets,
            "totals": totals,
            "ticket_totals": db.get_total_ticket_stats(),
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
        continuation_summary = build_execution_continuation_summary(execution)
        failure_context = build_execution_failure_context(execution, steps)
        issue_control = db.get_issue_control_state(execution.get("issue_key"))
        return jsonify({
            "execution": execution,
            "steps": steps,
            "ci_runs": ci_runs,
            "cost_breakdown": cost_breakdown,
            "ticket_cost_breakdown": ticket_cost_breakdown,
            "continuation_summary": continuation_summary,
            "failure_context": failure_context,
            "issue_control": issue_control,
        })

    @app.route("/api/execution/<int:execution_id>/stop", methods=["POST"])
    def api_execution_stop(execution_id):
        execution = db.get_execution(execution_id)
        if not execution:
            return jsonify({"error": "not found"}), 404
        payload = request.get_json(silent=True) or {}
        reason = (request.form.get("reason") or payload.get("reason") or "").strip()
        updated_by = request.remote_addr or "dashboard"
        control = db.set_issue_control_state(execution["issue_key"], "stopped", reason=reason, updated_by=updated_by)
        if request.is_json:
            return jsonify({"control": control})
        return _render_dashboard_redirect()

    @app.route("/api/execution/<int:execution_id>/resume", methods=["POST"])
    def api_execution_resume(execution_id):
        execution = db.get_execution(execution_id)
        if not execution:
            return jsonify({"error": "not found"}), 404
        payload = request.get_json(silent=True) or {}
        reason = (request.form.get("reason") or payload.get("reason") or "").strip()
        updated_by = request.remote_addr or "dashboard"
        control = db.set_issue_control_state(execution["issue_key"], "running", reason=reason, updated_by=updated_by)
        if request.is_json:
            return jsonify({"control": control})
        return _render_dashboard_redirect()

    @app.route("/api/ticket/<issue_key>/stop", methods=["POST"])
    def api_ticket_stop(issue_key):
        issue_key = (issue_key or "").strip()
        if not issue_key:
            return jsonify({"error": "issue_key required"}), 400
        payload = request.get_json(silent=True) or {}
        reason = (request.form.get("reason") or payload.get("reason") or "").strip()
        updated_by = request.remote_addr or "dashboard"
        control = db.set_issue_control_state(issue_key, "stopped", reason=reason, updated_by=updated_by)
        if request.is_json:
            return jsonify({"control": control})
        return _render_ticket_redirect(issue_key)

    @app.route("/api/ticket/<issue_key>/resume", methods=["POST"])
    def api_ticket_resume(issue_key):
        issue_key = (issue_key or "").strip()
        if not issue_key:
            return jsonify({"error": "issue_key required"}), 400
        payload = request.get_json(silent=True) or {}
        reason = (request.form.get("reason") or payload.get("reason") or "").strip()
        updated_by = request.remote_addr or "dashboard"
        control = db.set_issue_control_state(issue_key, "running", reason=reason, updated_by=updated_by)
        if request.is_json:
            return jsonify({"control": control})
        return _render_ticket_redirect(issue_key)

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
