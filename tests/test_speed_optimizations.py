"""Tests for speed optimizations: batch review, multi_edit tool, pre-search, fail-fast branch."""
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from brad.adapters.llm.azure_openai_adapter import AzureOpenAIAdapter
from brad.agents.interface import AIAgentInterface
from brad.adapters.llm.base import LLMAdapter, LLMResult, LLMUsage
from test_helpers import make_test_config


# -------------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------------

@pytest.fixture
def temp_git_repo(tmp_path):
    """Create a minimal git-like repo for tests."""
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture
def adapter(temp_git_repo):
    cfg = make_test_config(temp_git_repo)
    return AzureOpenAIAdapter(cfg)


@pytest.fixture
def mock_llm():
    return Mock(spec=LLMAdapter)


@pytest.fixture
def agent(mock_llm, temp_git_repo):
    cfg = make_test_config(temp_git_repo)
    return AIAgentInterface(mock_llm, cfg)


# =========================================================================
# multi_edit_file tool
# =========================================================================

class TestMultiEditFile:
    def test_applies_multiple_edits(self, adapter, temp_git_repo):
        f1 = temp_git_repo / "a.py"
        f2 = temp_git_repo / "b.py"
        f1.write_text("old_a = 1\n", encoding="utf-8")
        f2.write_text("old_b = 2\n", encoding="utf-8")

        edits = [
            {"path": "a.py", "old_text": "old_a = 1", "new_text": "new_a = 1"},
            {"path": "b.py", "old_text": "old_b = 2", "new_text": "new_b = 2"},
        ]
        result = adapter._tool_multi_edit_file(str(temp_git_repo), edits)

        assert "OK" in result
        assert f1.read_text(encoding="utf-8").strip() == "new_a = 1"
        assert f2.read_text(encoding="utf-8").strip() == "new_b = 2"

    def test_reports_missing_old_text(self, adapter, temp_git_repo):
        f = temp_git_repo / "c.py"
        f.write_text("hello\n", encoding="utf-8")

        edits = [{"path": "c.py", "old_text": "nonexistent", "new_text": "x"}]
        result = adapter._tool_multi_edit_file(str(temp_git_repo), edits)
        assert "Error" in result

    def test_empty_edits(self, adapter, temp_git_repo):
        result = adapter._tool_multi_edit_file(str(temp_git_repo), [])
        assert "Error" in result

    def test_missing_path_field(self, adapter, temp_git_repo):
        edits = [{"old_text": "x", "new_text": "y"}]
        result = adapter._tool_multi_edit_file(str(temp_git_repo), edits)
        assert "Error" in result

    def test_tool_is_in_definitions(self):
        names = [t["name"] for t in AzureOpenAIAdapter.TOOL_DEFINITIONS]
        assert "multi_edit_file" in names

    def test_dispatcher_routes_multi_edit(self, adapter, temp_git_repo):
        f = temp_git_repo / "d.py"
        f.write_text("old = 1\n", encoding="utf-8")
        result = adapter._execute_tool(
            "multi_edit_file",
            {"edits": [{"path": "d.py", "old_text": "old = 1", "new_text": "new = 1"}]},
            str(temp_git_repo),
        )
        assert "OK" in result


# =========================================================================
# Batch review comment prompt & parsing
# =========================================================================

class TestBatchReviewComments:
    def _make_comment(self, cid, body="Fix this", path="src/foo.py", line=10):
        return {
            "id": cid,
            "body": body,
            "path": path,
            "line": line,
            "user": {"login": "reviewer"},
            "diff_hunk": "@@ -1,5 +1,5 @@\n-old\n+new",
        }

    def test_batch_prompt_contains_all_comment_ids(self, agent):
        comments = [self._make_comment(111), self._make_comment(222)]
        prompt = agent._build_code_review_reader_batch_prompt(42, "feature-branch", comments)
        assert "id=111" in prompt
        assert "id=222" in prompt
        assert "2 code review comments" in prompt

    def test_batch_response_parser_extracts_results(self, agent):
        comments = [self._make_comment(111), self._make_comment(222)]
        output = (
            "I analyzed both comments.\n"
            "COMMENT 111: REPLIED: The code is correct because X.\n"
            "COMMENT 222: CODE_CHANGED: Fixed the null check.\n"
        )
        result = agent._parse_code_review_reader_batch_response(output, comments)
        cr = result["comment_results"]
        assert len(cr) == 2
        assert cr[0]["comment_id"] == 111
        assert cr[0]["action"] == "replied"
        assert cr[1]["comment_id"] == 222
        assert cr[1]["action"] == "code_changed"

    def test_batch_response_parser_fallback(self, agent):
        comments = [self._make_comment(999)]
        output = "I did some stuff but forgot the format."
        result = agent._parse_code_review_reader_batch_response(output, comments)
        cr = result["comment_results"]
        assert len(cr) == 1
        assert cr[0]["action"] == "replied"  # fallback

    def test_invoke_batch_calls_llm_once(self, agent, mock_llm, temp_git_repo):
        comments = [self._make_comment(1), self._make_comment(2), self._make_comment(3)]
        mock_llm.run.return_value = LLMResult(
            text="COMMENT 1: REPLIED: ok\nCOMMENT 2: REPLIED: ok\nCOMMENT 3: CODE_CHANGED: fixed",
            response_id="resp_123",
            usage=LLMUsage(prompt_tokens=100, completion_tokens=50),
        )
        with patch("brad.agents.interface.get_codebase_map", return_value="map"):
            result = agent.invoke_code_review_reader_batch(
                pr_number=10,
                branch_name="DEV-100",
                comments=comments,
                repo_path=str(temp_git_repo),
            )
        # LLM was called exactly ONCE for 3 comments
        assert mock_llm.run.call_count == 1
        assert len(result["comment_results"]) == 3


# =========================================================================
# Pre-search codebase
# =========================================================================

class TestPreSearchCodebase:
    def test_extracts_camel_case_terms(self, agent, temp_git_repo):
        desc = 'Use ColorTagDefinition to set MessageTagService colors'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="")
            agent._pre_search_codebase(desc, str(temp_git_repo))
            # Should have searched for CamelCase terms
            calls = [str(c) for c in mock_run.call_args_list]
            combined = " ".join(calls)
            assert "ColorTagDefinition" in combined or "MessageTagService" in combined

    def test_extracts_upper_case_constants(self, agent, temp_git_repo):
        desc = 'Replace RED_COLOR_TAG and ORANGE_COLOR_TAG constants'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="")
            agent._pre_search_codebase(desc, str(temp_git_repo))
            calls = [str(c) for c in mock_run.call_args_list]
            combined = " ".join(calls)
            assert "RED_COLOR_TAG" in combined or "ORANGE_COLOR_TAG" in combined

    def test_returns_empty_when_no_terms(self, agent, temp_git_repo):
        result = agent._pre_search_codebase("do stuff", str(temp_git_repo))
        assert result == ""

    def test_formats_results_with_files(self, agent, temp_git_repo):
        desc = 'Fix the ColorTagDefinition'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="src/services/color_tags.py\nsrc/models/tags.py\n",
            )
            result = agent._pre_search_codebase(desc, str(temp_git_repo))
            assert "color_tags.py" in result


# =========================================================================
# Fail-fast on missing branch (orchestrator level)
# =========================================================================

class TestFailFastMissingBranch:
    def test_skips_all_comments_when_branch_missing(self, temp_git_repo):
        from brad.orchestrator import BradOrchestrator
        from brad import db

        cfg = make_test_config(temp_git_repo)
        orch = BradOrchestrator(cfg)
        orch.ticketing = Mock()
        orch.code_repo = Mock()
        orch.ci = Mock()
        orch.observability = Mock()
        orch.agent = Mock()
        orch.repo = Mock()
        orch.repo.repo_path = temp_git_repo

        # Branch checkout raises -> should skip
        orch.repo.checkout_branch.side_effect = RuntimeError("Branch 'X' does not exist")
        orch.code_repo.get_brad_prs.return_value = [
            {"number": 100, "head": {"ref": "X"}}
        ]
        orch.code_repo.get_review_comments_needing_response.return_value = [
            {"id": 1, "body": "fix", "path": "a.py", "user": {"login": "rev"}},
            {"id": 2, "body": "fix2", "path": "b.py", "user": {"login": "rev"}},
        ]

        orch._process_review_comments()

        # Agent should NOT have been called at all
        orch.agent.invoke_code_review_reader_batch.assert_not_called()
        orch.agent.invoke_code_review_reader.assert_not_called()
        # No "Brad checking" replies either
        orch.code_repo.reply_to_review_comment.assert_not_called()
