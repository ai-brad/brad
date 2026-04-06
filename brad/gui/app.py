"""Brad Web GUI — read-only Flask dashboard for execution history and costs."""
import os
from flask import Flask, render_template, jsonify
from brad import db
from brad.logging_config import get_logger

logger = get_logger(__name__)


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

    jira_url = os.environ.get("JIRA_URL", "https://jira.example.com")
    github_repo = os.environ.get("GITHUB_REPO", "")

    @app.context_processor
    def inject_globals():
        return {"jira_url": jira_url, "github_repo": github_repo}

    @app.route("/")
    def dashboard():
        running = db.get_running_execution()
        recent = db.get_all_executions(limit=10)
        totals = db.get_total_costs()
        return render_template(
            "dashboard.html",
            running=running,
            recent=recent,
            totals=totals,
        )

    @app.route("/history")
    def history():
        executions = db.get_all_executions(limit=200)
        totals = db.get_total_costs()
        return render_template("history.html", executions=executions, totals=totals)

    @app.route("/execution/<int:execution_id>")
    def execution_detail(execution_id):
        execution = db.get_execution(execution_id)
        if not execution:
            return "Execution not found", 404
        steps = db.get_execution_steps(execution_id)
        ci_runs = db.get_execution_ci_runs(execution_id)
        return render_template(
            "ticket_detail.html",
            execution=execution,
            steps=steps,
            ci_runs=ci_runs,
        )

    @app.route("/api/status")
    def api_status():
        running = db.get_running_execution()
        totals = db.get_total_costs()
        return jsonify({
            "backend_running": running is not None,
            "current_issue": running["issue_key"] if running else None,
            "totals": totals,
        })

    @app.route("/api/dashboard")
    def api_dashboard():
        running = db.get_running_execution()
        recent = db.get_all_executions(limit=10)
        totals = db.get_total_costs()
        return jsonify({
            "running": running,
            "recent": recent,
            "totals": totals,
        })

    @app.route("/api/executions")
    def api_executions():
        executions = db.get_all_executions(limit=200)
        totals = db.get_total_costs()
        return jsonify({"executions": executions, "totals": totals})

    @app.route("/api/execution/<int:execution_id>")
    def api_execution_detail(execution_id):
        execution = db.get_execution(execution_id)
        if not execution:
            return jsonify({"error": "not found"}), 404
        steps = db.get_execution_steps(execution_id)
        ci_runs = db.get_execution_ci_runs(execution_id)
        return jsonify({
            "execution": execution,
            "steps": steps,
            "ci_runs": ci_runs,
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
        github_token = os.environ.get("GITHUB_TOKEN", "")
        if not github_token:
            return jsonify({"error": "GITHUB_TOKEN not configured"}), 500
        try:
            adapter = _get_github_adapter(github_token)
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
        github_token = os.environ.get("GITHUB_TOKEN", "")
        if not github_token:
            return jsonify({"error": "GITHUB_TOKEN not configured"}), 500
        try:
            adapter = _get_github_adapter(github_token)
            comments = adapter.fetch_review_comments(pr_number)
            enriched = []
            for c in comments:
                replies = adapter.get_comment_replies(pr_number, c["id"])
                brad_replied = any(
                    r.get("body", "").startswith("Brad reaction: ")
                    or r.get("body", "").startswith("Brad checking")
                    for r in replies
                )
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

    def _get_github_adapter(token):
        from brad.adapters.code_repository.github_adapter import GitHubAdapter

        class _MiniCfg:
            pass

        cfg = _MiniCfg()
        cfg.github_token = token
        cfg.github_repo = github_repo
        return GitHubAdapter(cfg)

    return app
