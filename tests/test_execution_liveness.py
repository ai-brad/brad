import sqlite3
import tempfile
import os

import pytest

from brad import db
from brad.execution_liveness import ExecutionLivenessTracker


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    db.set_execution_runtime_observer(None)
    yield path
    db.set_execution_runtime_observer(None)
    try:
        os.unlink(path)
    except OSError:
        pass


def test_runtime_observer_claims_execution_and_sets_liveness_fields(temp_db):
    tracker = ExecutionLivenessTracker(
        heartbeat_interval_seconds=3600,
        memory_log_interval_seconds=3600,
        worker_id="worker-test",
        worker_pid=4242,
    )
    db.set_execution_runtime_observer(tracker)

    execution_id = db.create_execution("TEST-LIVE", "Test execution")
    row = db.get_execution(execution_id)

    tracker.close()
    db.set_execution_runtime_observer(None)

    assert row["worker_id"] == "worker-test"
    assert row["worker_pid"] == 4242
    assert row["last_progress_at"] is not None
    assert row["last_heartbeat_at"] is not None


def test_mark_active_failed_updates_execution_on_shutdown(temp_db):
    tracker = ExecutionLivenessTracker(
        heartbeat_interval_seconds=3600,
        memory_log_interval_seconds=3600,
        worker_id="worker-test",
        worker_pid=4242,
    )
    db.set_execution_runtime_observer(tracker)

    execution_id = db.create_execution("TEST-SHUTDOWN", "Shutdown test")
    tracker.mark_active_failed("Worker interrupted by service stop signal")
    row = db.get_execution(execution_id)

    tracker.close()
    db.set_execution_runtime_observer(None)

    assert row["status"] == "failed"
    assert row["error_message"] == "Worker interrupted by service stop signal"
    assert row["finished_at"] is not None


def test_get_running_execution_ignores_stale_rows(temp_db):
    execution_id = db.create_execution("TEST-STALE", "Stale execution")
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            """
            UPDATE executions
               SET last_heartbeat_at = ?,
                   last_progress_at = ?
             WHERE id = ?
            """,
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", execution_id),
        )

    assert db.get_running_execution(stale_after_seconds=300) is None


def test_reconcile_running_executions_marks_only_stale_rows_when_multiworker_mode(temp_db):
    old_id = db.create_execution("TEST-OLD", "Old running execution")
    fresh_id = db.create_execution("TEST-FRESH", "Fresh running execution")

    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            """
            UPDATE executions
               SET last_heartbeat_at = ?,
                   last_progress_at = ?,
                   worker_id = ?
             WHERE id = ?
            """,
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", "worker-a", old_id),
        )
        conn.execute(
            """
            UPDATE executions
               SET worker_id = ?,
                   worker_pid = ?
             WHERE id = ?
            """,
            ("worker-a", 1111, fresh_id),
        )

    reconciled = db.reconcile_running_executions(
        "Worker heartbeat expired",
        stale_after_seconds=300,
        current_worker_id="worker-b",
        assume_single_worker=False,
    )

    old_row = db.get_execution(old_id)
    fresh_row = db.get_execution(fresh_id)

    assert reconciled == 1
    assert old_row["status"] == "failed"
    assert fresh_row["status"] == "running"
