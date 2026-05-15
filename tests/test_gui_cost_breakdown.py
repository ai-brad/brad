import os
import tempfile

import pytest

from brad import db
from brad.gui.app import build_execution_cost_breakdown, create_app


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
