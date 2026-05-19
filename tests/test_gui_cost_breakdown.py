import os
import tempfile

import pytest

from brad import db
from brad.gui.app import (
    build_execution_cost_breakdown,
    build_ticket_cost_breakdown,
    build_execution_failure_context,
    decorate_execution,
    format_execution_action,
    format_execution_status_label,
    create_app,
)


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


def test_build_execution_cost_breakdown_aggregates_cached_and_non_cached_tokens():
    execution = {"total_cost": 3.5}
    steps = [
        {
            "phase": "implementation",
            "prompt_tokens": 1000,
            "cached_prompt_tokens": 200,
            "completion_tokens": 100,
            "cost": 2.0,
        },
        {
            "phase": "implementation",
            "prompt_tokens": 500,
            "cached_prompt_tokens": 100,
            "completion_tokens": 50,
            "cost": 1.0,
        },
        {
            "phase": "local_review",
            "prompt_tokens": 300,
            "cached_prompt_tokens": 0,
            "completion_tokens": 20,
            "cost": 0.5,
        },
    ]

    rows = build_execution_cost_breakdown(execution, steps)

    assert rows == [
        {
            "phase": "implementation",
            "steps": 2,
            "non_cached_prompt_tokens": 1200,
            "cached_prompt_tokens": 300,
            "completion_tokens": 150,
            "total_input_tokens": 1500,
            "total_tokens": 1650,
            "cost": 3.0,
            "cost_pct": pytest.approx(85.7142857),
        },
        {
            "phase": "local_review",
            "steps": 1,
            "non_cached_prompt_tokens": 300,
            "cached_prompt_tokens": 0,
            "completion_tokens": 20,
            "total_input_tokens": 300,
            "total_tokens": 320,
            "cost": 0.5,
            "cost_pct": pytest.approx(14.2857142),
        },
    ]


def test_api_execution_detail_includes_cost_breakdown(temp_db):
    execution_id = db.create_execution("TEST-123", "GUI breakdown test", cost_budget=150.0)
    step_id = db.create_step(execution_id, "implementation", "Test step")
    db.finish_step(
        step_id,
        status="success",
        prompt_tokens=1000,
        cached_prompt_tokens=250,
        completion_tokens=40,
        cost=1.23,
        result_summary="ok",
    )
    db.update_execution_costs(
        execution_id,
        prompt_tokens=1000,
        cached_prompt_tokens=250,
        completion_tokens=40,
        cost=1.23,
    )

    app = create_app(temp_db)
    client = app.test_client()

    resp = client.get(f"/api/execution/{execution_id}")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["execution"]["id"] == execution_id
    assert payload["cost_breakdown"] == [
        {
            "phase": "implementation",
            "steps": 1,
            "non_cached_prompt_tokens": 750,
            "cached_prompt_tokens": 250,
            "completion_tokens": 40,
            "total_input_tokens": 1000,
            "total_tokens": 1040,
            "cost": 1.23,
            "cost_pct": 100.0,
        }
    ]
    assert payload["ticket_cost_breakdown"] == payload["cost_breakdown"]


def test_execution_detail_and_history_show_model_name(temp_db):
    execution_id = db.create_execution(
        "TEST-MODEL",
        "Model display test",
        cost_budget=150.0,
        model_name="gpt-5.4-mini",
    )
    db.finish_execution(execution_id, status="completed")

    app = create_app(temp_db)
    client = app.test_client()

    detail_resp = client.get(f"/execution/{execution_id}")
    assert detail_resp.status_code == 200
    detail_html = detail_resp.get_data(as_text=True)
    assert "Model:" in detail_html
    assert "gpt-5.4-mini" in detail_html

    history_resp = client.get("/history")
    assert history_resp.status_code == 200
    history_html = history_resp.get_data(as_text=True)
    assert "<th>Model</th>" in history_html
    assert "gpt-5.4-mini" in history_html


def test_build_ticket_cost_breakdown_aggregates_across_executions(temp_db):
    implementation_id = db.create_execution("TEST-999", "Add feature", cost_budget=150.0)
    impl_step = db.create_step(implementation_id, "implementation", "Implementing")
    db.finish_step(
        impl_step,
        status="completed",
        prompt_tokens=1000,
        cached_prompt_tokens=100,
        completion_tokens=50,
        cost=2.0,
        result_summary="done",
    )
    db.update_execution_costs(
        implementation_id,
        prompt_tokens=1000,
        cached_prompt_tokens=100,
        completion_tokens=50,
        cost=2.0,
    )
    db.finish_execution(implementation_id, status="completed", pr_number=321, pr_url="https://github.com/org/repo/pull/321")

    ci_id = db.create_execution("TEST-999", "Review-driven CI watch on PR #321", cost_budget=150.0)
    ci_step = db.create_step(ci_id, "ci_monitoring", "Monitoring CI")
    db.finish_step(
        ci_step,
        status="completed",
        prompt_tokens=0,
        cached_prompt_tokens=0,
        completion_tokens=0,
        cost=0.0,
        result_summary="CI passed",
    )
    db.finish_execution(ci_id, status="completed", pr_number=321, pr_url="https://github.com/org/repo/pull/321")

    rows = build_ticket_cost_breakdown("TEST-999")

    assert rows == [
        {
            "phase": "implementation",
            "steps": 1,
            "non_cached_prompt_tokens": 900,
            "cached_prompt_tokens": 100,
            "completion_tokens": 50,
            "total_input_tokens": 1000,
            "total_tokens": 1050,
            "cost": 2.0,
            "cost_pct": pytest.approx(100.0),
        },
        {
            "phase": "ci_monitoring",
            "steps": 1,
            "non_cached_prompt_tokens": 0,
            "cached_prompt_tokens": 0,
            "completion_tokens": 0,
            "total_input_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "cost_pct": pytest.approx(0.0),
        },
    ]


def test_db_ticket_cost_breakdown_aggregates_via_join(temp_db):
    exec1 = db.create_execution("TEST-JOIN", "First", cost_budget=150.0)
    s1 = db.create_step(exec1, "implementation", "Implementing")
    db.finish_step(s1, status="completed", prompt_tokens=200, cached_prompt_tokens=20, completion_tokens=10, cost=0.6)
    db.update_execution_costs(exec1, prompt_tokens=200, cached_prompt_tokens=20, completion_tokens=10, cost=0.6)
    db.finish_execution(exec1, status="completed")

    exec2 = db.create_execution("TEST-JOIN", "Second", cost_budget=150.0)
    s2 = db.create_step(exec2, "ci_monitoring", "Monitoring CI")
    db.finish_step(s2, status="completed", prompt_tokens=0, cached_prompt_tokens=0, completion_tokens=0, cost=0.0)
    db.finish_execution(exec2, status="completed")

    rows = db.get_ticket_cost_breakdown("TEST-JOIN")
    assert [row["phase"] for row in rows] == ["implementation", "ci_monitoring"]
    assert rows[0]["non_cached_prompt_tokens"] == 180
    assert rows[0]["cached_prompt_tokens"] == 20


def test_execution_list_helpers_prefer_issue_title_and_short_action_labels():
    execution = {
        "issue_key": "DEV-3763",
        "summary": "Review-driven CI watch on PR #2442",
        "current_phase": "ci_fix",
        "status": "completed",
        "issue_title": "Previo - late check-in reservation notes",
    }

    decorated = decorate_execution(execution)

    assert decorated["issue_title"] == "Previo - late check-in reservation notes"
    assert decorated["action"] == "CI Fix"
    assert decorated["status_label"] == "DONE"

    assert format_execution_action({
        "issue_key": "DEV-1",
        "summary": "Rebase conflict resolution for PR #2110",
        "current_phase": "conflict_resolution",
    }) == "Rebase"
    assert format_execution_status_label("completed") == "DONE"
    assert format_execution_status_label("failed") == "FAILED"


def test_api_executions_includes_display_fields(temp_db):
    execution_id = db.create_execution(
        "TEST-456",
        "Review-driven CI watch on PR #77",
        issue_title="Ticket title from Jira",
        action="CI Fix",
    )

    app = create_app(temp_db)
    client = app.test_client()

    resp = client.get("/api/executions")
    assert resp.status_code == 200
    payload = resp.get_json()

    row = next(ex for ex in payload["executions"] if ex["id"] == execution_id)
    assert row["issue_title"] == "Ticket title from Jira"
    assert row["action"] == "CI Fix"
    assert row["status_label"] == "RUNNING"


def test_api_execution_detail_includes_ticket_cost_breakdown(temp_db):
    implementation_id = db.create_execution("TEST-ABC", "Implement feature", cost_budget=150.0)
    impl_step = db.create_step(implementation_id, "implementation", "Implementing")
    db.finish_step(
        impl_step,
        status="completed",
        prompt_tokens=500,
        cached_prompt_tokens=0,
        completion_tokens=25,
        cost=1.5,
        result_summary="done",
    )
    db.update_execution_costs(
        implementation_id,
        prompt_tokens=500,
        cached_prompt_tokens=0,
        completion_tokens=25,
        cost=1.5,
    )
    db.finish_execution(implementation_id, status="completed", pr_number=999, pr_url="https://github.com/org/repo/pull/999")

    ci_id = db.create_execution("TEST-ABC", "Review-driven CI watch on PR #999", cost_budget=150.0)
    ci_step = db.create_step(ci_id, "ci_monitoring", "Monitoring CI")
    db.finish_step(ci_step, status="completed", result_summary="CI passed")
    db.finish_execution(ci_id, status="completed", pr_number=999, pr_url="https://github.com/org/repo/pull/999")

    app = create_app(temp_db)
    client = app.test_client()
    resp = client.get(f"/api/execution/{ci_id}")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert [row["phase"] for row in payload["ticket_cost_breakdown"]] == ["implementation", "ci_monitoring"]


def test_build_execution_failure_context_surfaces_last_step_details():
    execution = {
        "status": "stuck",
        "error_message": "Brad could not create a PR",
        "current_phase": "stuck",
        "current_phase_detail": "",
    }
    steps = [
        {
            "phase": "implementation",
            "status": "success",
            "detail": "Running LLM implementation agent",
            "result_summary": "DONE: Created PR #2442: https://github.com/flaerobotics/bea/pull/2442\nBRAD_STATUS: READY",
        }
    ]

    ctx = build_execution_failure_context(execution, steps)

    assert ctx["primary_message"] == "Brad could not create a PR"
    assert ctx["last_step_phase"] == "implementation"
    assert ctx["related_pr_number"] == 2442
    assert ctx["related_pr_url"] == "https://github.com/flaerobotics/bea/pull/2442"
    assert "reported that it created a PR" in ctx["inferred_reason"]
