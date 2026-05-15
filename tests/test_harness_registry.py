"""Tests for the harness/provider registries and back-compat shims."""

import importlib
import io
import warnings
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from brad.adapters.harness import build_harness
from brad.adapters.harness.base import AgentHarness
from brad.adapters.harness.brad_harness import BradHarness
from brad.adapters.harness.codex_cli_harness import CodexCliHarness
from brad.adapters.llm import build_provider
from brad.adapters.llm.azure_openai_provider import AzureOpenAIProvider


def _cfg(**overrides):
    """Minimal duck-typed config object."""
    base = SimpleNamespace(
        harness="brad",
        llm_provider="azure_openai",
        azure_openai_endpoint="https://example.test/responses",
        azure_openai_api_key="dummy",
        azure_openai_model="gpt-4o",
        codex_bin="codex",
        codex_model="gpt-5-codex",
        codex_sandbox="workspace-write",
        codex_approval="never",
        codex_timeout=60,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestProviderRegistry:
    def test_default_is_azure_openai(self):
        provider = build_provider(_cfg())
        assert isinstance(provider, AzureOpenAIProvider)

    def test_aliases(self):
        for alias in ("azure_openai", "AZURE_OPENAI", "azure-openai", "azure"):
            provider = build_provider(_cfg(llm_provider=alias))
            assert isinstance(provider, AzureOpenAIProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown BRAD_LLM_PROVIDER"):
            build_provider(_cfg(llm_provider="not_a_provider"))


class TestHarnessRegistry:
    def test_default_is_brad(self):
        harness = build_harness(_cfg())
        assert isinstance(harness, BradHarness)
        assert isinstance(harness, AgentHarness)

    def test_codex_alias(self):
        for alias in ("codex", "codex_cli", "codex-cli", "CODEX"):
            harness = build_harness(_cfg(harness=alias))
            assert isinstance(harness, CodexCliHarness)

    def test_unknown_harness_raises(self):
        with pytest.raises(ValueError, match="Unknown BRAD_HARNESS"):
            build_harness(_cfg(harness="not_a_harness"))

    def test_brad_harness_uses_configured_provider(self):
        harness = build_harness(_cfg())
        assert isinstance(harness, BradHarness)
        # Provider was built and stored.
        assert isinstance(harness.provider, AzureOpenAIProvider)


class TestBackCompatShim:
    def test_llm_adapter_alias_emits_deprecation(self):
        # Re-import the module to get a clean __getattr__ invocation.
        mod = importlib.import_module("brad.adapters.llm.base")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            LegacyAdapter = mod.LLMAdapter  # noqa: N806 - intentional legacy name
        assert LegacyAdapter is AgentHarness
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)


class _FakePopen:
    """Minimal Popen stand-in for CodexCliHarness tests.

    Provides line-iterable stdout/stderr and a stdin sink. ``stdout_lines`` is
    the JSONL stream codex would emit under ``--json``. If ``last_message`` is
    set, the constructor writes it to the path in argv right after
    ``--output-last-message`` so :meth:`_extract_final_text` can pick it up.
    """

    def __init__(
        self, argv, stdout_lines=(), returncode=0, last_message=None, **_kwargs
    ):
        self.argv = argv
        self.returncode = returncode
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(stdout_lines))
        self.stderr = io.StringIO("")
        if last_message is not None and "--output-last-message" in argv:
            idx = argv.index("--output-last-message")
            with open(argv[idx + 1], "w", encoding="utf-8") as f:
                f.write(last_message)

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _patch_popen(
    stdout_lines=(), last_message="final message\nBRAD_STATUS: READY\n", returncode=0
):
    """Returns a (popen_patch, which_patch) context-manager pair recipe."""
    captured = {}

    def _factory(argv, **kwargs):
        proc = _FakePopen(
            argv,
            stdout_lines=stdout_lines,
            returncode=returncode,
            last_message=last_message,
        )
        captured["argv"] = argv
        captured["proc"] = proc
        return proc

    return _factory, captured


class TestCodexCliHarnessInvocation:
    def test_invokes_codex_with_expected_argv(self, tmp_path):
        harness = CodexCliHarness(_cfg(harness="codex"))
        factory, captured = _patch_popen()
        with (
            patch(
                "brad.adapters.harness.codex_cli_harness.subprocess.Popen",
                side_effect=factory,
            ),
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            result = harness.run(
                task_prompt="do the thing",
                repo_path=str(tmp_path),
                system_prompt="ctx",
            )

        assert "final message" in result.text
        assert "BRAD_STATUS: READY" in result.text
        argv = captured["argv"]
        assert argv[0] == "codex"
        assert "exec" in argv
        assert "--cd" in argv
        assert str(tmp_path) in argv
        # Default approval mode is 'danger' so the agent can git push / gh pr create.
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        # The deprecated/non-existent flag must NOT be passed.
        assert "--ask-for-approval" not in argv
        # JSONL streaming is always on.
        assert "--json" in argv

    def test_danger_approval_mode_uses_bypass_flag(self, tmp_path):
        harness = CodexCliHarness(_cfg(harness="codex", codex_approval="danger"))
        factory, captured = _patch_popen()
        with (
            patch(
                "brad.adapters.harness.codex_cli_harness.subprocess.Popen",
                side_effect=factory,
            ),
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            harness.run("x", str(tmp_path))
        argv = captured["argv"]
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert "--full-auto" not in argv

    def test_sandbox_only_approval_mode_passes_sandbox_flag(self, tmp_path):
        harness = CodexCliHarness(_cfg(harness="codex", codex_approval="sandbox-only"))
        factory, captured = _patch_popen()
        with (
            patch(
                "brad.adapters.harness.codex_cli_harness.subprocess.Popen",
                side_effect=factory,
            ),
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            harness.run("x", str(tmp_path))
        argv = captured["argv"]
        assert "--sandbox" in argv
        assert "workspace-write" in argv
        assert "--full-auto" not in argv

    def test_omits_model_flag_when_codex_model_unset(self, tmp_path):
        """When CODEX_MODEL is not set, codex should fall back to ~/.codex/config.toml."""
        harness = CodexCliHarness(_cfg(harness="codex", codex_model=None))
        factory, captured = _patch_popen()
        with (
            patch(
                "brad.adapters.harness.codex_cli_harness.subprocess.Popen",
                side_effect=factory,
            ),
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            harness.run("x", str(tmp_path))
        argv = captured["argv"]
        assert "--model" not in argv

    def test_model_name_reads_codex_config_when_model_flag_unset(self, tmp_path):
        cfg = _cfg(harness="codex", codex_model=None)
        with patch.object(
            CodexCliHarness,
            "_read_codex_config_model",
            return_value="gpt-5.4",
        ):
            harness = CodexCliHarness(cfg)
        assert harness.model_name == "gpt-5.4"

    def test_passes_model_flag_when_codex_model_set(self, tmp_path):
        harness = CodexCliHarness(_cfg(harness="codex", codex_model="gpt-5.4"))
        factory, captured = _patch_popen()
        with (
            patch(
                "brad.adapters.harness.codex_cli_harness.subprocess.Popen",
                side_effect=factory,
            ),
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            harness.run("x", str(tmp_path))
        argv = captured["argv"]
        assert "--model" in argv
        assert "gpt-5.4" in argv

    def test_streams_token_count_into_usage(self, tmp_path):
        """Token-count events from codex JSONL accumulate into LLMUsage."""
        harness = CodexCliHarness(_cfg(harness="codex"))
        events = [
            '{"type":"event_msg","payload":{"type":"agent_message","message":"hello"}}\n',
            (
                '{"type":"event_msg","payload":{"type":"token_count","info":'
                '{"total_token_usage":{"input_tokens":1000,"cached_input_tokens":600,"output_tokens":50}}}}\n'
            ),
        ]
        factory, _ = _patch_popen(stdout_lines=events)
        with (
            patch(
                "brad.adapters.harness.codex_cli_harness.subprocess.Popen",
                side_effect=factory,
            ),
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            result = harness.run("x", str(tmp_path))
        assert result.usage.prompt_tokens == 1600
        assert result.usage.cached_tokens == 600
        assert result.usage.completion_tokens == 50
        assert result.usage.total_tokens == 1650

    def test_missing_binary_returns_error(self, tmp_path):
        harness = CodexCliHarness(
            _cfg(harness="codex", codex_bin="definitely-not-codex")
        )
        with patch(
            "brad.adapters.harness.codex_cli_harness.subprocess.Popen",
            side_effect=FileNotFoundError(),
        ):
            result = harness.run(
                task_prompt="x", repo_path=str(tmp_path), system_prompt=""
            )
        assert result.text.startswith("ERROR: codex binary not found")
