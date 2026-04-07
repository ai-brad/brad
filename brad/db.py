"""SQLite database for Brad execution history, costs, and step tracking."""
import sqlite3
import json
import os
import glob
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional
from contextlib import contextmanager
from brad.logging_config import get_logger

logger = get_logger(__name__)

_DB_PATH = None
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def init_db(db_path: str = "brad_data.db") -> None:
    """Initialize the database, running any pending up-migrations."""
    global _DB_PATH
    _DB_PATH = db_path
    logger.info(f"Initializing database at {db_path}")

    with _get_conn() as conn:
        # Create migration tracking table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL
            )
        """)
        _run_migrations(conn)
        _seed_default_model_costs(conn)
    logger.info("Database initialized")


def _run_migrations(conn) -> None:
    """Run all pending SQL migrations in order."""
    applied = {row[0] for row in conn.execute("SELECT name FROM _migrations").fetchall()}

    migration_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    for mf in migration_files:
        name = mf.name
        if name in applied:
            continue
        logger.info(f"Applying migration: {name}")
        sql = mf.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO _migrations (name, applied_at) VALUES (?, ?)",
            (name, _now()),
        )
        logger.info(f"Migration applied: {name}")


@contextmanager
def _get_conn():
    """Get a database connection as a context manager."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -------------------------
# Executions
# -------------------------

def create_execution(issue_key: str, summary: str = "") -> int:
    """Create a new execution record. Returns the execution ID."""
    with _get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO executions (issue_key, summary, started_at, status) VALUES (?, ?, ?, ?)",
            (issue_key, summary, _now(), "running"),
        )
        exec_id = cursor.lastrowid
        logger.info(f"Created execution #{exec_id} for {issue_key}")
        return exec_id


def finish_execution(execution_id: int, status: str = "completed", pr_number: Optional[int] = None, pr_url: Optional[str] = None, error_message: Optional[str] = None) -> None:
    """Mark an execution as finished."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE executions SET finished_at=?, status=?, pr_number=?, pr_url=?, error_message=? WHERE id=?",
            (_now(), status, pr_number, pr_url, error_message, execution_id),
        )
    logger.info(f"Execution #{execution_id} finished: {status}")


def update_execution_costs(execution_id: int, prompt_tokens: int, completion_tokens: int, cost: float) -> None:
    """Add token usage and cost to an execution's totals."""
    with _get_conn() as conn:
        conn.execute(
            """UPDATE executions
               SET total_prompt_tokens = total_prompt_tokens + ?,
                   total_completion_tokens = total_completion_tokens + ?,
                   total_cost = total_cost + ?
               WHERE id = ?""",
            (prompt_tokens, completion_tokens, cost, execution_id),
        )


def update_execution_pr(execution_id: int, pr_number: int, pr_url: str) -> None:
    """Update PR info on an execution as soon as the PR is created."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE executions SET pr_number=?, pr_url=? WHERE id=?",
            (pr_number, pr_url, execution_id),
        )
    logger.info(f"Execution #{execution_id} linked to PR #{pr_number}")


def update_execution_phase(execution_id: int, phase: str, detail: str = "") -> None:
    """Update the current phase and detail for live dashboard display."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE executions SET current_phase=?, current_phase_detail=? WHERE id=?",
            (phase, detail[:500] if detail else "", execution_id),
        )


def get_execution_cost(execution_id: int) -> float:
    """Get current total cost for an execution."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT total_cost FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
        return float(row["total_cost"]) if row else 0.0


def get_latest_execution_for_issue(issue_key: str) -> Optional[Dict]:
    """Get the most recent execution for a JIRA issue."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE issue_key=? ORDER BY started_at DESC LIMIT 1",
            (issue_key,),
        ).fetchone()
        return dict(row) if row else None


# -------------------------
# Steps
# -------------------------

def create_step(execution_id: int, phase: str, detail: str = "", iteration: int = 0) -> int:
    """Create a new step within an execution. Returns the step ID."""
    with _get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO steps (execution_id, phase, started_at, status, detail, iteration) VALUES (?, ?, ?, ?, ?, ?)",
            (execution_id, phase, _now(), "running", detail[:500] if detail else "", iteration),
        )
        step_id = cursor.lastrowid
        logger.debug(f"Created step #{step_id} ({phase}) for execution #{execution_id}")
        return step_id


def finish_step(step_id: int, status: str = "completed", prompt_tokens: int = 0, completion_tokens: int = 0, cost: float = 0.0, result_summary: Optional[str] = None) -> None:
    """Mark a step as finished with usage stats."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE steps SET finished_at=?, status=?, prompt_tokens=?, completion_tokens=?, cost=?, result_summary=? WHERE id=?",
            (_now(), status, prompt_tokens, completion_tokens, cost, result_summary, step_id),
        )


def update_step_detail(step_id: int, detail: str) -> None:
    """Update the detail text on a running step."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE steps SET detail=? WHERE id=?",
            (detail[:500] if detail else "", step_id),
        )


# -------------------------
# CI Runs
# -------------------------

def record_ci_run(execution_id: int, run_id: Optional[int], workflow_name: str, conclusion: str, logs_summary: Optional[str] = None) -> int:
    """Record a CI/CD run result."""
    with _get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO ci_runs (execution_id, run_id, workflow_name, conclusion, logs_summary, checked_at) VALUES (?, ?, ?, ?, ?, ?)",
            (execution_id, run_id, workflow_name, conclusion, logs_summary, _now()),
        )
        return cursor.lastrowid


# -------------------------
# Query helpers (used by GUI)
# -------------------------

def get_all_executions(limit: int = 100) -> List[Dict]:
    """Get all executions ordered by most recent update first (finished_at if available, otherwise started_at)."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT *, COALESCE(finished_at, started_at) as last_update FROM executions ORDER BY last_update DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_execution(execution_id: int) -> Optional[Dict]:
    """Get a single execution by ID."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
        return dict(row) if row else None


def get_execution_steps(execution_id: int) -> List[Dict]:
    """Get all steps for an execution."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM steps WHERE execution_id = ? ORDER BY started_at ASC",
            (execution_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_execution_ci_runs(execution_id: int) -> List[Dict]:
    """Get all CI runs for an execution."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ci_runs WHERE execution_id = ? ORDER BY checked_at ASC",
            (execution_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_executions_by_issue(issue_key: str) -> List[Dict]:
    """Get all executions for a specific issue."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM executions WHERE issue_key = ? ORDER BY started_at DESC",
            (issue_key,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_total_costs() -> Dict:
    """Get aggregate cost stats."""
    with _get_conn() as conn:
        row = conn.execute(
            """SELECT
                COUNT(*) as total_executions,
                SUM(total_prompt_tokens) as total_prompt_tokens,
                SUM(total_completion_tokens) as total_completion_tokens,
                SUM(total_cost) as total_cost
            FROM executions"""
        ).fetchone()
        return dict(row) if row else {}


def get_open_brad_prs() -> List[Dict]:
    """Get all executions that have a PR number and completed successfully.
    These PRs may still be open on GitHub and eligible for rebasing."""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT id, issue_key, pr_number, pr_url, status, started_at, finished_at
               FROM executions
               WHERE pr_number IS NOT NULL
                 AND status = 'completed'
               ORDER BY finished_at DESC""",
        ).fetchall()
        # Deduplicate by pr_number (keep most recent execution per PR)
        seen = set()
        result = []
        for r in rows:
            d = dict(r)
            if d["pr_number"] not in seen:
                seen.add(d["pr_number"])
                result.append(d)
        return result


def get_running_execution() -> Optional[Dict]:
    """Get the currently running execution, if any."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE status = 'running' ORDER BY started_at DESC LIMIT 1",
        ).fetchone()
        return dict(row) if row else None


def has_ongoing_work_for_pr(pr_number: int) -> bool:
    """Check if there's currently running work for this PR."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM executions WHERE pr_number = ? AND status = 'running' LIMIT 1",
            (pr_number,),
        ).fetchone()
        return row is not None


def pr_belongs_to_brad(pr_number: int) -> bool:
    """Check if a PR was created by this Brad instance (exists in DB)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM executions WHERE pr_number = ? LIMIT 1",
            (pr_number,),
        ).fetchone()
        return row is not None


# -------------------------
# Model costs (with TTL)
# -------------------------

_DEFAULT_MODEL_COSTS = [
    ("gpt-4o",          0.0025,  0.0100, "default"),
    ("gpt-4o-mini",     0.00015, 0.0006, "default"),
    ("gpt-4.1",         0.002,   0.008,  "default"),
    ("gpt-4.1-mini",    0.0004,  0.0016, "default"),
    ("gpt-4.1-nano",    0.0001,  0.0004, "default"),
    ("o3",              0.002,   0.008,  "default"),
    ("o3-mini",         0.0011,  0.0044, "default"),
    ("o4-mini",         0.0011,  0.0044, "default"),
    ("gpt-5.2-codex",   0.003,   0.012,  "default"),
]

_COST_TTL_HOURS = 24


def _seed_default_model_costs(conn):
    """Seed default model costs if the table is empty."""
    count = conn.execute("SELECT COUNT(*) FROM model_costs").fetchone()[0]
    if count > 0:
        return
    now = _now()
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=_COST_TTL_HOURS)).isoformat()
    for pattern, prompt_cost, completion_cost, source in _DEFAULT_MODEL_COSTS:
        conn.execute(
            "INSERT OR IGNORE INTO model_costs (model_pattern, prompt_cost_per_1k, completion_cost_per_1k, updated_at, expires_at, source) VALUES (?, ?, ?, ?, ?, ?)",
            (pattern, prompt_cost, completion_cost, now, expires, source),
        )


def get_model_cost(model_name: str) -> Dict:
    """Get per-1k-token costs for a model. Returns {'prompt': float, 'completion': float}.
    Falls back to best-match pattern, then zero."""
    with _get_conn() as conn:
        # Exact match first
        row = conn.execute(
            "SELECT prompt_cost_per_1k, completion_cost_per_1k, expires_at FROM model_costs WHERE model_pattern = ?",
            (model_name,),
        ).fetchone()
        if row:
            return {"prompt": row["prompt_cost_per_1k"], "completion": row["completion_cost_per_1k"]}

        # Prefix match (e.g. 'gpt-4o' matches 'gpt-4o-2024-11-20')
        rows = conn.execute(
            "SELECT model_pattern, prompt_cost_per_1k, completion_cost_per_1k FROM model_costs ORDER BY LENGTH(model_pattern) DESC"
        ).fetchall()
        for r in rows:
            if model_name.startswith(r["model_pattern"]):
                return {"prompt": r["prompt_cost_per_1k"], "completion": r["completion_cost_per_1k"]}

    return {"prompt": 0.0, "completion": 0.0}


def upsert_model_cost(model_pattern: str, prompt_cost_per_1k: float, completion_cost_per_1k: float, source: str = "manual") -> None:
    """Insert or update a model cost entry, resetting TTL."""
    from datetime import timedelta
    now = _now()
    expires = (datetime.now(timezone.utc) + timedelta(hours=_COST_TTL_HOURS)).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO model_costs (model_pattern, prompt_cost_per_1k, completion_cost_per_1k, updated_at, expires_at, source)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_pattern) DO UPDATE SET
                   prompt_cost_per_1k=excluded.prompt_cost_per_1k,
                   completion_cost_per_1k=excluded.completion_cost_per_1k,
                   updated_at=excluded.updated_at,
                   expires_at=excluded.expires_at,
                   source=excluded.source""",
            (model_pattern, prompt_cost_per_1k, completion_cost_per_1k, now, expires, source),
        )


def get_all_model_costs() -> List[Dict]:
    """Get all model cost entries."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM model_costs ORDER BY model_pattern").fetchall()
        return [dict(r) for r in rows]


# ---- Repo metadata cache ----

def get_repo_metadata(repo_path: str, key: str) -> Optional[str]:
    """Get a cached metadata value for a repo."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM repo_metadata WHERE repo_path = ? AND key = ?",
            (repo_path, key),
        ).fetchone()
        return row["value"] if row else None


def set_repo_metadata(repo_path: str, key: str, value: str, source_file: str = "") -> None:
    """Cache a metadata value for a repo (upsert)."""
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO repo_metadata (repo_path, key, value, source_file, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(repo_path, key) DO UPDATE SET
                   value=excluded.value,
                   source_file=excluded.source_file,
                   updated_at=excluded.updated_at""",
            (repo_path, key, value, source_file, _now()),
        )
