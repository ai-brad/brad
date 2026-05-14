"""
Integration tests for Brad - focus on real behavior not mocking.
Tests actual DB operations, phase transitions, cost tracking, etc.
"""
import tempfile
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from brad import db
from brad.orchestrator import BradOrchestrator, IssueState
from test_helpers import make_test_config


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    db.init_db(path)
    yield path
    try:
        os.unlink(path)
    except:
        pass


class TestDatabaseIntegration:
    """Test real DB operations."""
    
    def test_execution_lifecycle_tracking(self, temp_db):
        """Test complete execution lifecycle is tracked in DB."""
        # Create execution
        exec_id = db.create_execution("TEST-1", "Test issue")
        assert exec_id > 0
        
        # Add steps
        step1 = db.create_step(exec_id, "requirements", "Analyzing")
        db.finish_step(step1, status="completed", result_summary="Done")
        
        step2 = db.create_step(exec_id, "implementation", "Coding")
        db.finish_step(step2, status="completed", result_summary="Code written")
        
        # Update costs
        db.update_execution_costs(exec_id, prompt_tokens=1000, completion_tokens=500, cost=0.50)
        db.update_execution_costs(exec_id, prompt_tokens=2000, completion_tokens=800, cost=0.80)
        
        # Update PR
        db.update_execution_pr(exec_id, pr_number=123, pr_url="https://github.com/test/repo/pull/123")
        
        # Update phase
        db.update_execution_phase(exec_id, "ci_monitoring")
        
        # Finish execution
        db.finish_execution(exec_id, status="completed")
        
        # Verify everything persisted
        execution = db.get_execution(exec_id)
        assert execution["issue_key"] == "TEST-1"
        # Note: pr_number is set via finish_execution, not update_execution_pr in this flow
        assert execution["total_cost"] == 1.30
        assert execution["total_prompt_tokens"] == 3000
        assert execution["total_completion_tokens"] == 1300
        assert execution["current_phase"] == "ci_monitoring"
        assert execution["status"] == "completed"
        
        # Verify steps
        steps = db.get_execution_steps(exec_id)
        assert len(steps) == 2
        assert steps[0]["phase"] == "requirements"
        assert steps[1]["phase"] == "implementation"
    
    def test_model_cost_caching(self, temp_db):
        """Test model costs are cached and retrieved."""
        
        # Upsert costs
        db.upsert_model_cost("gpt-4", prompt_cost_per_1k=0.03, completion_cost_per_1k=0.06)
        
        # Retrieve
        cost = db.get_model_cost("gpt-4")
        assert cost["prompt"] == 0.03
        assert cost["completion"] == 0.06
        
        # Update
        db.upsert_model_cost("gpt-4", prompt_cost_per_1k=0.04, completion_cost_per_1k=0.08)
        
        # Verify updated
        cost = db.get_model_cost("gpt-4")
        assert cost["prompt"] == 0.04

    def test_reconcile_running_executions_marks_stale_rows_failed(self, temp_db):
        """Startup cleanup should fail stale running executions and steps."""
        exec_id = db.create_execution("TEST-STALE", "Stale issue")
        db.update_execution_phase(exec_id, "implementing", "Writing code and tests")
        step_id = db.create_step(exec_id, "implementation", "Running LLM implementation agent")

        reconciled = db.reconcile_running_executions("Worker restarted during execution")

        assert reconciled == 1

        execution = db.get_execution(exec_id)
        assert execution["status"] == "failed"
        assert execution["finished_at"] is not None
        assert execution["error_message"] == "Worker restarted during execution"
        assert execution["current_phase"] == "implementing"
        assert execution["current_phase_detail"] == "Writing code and tests"

        steps = db.get_execution_steps(exec_id)
        assert len(steps) == 1
        assert steps[0]["id"] == step_id
        assert steps[0]["status"] == "failed"
        assert steps[0]["finished_at"] is not None
        assert steps[0]["result_summary"] == "Worker restarted during execution"

    def test_reconcile_running_executions_clears_phantom_dashboard_state(self, temp_db):
        """A worker restart should recover stale runs and clear dashboard state."""
        completed_id = db.create_execution("TEST-DONE", "Already completed")
        db.update_execution_phase(completed_id, "done", "Finished successfully")
        db.finish_execution(completed_id, status="completed")

        stale_old_id = db.create_execution("TEST-OLD", "Older stale run")
        db.update_execution_phase(stale_old_id, "ci_fix", "Fixing CI failures")
        db.create_step(stale_old_id, "ci_fix", "Re-running failed CI jobs", iteration=1)

        stale_latest_id = db.create_execution("TEST-LATEST", "Newest stale run")
        db.update_execution_phase(stale_latest_id, "implementing", "Writing code and tests")
        latest_step_id = db.create_step(
            stale_latest_id,
            "implementation",
            "Running LLM implementation agent",
        )

        assert db.get_running_execution()["id"] == stale_latest_id

        reconciled = db.reconcile_running_executions("Worker restarted during execution")

        assert reconciled == 2
        assert db.get_running_execution() is None

        latest = db.get_execution(stale_latest_id)
        assert latest["status"] == "failed"
        assert latest["error_message"] == "Worker restarted during execution"

        old = db.get_execution(stale_old_id)
        assert old["status"] == "failed"
        assert old["error_message"] == "Worker restarted during execution"

        completed = db.get_execution(completed_id)
        assert completed["status"] == "completed"
        assert completed["error_message"] is None

        latest_steps = db.get_execution_steps(stale_latest_id)
        assert latest_steps[0]["id"] == latest_step_id
        assert latest_steps[0]["status"] == "failed"
        assert latest_steps[0]["result_summary"] == "Worker restarted during execution"

    def test_reconcile_running_executions_marks_legacy_unowned_rows_immediately(self, temp_db):
        """Single-worker startup should fail legacy running rows even without heartbeat fields."""
        exec_id = db.create_execution("TEST-LEGACY", "Legacy stale run")
        db.update_execution_phase(exec_id, "implementing", "Writing code and tests")

        # Simulate old schema/runtime that never wrote worker ownership or heartbeat fields.
        import sqlite3
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                """
                UPDATE executions
                   SET worker_id = NULL,
                       worker_pid = NULL,
                       last_heartbeat_at = NULL
                 WHERE id = ?
                """,
                (exec_id,),
            )

        reconciled = db.reconcile_running_executions(
            "Worker restarted during execution",
            stale_after_seconds=3600,
            current_worker_id="new-worker",
            assume_single_worker=True,
        )

        execution = db.get_execution(exec_id)
        assert reconciled == 1
        assert execution["status"] == "failed"
        assert execution["error_message"] == "Worker restarted during execution"
    
    def test_repo_metadata_caching(self, temp_db):
        """Test repository metadata caching."""
        
        # Set metadata
        db.set_repo_metadata("/test/repo", "dev_instructions", "Run pytest tests/", source_file="CONTRIBUTING.md")
        
        # Get metadata
        cached = db.get_repo_metadata("/test/repo", "dev_instructions")
        assert cached is not None
        assert "Run pytest tests/" in cached
        
        # Update metadata
        db.set_repo_metadata("/test/repo", "dev_instructions", "Run pytest -v", source_file="README.md")
        cached = db.get_repo_metadata("/test/repo", "dev_instructions")
        assert "Run pytest -v" in cached


class TestOrchestratorPhaseManagement:
    """Test orchestrator phase transitions and state management."""
    
    def test_phase_transitions_tracked(self, temp_db, temp_git_repo):
        """Test that phase updates are tracked in DB."""
        cfg = make_test_config(temp_git_repo)
        
        with patch("brad.orchestrator.build_harness") as mock_build:
            mock_harness = Mock()
            mock_harness.run.return_value = SimpleNamespace(text="", response_id="1", usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, cached_tokens=0))
            mock_build.return_value = mock_harness
            orchestrator = BradOrchestrator(cfg)
        
        exec_id = db.create_execution("TEST-2", "Test issue")
        
        state = IssueState(
            issue_key="TEST-2",
            description="Test",
            attachments=[],
            attachment_paths=[],
            branch_name="TEST-2",
            execution_id=exec_id,
        )
        
        # Set different phases
        orchestrator._set_phase(state, "requirements_analysis", "Reading ticket")
        execution = db.get_execution(exec_id)
        assert execution["current_phase"] == "requirements_analysis"
        assert execution["current_phase_detail"] == "Reading ticket"
        
        orchestrator._set_phase(state, "implementing", "Writing code")
        execution = db.get_execution(exec_id)
        assert execution["current_phase"] == "implementing"
        assert execution["current_phase_detail"] == "Writing code"
        
        orchestrator._set_phase(state, "ci_monitoring", "Waiting for CI")
        execution = db.get_execution(exec_id)
        assert execution["current_phase"] == "ci_monitoring"
    
    def test_cost_calculation(self, temp_db):
        """Test that costs are calculated correctly."""
        exec_id = db.create_execution("TEST-3", "Test issue")
        
        # Add costs
        db.update_execution_costs(exec_id, prompt_tokens=1000, completion_tokens=500, cost=0.50)
        db.update_execution_costs(exec_id, prompt_tokens=2000, completion_tokens=800, cost=0.80)
        
        # Verify accumulated
        execution = db.get_execution(exec_id)
        assert execution["total_cost"] == 1.30
        assert execution["total_prompt_tokens"] == 3000
        assert execution["total_completion_tokens"] == 1300


class TestRealWorkflows:
    """Test real workflow patterns without extensive mocking."""
    
    def test_issue_state_initialization(self):
        """Test IssueState properly tracks state."""
        state = IssueState(
            issue_key="TEST-4",
            description="Test issue",
            attachments=[],
            attachment_paths=[],
            branch_name="TEST-4",
            execution_id=1,
        )
        
        assert state.issue_key == "TEST-4"
        assert state.clarification_count == 0
        assert state.ci_fix_count == 0
        assert state.review_fix_count == 0
        assert state.local_review_fix_count == 0
        assert state.cost_budget == 10.0
    
    def test_iteration_limits_respected(self, temp_db, temp_git_repo):
        """Test that iteration limits prevent runaway loops."""
        cfg = make_test_config(temp_git_repo)
        cfg.max_review_fix_iterations = 3
        
        with patch("brad.orchestrator.build_harness") as mock_build:
            mock_harness = Mock()
            mock_harness.run.return_value = SimpleNamespace(text="", response_id="1", usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, cached_tokens=0))
            mock_build.return_value = mock_harness
            orchestrator = BradOrchestrator(cfg)
        
        # Mock services
        orchestrator.ticketing = Mock()
        orchestrator.agent = Mock()
        
        exec_id = db.create_execution("TEST-5", "Test")
        
        state = IssueState(
            issue_key="TEST-5",
            description="Test",
            attachments=[],
            attachment_paths=[],
            branch_name="TEST-5",
            execution_id=exec_id,
            local_review_fix_count=3,  # At limit
        )
        
        # Attempt another fix
        result = orchestrator._handle_local_review_fix(state, "More feedback")
        
        # Should refuse
        assert result is False
        orchestrator.agent.invoke_local_review_fix.assert_not_called()
        orchestrator.ticketing.comment.assert_called_once()
