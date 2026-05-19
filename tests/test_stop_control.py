import os
import signal
import tempfile
from unittest.mock import Mock

import pytest

from brad import db
from brad.adapters.ci_cd.github_actions_adapter import GitHubActionsAdapter
from brad.gui.app import create_app
from brad.orchestrator import BradOrchestrator
from test_helpers import make_test_config


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_control_state_round_trip(temp_db):
    state = db.get_control_state()
    assert state["state"] == "running"

    stopped = db.set_control_state("stopped", reason="operator pause", updated_by="ui")
    assert stopped["state"] == "stopped"
    assert stopped["reason"] == "operator pause"
    assert db.is_brad_stopped()

    running = db.set_control_state("running", updated_by="ui")
    assert running["state"] == "running"
    assert not db.is_brad_stopped()


def test_stop_and_resume_routes_update_state_and_signal_worker(temp_db, monkeypatch):
    execution_id = db.create_execution("TEST-STOP", "Stop feature", cost_budget=150.0)
    db.touch_execution_liveness(
        execution_id,
        worker_id="worker-1",
        worker_pid=4321,
        progress=True,
    )

    killed = []
    monkeypatch.setattr("brad.gui.app.os.kill", lambda pid, sig: killed.append((pid, sig)))

    app = create_app(temp_db)
    client = app.test_client()

    stop_resp = client.post("/api/control/stop", data={"reason": "operator stop"})
    assert stop_resp.status_code in (302, 303)
    assert killed == [(4321, signal.SIGTERM)]

    stopped = db.get_control_state()
    assert stopped["state"] == "stopped"
    assert stopped["reason"] == "operator stop"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"Brad is stopped" in dashboard.data

    resume_resp = client.post("/api/control/resume")
    assert resume_resp.status_code in (302, 303)
    assert db.get_control_state()["state"] == "running"


def test_orchestrator_skips_work_when_stopped(temp_git_repo, monkeypatch):
    cfg = make_test_config(temp_git_repo)
    orch = BradOrchestrator(cfg)
    orch.ticketing = Mock()
    orch.code_repo = Mock()
    orch.ci = Mock()
    orch.observability = Mock()
    orch.agent = Mock()
    monkeypatch.setattr(db, "is_brad_stopped", lambda: True)

    orch.run_once()

    orch.code_repo.get_brad_prs.assert_not_called()
    orch.ticketing.fetch_issues_with_label.assert_not_called()


def test_ci_wait_aborts_when_stopped(temp_git_repo, monkeypatch):
    cfg = make_test_config(temp_git_repo)
    adapter = GitHubActionsAdapter(cfg)
    monkeypatch.setattr(db, "is_brad_stopped", lambda: True)

    with pytest.raises(KeyboardInterrupt):
        adapter.wait_for_pr(123, poll_interval=1, timeout=1)
