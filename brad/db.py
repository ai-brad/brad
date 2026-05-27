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
_EXECUTION_RUNTIME_OBSERVER = None


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
        _ensure_control_state(conn)
        _ensure_issue_control_state(conn)
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


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ensure_control_state(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS brad_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT NOT NULL DEFAULT 'running',
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO brad_control (id, state, reason, updated_at, updated_by)
        VALUES (1, 'running', '', '', '')
        """
    )


def _ensure_issue_control_state(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_control (
            issue_key TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'running',
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT ''
        )
        """
    )


def set_execution_runtime_observer(observer) -> None:
    """Register a worker-local execution observer for liveness tracking."""
    global _EXECUTION_RUNTIME_OBSERVER
    _EXECUTION_RUNTIME_OBSERVER = observer


def _notify_execution_observer(method_name: str, *args) -> None:
    observer = _EXECUTION_RUNTIME_OBSERVER
    if not observer:
        return
    method = getattr(observer, method_name, None)
    if not method:
        return
    try:
        method(*args)
    except Exception as exc:
        logger.warning("Execution runtime observer %s failed: %s", method_name, exc)


# -------------------------
# Executions
# -------------------------

def create_execution(
    issue_key: str,
    summary: str = "",
    cost_budget: Optional[float] = None,
    issue_title: str = "",
    action: Optional[str] = None,
    model_name: Optional[str] = None,
) -> int:
    """Create a new execution record. Returns the execution ID."""
    created_at = _now()
    action_value = (action if action is not None else summary) or ""
    model_value = (model_name or "").strip() or None
    with _get_conn() as conn:
        if cost_budget is None:
            cursor = conn.execute(
                """
                INSERT INTO executions (
                    issue_key, summary, issue_title, action, model_name, started_at, status, last_progress_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_key, summary, issue_title, action_value, model_value, created_at, "running", created_at),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO executions (
                    issue_key, summary, issue_title, action, model_name, started_at, status, last_progress_at, cost_budget
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_key,
                    summary,
                    issue_title,
                    action_value,
                    model_value,
                    created_at,
                    "running",
                    created_at,
                    float(cost_budget),
                ),
            )
        exec_id = cursor.lastrowid
    logger.info(f"Created execution #{exec_id} for {issue_key}")
    _notify_execution_observer("on_execution_created", exec_id, issue_key)
    return exec_id


def finish_execution(execution_id: int, status: str = "completed", pr_number: Optional[int] = None, pr_url: Optional[str] = None, error_message: Optional[str] = None) -> None:
    """Mark an execution as finished."""
    finished_at = _now()
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE executions
               SET finished_at=?,
                   status=?,
                   pr_number=?,
                   pr_url=?,
                   error_message=?,
                   last_progress_at=COALESCE(?, last_progress_at),
                   last_heartbeat_at=COALESCE(last_heartbeat_at, ?)
             WHERE id=?
            """,
            (finished_at, status, pr_number, pr_url, error_message, finished_at, finished_at, execution_id),
        )
    logger.info(f"Execution #{execution_id} finished: {status}")
    _notify_execution_observer("on_execution_finished", execution_id, status, error_message)


def reconcile_running_executions(
    error_message: str = "Worker restarted during execution",
    stale_after_seconds: int = 300,
    current_worker_id: Optional[str] = None,
    assume_single_worker: bool = True,
) -> int:
    """Mark any stale running executions/steps as failed.

    This should only be called by the worker process during startup. The GUI also
    initializes the database and must not mutate execution state.
    """
    stale_after_seconds = max(1, int(stale_after_seconds))
    now = datetime.now(timezone.utc)
    stale_ids = []

    with _get_conn() as conn:
        running = conn.execute(
            """
            SELECT id, issue_key, started_at, last_progress_at, last_heartbeat_at, worker_id
              FROM executions
             WHERE status = 'running'
            """
        ).fetchall()
        if not running:
            return 0

        for row in running:
            freshness = (
                _parse_timestamp(row["last_heartbeat_at"])
                or _parse_timestamp(row["last_progress_at"])
                or _parse_timestamp(row["started_at"])
            )
            age_seconds = (now - freshness).total_seconds() if freshness else float("inf")
            owned_by_other_worker = (
                assume_single_worker
                and current_worker_id is not None
                and row["worker_id"] is not None
                and row["worker_id"] != current_worker_id
            )
            legacy_unowned = assume_single_worker and row["worker_id"] is None

            if owned_by_other_worker or legacy_unowned or age_seconds > stale_after_seconds:
                stale_ids.append(row["id"])

        if not stale_ids:
            return 0

        finished_at = _now()
        placeholders = ",".join("?" for _ in stale_ids)

        conn.execute(
            f"""
            UPDATE executions
               SET status = 'failed',
                   finished_at = COALESCE(finished_at, ?),
                   error_message = COALESCE(error_message, ?),
                   last_progress_at = COALESCE(last_progress_at, ?),
                   last_heartbeat_at = COALESCE(last_heartbeat_at, ?),
                   current_phase_detail = CASE
                       WHEN current_phase_detail IS NULL OR current_phase_detail = ''
                       THEN ?
                       ELSE current_phase_detail
                   END
             WHERE id IN ({placeholders})
            """,
            (finished_at, error_message, finished_at, finished_at, error_message, *stale_ids),
        )
        conn.execute(
            f"""
            UPDATE steps
               SET status = 'failed',
                   finished_at = COALESCE(finished_at, ?),
                   result_summary = COALESCE(result_summary, ?)
             WHERE execution_id IN ({placeholders})
               AND status = 'running'
            """,
            (finished_at, error_message, *stale_ids),
        )

    logger.warning(
        "Reconciled %d stale running execution(s) on startup: %s",
        len(stale_ids),
        stale_ids,
    )
    return len(stale_ids)


def update_execution_costs(
    execution_id: int,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    cached_prompt_tokens: int = 0,
) -> None:
    """Add token usage and cost to an execution's totals."""
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """UPDATE executions
               SET total_prompt_tokens = total_prompt_tokens + ?,
                   total_cached_prompt_tokens = total_cached_prompt_tokens + ?,
                   total_completion_tokens = total_completion_tokens + ?,
                   total_cost = total_cost + ?,
                   last_progress_at = ?
               WHERE id = ?""",
            (prompt_tokens, cached_prompt_tokens, completion_tokens, cost, now, execution_id),
        )


def update_execution_continuation_summary(
    execution_id: int,
    summary: str,
    *,
    source: str = "",
    model_name: str = "",
    prompt_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
) -> None:
    """Persist the latest restart summary metadata on an execution."""
    updated_at = _now()
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE executions
               SET continuation_summary=?,
                   continuation_summary_source=?,
                   continuation_summary_model_name=?,
                   continuation_summary_prompt_tokens=?,
                   continuation_summary_cached_prompt_tokens=?,
                   continuation_summary_completion_tokens=?,
                   continuation_summary_total_tokens=?,
                   continuation_summary_cost=?,
                   continuation_summary_updated_at=?
             WHERE id=?
            """,
            (
                (summary or "").strip(),
                (source or "").strip(),
                (model_name or "").strip(),
                int(prompt_tokens or 0),
                int(cached_prompt_tokens or 0),
                int(completion_tokens or 0),
                int((prompt_tokens or 0) + (completion_tokens or 0)),
                float(cost or 0.0),
                updated_at,
                execution_id,
            ),
        )


def update_execution_pr(execution_id: int, pr_number: int, pr_url: str) -> None:
    """Update PR info on an execution as soon as the PR is created."""
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE executions SET pr_number=?, pr_url=?, last_progress_at=? WHERE id=?",
            (pr_number, pr_url, now, execution_id),
        )
    logger.info(f"Execution #{execution_id} linked to PR #{pr_number}")


def update_execution_issue_title(execution_id: int, issue_title: str) -> None:
    """Persist the Jira title for an execution."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE executions SET issue_title=? WHERE id=?",
            ((issue_title or "").strip(), execution_id),
        )


def update_issue_title_for_issue(issue_key: str, issue_title: str) -> None:
    """Persist the Jira title across all executions for a given issue key."""
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE executions
               SET issue_title=?
             WHERE issue_key=?
               AND (issue_title IS NULL OR issue_title = '')
            """,
            ((issue_title or "").strip(), issue_key),
        )


def update_execution_action(execution_id: int, action: str) -> None:
    """Persist the short action label for an execution."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE executions SET action=? WHERE id=?",
            ((action or "").strip(), execution_id),
        )


def get_control_state() -> Dict:
    """Return Brad's global execution control state."""
    try:
        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM brad_control WHERE id = 1").fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return {
        "id": 1,
        "state": "running",
        "reason": "",
        "updated_at": "",
        "updated_by": "",
    }


def get_issue_control_state(issue_key: str) -> Dict:
    """Return the stop state for a specific issue."""
    issue_key = (issue_key or "").strip()
    if not issue_key:
        return {
            "issue_key": "",
            "state": "running",
            "reason": "",
            "updated_at": "",
            "updated_by": "",
        }
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT issue_key, state, reason, updated_at, updated_by FROM issue_control WHERE issue_key = ?",
                (issue_key,),
            ).fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return {
        "issue_key": issue_key,
        "state": "running",
        "reason": "",
        "updated_at": "",
        "updated_by": "",
    }


def set_issue_control_state(issue_key: str, state: str, reason: str = "", updated_by: str = "") -> Dict:
    """Update the stop state for a specific issue."""
    issue_key = (issue_key or "").strip()
    if not issue_key:
        raise ValueError("issue_key must be provided")

    normalized_state = (state or "").strip().lower()
    if normalized_state not in {"running", "stopped"}:
        raise ValueError(f"Unknown issue control state: {state!r}")

    updated_at = _now()
    with _get_conn() as conn:
        _ensure_issue_control_state(conn)
        conn.execute(
            """
            INSERT INTO issue_control (issue_key, state, reason, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(issue_key) DO UPDATE SET
                state=excluded.state,
                reason=excluded.reason,
                updated_at=excluded.updated_at,
                updated_by=excluded.updated_by
            """,
            (issue_key, normalized_state, (reason or "").strip(), updated_at, (updated_by or "").strip()),
        )
    return get_issue_control_state(issue_key)


def is_brad_stopped(issue_key: Optional[str] = None) -> bool:
    """True when the given issue has been paused."""
    issue_key = (issue_key or "").strip()
    if not issue_key:
        return False
    return (get_issue_control_state(issue_key).get("state") or "").strip().lower() == "stopped"


def set_control_state(state: str, reason: str = "", updated_by: str = "") -> Dict:
    """Update Brad's global execution control state."""
    normalized_state = (state or "").strip().lower()
    if normalized_state not in {"running", "stopped"}:
        raise ValueError(f"Unknown Brad control state: {state!r}")

    updated_at = _now()
    with _get_conn() as conn:
        _ensure_control_state(conn)
        conn.execute(
            """
            UPDATE brad_control
               SET state=?,
                   reason=?,
                   updated_at=?,
                   updated_by=?
             WHERE id=1
            """,
            (normalized_state, (reason or "").strip(), updated_at, (updated_by or "").strip()),
        )
    return get_control_state()


def update_execution_phase(execution_id: int, phase: str, detail: str = "") -> None:
    """Update the current phase and detail for live dashboard display."""
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE executions
               SET current_phase=?,
                   current_phase_detail=?,
                   last_progress_at=?
             WHERE id=?
            """,
            (phase, detail[:500] if detail else "", now, execution_id),
        )
    _notify_execution_observer("on_execution_phase_changed", execution_id, phase, detail)


def touch_execution_liveness(
    execution_id: int,
    *,
    worker_id: Optional[str] = None,
    worker_pid: Optional[int] = None,
    current_memory_rss_bytes: Optional[int] = None,
    peak_memory_rss_bytes: Optional[int] = None,
    progress: bool = False,
) -> None:
    """Update heartbeat/ownership/memory fields on a running execution."""
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE executions
               SET worker_id = COALESCE(?, worker_id),
                   worker_pid = COALESCE(?, worker_pid),
                   last_heartbeat_at = ?,
                   last_progress_at = CASE WHEN ? THEN ? ELSE last_progress_at END,
                   last_memory_rss_bytes = COALESCE(?, last_memory_rss_bytes),
                   peak_memory_rss_bytes = CASE
                       WHEN ? IS NULL THEN peak_memory_rss_bytes
                       WHEN peak_memory_rss_bytes IS NULL THEN ?
                       WHEN ? > peak_memory_rss_bytes THEN ?
                       ELSE peak_memory_rss_bytes
                   END
             WHERE id = ?
               AND status = 'running'
            """,
            (
                worker_id,
                worker_pid,
                now,
                1 if progress else 0,
                now,
                current_memory_rss_bytes,
                peak_memory_rss_bytes,
                peak_memory_rss_bytes,
                peak_memory_rss_bytes,
                peak_memory_rss_bytes,
                execution_id,
            ),
        )


def get_execution_cost(execution_id: int) -> float:
    """Get current total cost for an execution."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT total_cost FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
        return float(row["total_cost"]) if row else 0.0


def estimate_execution_cost(
    execution: Dict,
    fallback_model_name: str = "gpt-5.4",
) -> float:
    """Estimate an execution's cost from token totals and model pricing."""
    model_name = (execution.get("model_name") or fallback_model_name or "").strip()
    costs = get_model_cost(model_name)
    prompt_tokens = int(execution.get("total_prompt_tokens") or 0)
    cached_prompt_tokens = int(execution.get("total_cached_prompt_tokens") or 0)
    completion_tokens = int(execution.get("total_completion_tokens") or 0)
    non_cached_prompt_tokens = max(0, prompt_tokens - cached_prompt_tokens)
    return (
        (non_cached_prompt_tokens / 1000.0) * float(costs.get("prompt", 0.0) or 0.0)
        + (cached_prompt_tokens / 1000.0) * float(costs.get("cached_prompt", 0.0) or 0.0)
        + (completion_tokens / 1000.0) * float(costs.get("completion", 0.0) or 0.0)
    )


def get_zero_cost_executions(
    limit: Optional[int] = None,
    issue_key: Optional[str] = None,
) -> List[Dict]:
    """Return finished executions that still have zero recorded cost."""
    issue_key = (issue_key or "").strip()
    sql = [
        "SELECT *",
        "  FROM executions",
        " WHERE COALESCE(total_cost, 0.0) = 0.0",
        "   AND status != 'running'",
        "   AND (",
        "       COALESCE(total_prompt_tokens, 0) > 0",
        "    OR COALESCE(total_cached_prompt_tokens, 0) > 0",
        "    OR COALESCE(total_completion_tokens, 0) > 0",
        "   )",
    ]
    params = []
    if issue_key:
        sql.insert(3, "   AND issue_key = ?")
        params.append(issue_key)
    sql.append(" ORDER BY COALESCE(finished_at, started_at) ASC, id ASC")
    if limit is not None:
        sql.append(" LIMIT ?")
        params.append(max(1, int(limit)))
    with _get_conn() as conn:
        rows = conn.execute("\n".join(sql), params).fetchall()
        return [dict(row) for row in rows]


def backfill_zero_cost_executions(
    limit: Optional[int] = None,
    issue_key: Optional[str] = None,
    fallback_model_name: str = "gpt-5.4",
) -> List[Dict]:
    """Recompute missing execution costs for finished zero-cost rows.

    Returns a list of backfilled rows with the computed cost for reporting.
    """
    updates: List[Dict] = []
    executions = get_zero_cost_executions(limit=limit, issue_key=issue_key)
    if not executions:
        return updates

    with _get_conn() as conn:
        for execution in executions:
            exec_id = int(execution.get("id") or 0)
            if not exec_id:
                continue
            cost = estimate_execution_cost(execution, fallback_model_name=fallback_model_name)
            if cost <= 0.0:
                continue
            conn.execute(
                """
                UPDATE executions
                   SET total_cost = ?
                 WHERE id = ?
                   AND COALESCE(total_cost, 0.0) = 0.0
                   AND status != 'running'
                """,
                (float(cost), exec_id),
            )
            updates.append(
                {
                    "id": exec_id,
                    "issue_key": execution.get("issue_key") or "",
                    "model_name": (execution.get("model_name") or fallback_model_name or "").strip(),
                    "total_prompt_tokens": int(execution.get("total_prompt_tokens") or 0),
                    "total_cached_prompt_tokens": int(execution.get("total_cached_prompt_tokens") or 0),
                    "total_completion_tokens": int(execution.get("total_completion_tokens") or 0),
                    "cost": float(cost),
                }
            )
    return updates


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
    started_at = _now()
    with _get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO steps (execution_id, phase, started_at, status, detail, iteration) VALUES (?, ?, ?, ?, ?, ?)",
            (execution_id, phase, started_at, "running", detail[:500] if detail else "", iteration),
        )
        conn.execute(
            "UPDATE executions SET last_progress_at=? WHERE id=?",
            (started_at, execution_id),
        )
        step_id = cursor.lastrowid
        logger.debug(f"Created step #{step_id} ({phase}) for execution #{execution_id}")
        return step_id


def finish_step(
    step_id: int,
    status: str = "completed",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
    result_summary: Optional[str] = None,
    cached_prompt_tokens: int = 0,
) -> None:
    """Mark a step as finished with usage stats."""
    finished_at = _now()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE steps SET finished_at=?, status=?, prompt_tokens=?, cached_prompt_tokens=?, completion_tokens=?, cost=?, result_summary=? WHERE id=?",
            (finished_at, status, prompt_tokens, cached_prompt_tokens, completion_tokens, cost, result_summary, step_id),
        )
        conn.execute(
            """
            UPDATE executions
               SET last_progress_at=?
             WHERE id = (SELECT execution_id FROM steps WHERE id = ?)
            """,
            (finished_at, step_id),
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
    checked_at = _now()
    with _get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO ci_runs (execution_id, run_id, workflow_name, conclusion, logs_summary, checked_at) VALUES (?, ?, ?, ?, ?, ?)",
            (execution_id, run_id, workflow_name, conclusion, logs_summary, checked_at),
        )
        conn.execute(
            "UPDATE executions SET last_progress_at=? WHERE id=?",
            (checked_at, execution_id),
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


def get_ticket_overview(limit: int = 100) -> List[Dict]:
    """Get one grouped row per ticket, ordered by running work first then recency."""
    limit = max(1, int(limit or 0))
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                e.issue_key AS issue_key,
                COUNT(*) AS execution_count,
                SUM(COALESCE(e.total_prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(e.total_cached_prompt_tokens, 0)) AS total_cached_prompt_tokens,
                SUM(COALESCE(e.total_completion_tokens, 0)) AS total_completion_tokens,
                SUM(COALESCE(e.total_cost, 0.0)) AS total_cost,
                MAX(COALESCE(e.finished_at, e.started_at)) AS last_update,
                MAX(CASE WHEN e.status = 'running' THEN 1 ELSE 0 END) AS is_running,
                (
                    SELECT x.id
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS latest_execution_id,
                (
                    SELECT x.status
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS latest_execution_status,
                (
                    SELECT x.model_name
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS latest_execution_model_name,
                (
                    SELECT x.action
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS latest_execution_action,
                (
                    SELECT x.current_phase
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS latest_execution_phase,
                (
                    SELECT x.current_phase_detail
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS latest_execution_phase_detail,
                (
                    SELECT x.pr_number
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS latest_pr_number,
                (
                    SELECT x.pr_url
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS latest_pr_url,
                (
                    SELECT NULLIF(x.issue_title, '')
                      FROM executions x
                     WHERE x.issue_key = e.issue_key
                       AND COALESCE(x.issue_title, '') <> ''
                     ORDER BY COALESCE(x.finished_at, x.started_at) DESC, x.id DESC
                     LIMIT 1
                ) AS issue_title,
                COALESCE(ic.state, 'running') AS issue_control_state,
                COALESCE(ic.reason, '') AS issue_control_reason,
                COALESCE(ic.updated_at, '') AS issue_control_updated_at,
                COALESCE(ic.updated_by, '') AS issue_control_updated_by
              FROM executions e
         LEFT JOIN issue_control ic ON ic.issue_key = e.issue_key
          GROUP BY e.issue_key
          ORDER BY is_running DESC, last_update DESC, e.issue_key ASC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_total_ticket_stats() -> Dict:
    """Get aggregate stats for the ticket-centric dashboard."""
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(DISTINCT issue_key) AS total_tickets,
                COUNT(*) AS total_executions,
                COUNT(DISTINCT CASE WHEN status = 'running' THEN issue_key END) AS running_tickets,
                SUM(total_prompt_tokens) AS total_prompt_tokens,
                SUM(total_cached_prompt_tokens) AS total_cached_prompt_tokens,
                SUM(total_completion_tokens) AS total_completion_tokens,
                SUM(total_cost) AS total_cost
              FROM executions
            """
        ).fetchone()
        return dict(row) if row else {}


def get_ticket_cost_breakdown(issue_key: str) -> List[Dict]:
    """Aggregate step costs across all executions for a Jira issue."""
    issue_key = (issue_key or "").strip()
    if not issue_key:
        return []

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                s.phase AS phase,
                COUNT(*) AS steps,
                SUM(COALESCE(s.prompt_tokens, 0) - COALESCE(s.cached_prompt_tokens, 0)) AS non_cached_prompt_tokens,
                SUM(COALESCE(s.cached_prompt_tokens, 0)) AS cached_prompt_tokens,
                SUM(COALESCE(s.completion_tokens, 0)) AS completion_tokens,
                SUM(COALESCE(s.prompt_tokens, 0)) AS total_input_tokens,
                SUM(COALESCE(s.prompt_tokens, 0) + COALESCE(s.completion_tokens, 0)) AS total_tokens,
                SUM(COALESCE(s.cost, 0.0)) AS cost,
                MIN(s.started_at) AS first_started_at
              FROM executions e
              JOIN steps s ON s.execution_id = e.id
             WHERE e.issue_key = ?
             GROUP BY s.phase
             ORDER BY first_started_at ASC
            """,
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
                SUM(total_cached_prompt_tokens) as total_cached_prompt_tokens,
                SUM(total_completion_tokens) as total_completion_tokens,
                SUM(total_cost) as total_cost
            FROM executions"""
        ).fetchone()
        return dict(row) if row else {}


def get_running_execution(stale_after_seconds: int = 300) -> Optional[Dict]:
    """Get the freshest currently-running execution, excluding stale rows."""
    stale_after_seconds = max(1, int(stale_after_seconds))
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM executions WHERE status = 'running' ORDER BY started_at DESC",
        ).fetchall()
        for row in rows:
            data = dict(row)
            freshness = (
                _parse_timestamp(data.get("last_heartbeat_at"))
                or _parse_timestamp(data.get("last_progress_at"))
                or _parse_timestamp(data.get("started_at"))
            )
            if not freshness:
                continue
            if (now - freshness).total_seconds() <= stale_after_seconds:
                return data
        return None


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
# Processed PR comments (dedupe)
# -------------------------

# Kinds supported by pr_processed_comments. Kept as a module constant so
# callers can't accidentally introduce typos that would silently bypass
# dedupe.
PROCESSED_COMMENT_KINDS = ("review", "review_level", "issue")


def get_processed_comment_ids(pr_number: int, kind: str) -> set:
    """Return the set of comment IDs already processed for (pr_number, kind).

    Used by the review/issue comment paths to skip anything Brad has already
    acted on. Unknown kinds return an empty set (fail-open) so a caller bug
    degrades to "old behaviour" rather than "silent skip everything".
    """
    if kind not in PROCESSED_COMMENT_KINDS:
        logger.warning(f"get_processed_comment_ids: unknown kind '{kind}'")
        return set()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT comment_id FROM pr_processed_comments "
            "WHERE pr_number = ? AND kind = ?",
            (pr_number, kind),
        ).fetchall()
        return {row["comment_id"] for row in rows}


def mark_comments_processed(
    pr_number: int,
    kind: str,
    comments: List[Dict],
    skip_reason: Optional[str] = None,
) -> int:
    """Mark a batch of comments as processed.

    ``comments`` is a list of GitHub-shaped dicts (must contain 'id';
    optionally 'user.login' for observability). Returns the number of rows
    actually inserted. Existing (pr, kind, id) rows are left untouched so
    we never overwrite an earlier skip_reason with a later one.
    """
    if kind not in PROCESSED_COMMENT_KINDS:
        raise ValueError(f"mark_comments_processed: unknown kind '{kind}'")
    if not comments:
        return 0
    now = _now()
    rows = []
    for c in comments:
        cid = c.get("id")
        if cid is None:
            continue
        author = (c.get("user") or {}).get("login") if isinstance(c.get("user"), dict) else None
        rows.append((pr_number, kind, int(cid), author, now, skip_reason))
    if not rows:
        return 0
    with _get_conn() as conn:
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO pr_processed_comments "
            "(pr_number, kind, comment_id, author, processed_at, skip_reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        return cursor.rowcount or 0


def filter_unprocessed_comments(
    pr_number: int,
    kind: str,
    comments: List[Dict],
) -> List[Dict]:
    """Return only the comments not yet in pr_processed_comments.

    Preserves input order. This is the single chokepoint the orchestrator
    calls right after fetching "needing response" lists.
    """
    if not comments:
        return comments
    known = get_processed_comment_ids(pr_number, kind)
    if not known:
        return comments
    return [c for c in comments if c.get("id") not in known]


# -------------------------
# Model costs (with TTL)
# -------------------------

_DEFAULT_MODEL_COSTS = [
    ("gpt-4o",          0.0025,  None,     0.0100, "default"),
    ("gpt-4o-mini",     0.00015, None,     0.0006, "default"),
    ("gpt-4.1",         0.002,   None,     0.008,  "default"),
    ("gpt-4.1-mini",    0.0004,  None,     0.0016, "default"),
    ("gpt-4.1-nano",    0.0001,  None,     0.0004, "default"),
    ("o3",              0.002,   None,     0.008,  "default"),
    ("o3-mini",         0.0011,  None,     0.0044, "default"),
    ("o4-mini",         0.0011,  None,     0.0044, "default"),
    ("gpt-5.2-codex",   0.003,   None,     0.012,  "default"),
    ("gpt-5-codex",     0.00125, None,     0.0100, "default"),
    ("gpt-5.4",         0.0025,  0.00025, 0.015,   "default"),
    ("gpt-5.4-mini",    0.00075, 0.000075, 0.0045,  "default"),
]

_COST_TTL_HOURS = 24


def _seed_default_model_costs(conn):
    """Seed missing default model costs and refresh stale default-backed rows.

    Manual overrides are preserved by leaving any non-default ``source`` rows
    untouched. Default-managed rows are updated so newly added cached-input
    pricing or corrected defaults propagate to existing databases.
    """
    now = _now()
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=_COST_TTL_HOURS)).isoformat()
    for pattern, prompt_cost, cached_prompt_cost, completion_cost, source in _DEFAULT_MODEL_COSTS:
        row = conn.execute(
            """
            SELECT source
              FROM model_costs
             WHERE model_pattern = ?
            """,
            (pattern,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO model_costs (
                    model_pattern,
                    prompt_cost_per_1k,
                    cached_prompt_cost_per_1k,
                    completion_cost_per_1k,
                    updated_at,
                    expires_at,
                    source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pattern, prompt_cost, cached_prompt_cost, completion_cost, now, expires, source),
            )
            continue

        if row["source"] not in (None, "default"):
            continue

        conn.execute(
            """
            UPDATE model_costs
               SET prompt_cost_per_1k = ?,
                   cached_prompt_cost_per_1k = ?,
                   completion_cost_per_1k = ?,
                   updated_at = ?,
                   expires_at = ?,
                   source = ?
             WHERE model_pattern = ?
            """,
            (prompt_cost, cached_prompt_cost, completion_cost, now, expires, source, pattern),
        )


def get_model_cost(model_name: str) -> Dict:
    """Get per-1k-token costs for a model.

    Returns {'prompt': float, 'cached_prompt': float, 'completion': float}.
    Falls back to best-match pattern, then zero."""
    with _get_conn() as conn:
        # Exact match first
        row = conn.execute(
            "SELECT prompt_cost_per_1k, cached_prompt_cost_per_1k, completion_cost_per_1k, expires_at FROM model_costs WHERE model_pattern = ?",
            (model_name,),
        ).fetchone()
        if row:
            cached_prompt = row["cached_prompt_cost_per_1k"]
            return {
                "prompt": row["prompt_cost_per_1k"],
                "cached_prompt": cached_prompt if cached_prompt is not None else row["prompt_cost_per_1k"] * 0.5,
                "completion": row["completion_cost_per_1k"],
            }

        # Prefix match (e.g. 'gpt-4o' matches 'gpt-4o-2024-11-20')
        rows = conn.execute(
            "SELECT model_pattern, prompt_cost_per_1k, cached_prompt_cost_per_1k, completion_cost_per_1k FROM model_costs ORDER BY LENGTH(model_pattern) DESC"
        ).fetchall()
        for r in rows:
            if model_name.startswith(r["model_pattern"]):
                cached_prompt = r["cached_prompt_cost_per_1k"]
                return {
                    "prompt": r["prompt_cost_per_1k"],
                    "cached_prompt": cached_prompt if cached_prompt is not None else r["prompt_cost_per_1k"] * 0.5,
                    "completion": r["completion_cost_per_1k"],
                }

    return {"prompt": 0.0, "cached_prompt": 0.0, "completion": 0.0}


def upsert_model_cost(
    model_pattern: str,
    prompt_cost_per_1k: float,
    completion_cost_per_1k: float,
    source: str = "manual",
    cached_prompt_cost_per_1k: Optional[float] = None,
) -> None:
    """Insert or update a model cost entry, resetting TTL."""
    from datetime import timedelta
    now = _now()
    expires = (datetime.now(timezone.utc) + timedelta(hours=_COST_TTL_HOURS)).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO model_costs (model_pattern, prompt_cost_per_1k, cached_prompt_cost_per_1k, completion_cost_per_1k, updated_at, expires_at, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_pattern) DO UPDATE SET
                   prompt_cost_per_1k=excluded.prompt_cost_per_1k,
                   cached_prompt_cost_per_1k=excluded.cached_prompt_cost_per_1k,
                   completion_cost_per_1k=excluded.completion_cost_per_1k,
                   updated_at=excluded.updated_at,
                   expires_at=excluded.expires_at,
                   source=excluded.source""",
            (model_pattern, prompt_cost_per_1k, cached_prompt_cost_per_1k, completion_cost_per_1k, now, expires, source),
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


def _issue_context_metadata_key(issue_key: str) -> str:
    issue_key = (issue_key or "").strip()
    if not issue_key:
        raise ValueError("issue_key must be provided")
    return f"issue_context_summary::{issue_key}"


def get_issue_context_summary(repo_path: str, issue_key: str) -> Optional[Dict]:
    """Get the stored restart summary for a repo issue, if present."""
    raw = get_repo_metadata(repo_path, _issue_context_metadata_key(issue_key))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return {"summary": raw}
    if isinstance(data, dict):
        summary = data.get("summary")
        if isinstance(summary, str):
            return data
    if isinstance(data, str):
        return {"summary": data}
    return None


def set_issue_context_summary(
    repo_path: str,
    issue_key: str,
    summary: str,
    *,
    source: str = "",
    model_name: str = "",
    execution_id: Optional[int] = None,
    prompt_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
) -> None:
    """Persist the restart summary for a repo issue."""
    payload = {
        "summary": (summary or "").strip(),
        "source": (source or "").strip(),
        "model_name": (model_name or "").strip(),
        "execution_id": execution_id,
        "prompt_tokens": int(prompt_tokens or 0),
        "cached_prompt_tokens": int(cached_prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int((prompt_tokens or 0) + (completion_tokens or 0)),
        "cost": float(cost or 0.0),
        "updated_at": _now(),
    }
    set_repo_metadata(
        repo_path,
        _issue_context_metadata_key(issue_key),
        json.dumps(payload, ensure_ascii=False),
        source_file=(source or "").strip(),
    )
