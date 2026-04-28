"""Tests for the harness/provider registries and back-compat shims."""

import importlib
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


class TestCodexCliHarnessInvocation:
    def test_invokes_codex_with_expected_argv(self, tmp_path):
        harness = CodexCliHarness(_cfg(harness="codex"))
        completed = SimpleNamespace(
            returncode=0, stdout="hello\nBRAD_STATUS: READY\n", stderr=""
        )

        # Pre-create the last-message file so the harness reads from it.
        with (
            patch("brad.adapters.harness.codex_cli_harness.subprocess.run") as run,
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):

            def _fake_run(argv, **kwargs):
                # The --output-last-message path is the value right after the flag.
                idx = argv.index("--output-last-message")
                out_path = argv[idx + 1]
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("final message\nBRAD_STATUS: READY\n")
                return completed

            run.side_effect = _fake_run

            result = harness.run(
                task_prompt="do the thing",
                repo_path=str(tmp_path),
                system_prompt="ctx",
            )

        assert "final message" in result.text
        assert "BRAD_STATUS: READY" in result.text
        run.assert_called_once()
        argv = run.call_args.args[0]
        assert argv[0] == "codex"
        assert "exec" in argv
        assert "--cd" in argv
        assert str(tmp_path) in argv
        # Default approval mode is full-auto, which translates to a single flag.
        assert "--full-auto" in argv
        # The deprecated/non-existent flag must NOT be passed.
        assert "--ask-for-approval" not in argv

    def test_danger_approval_mode_uses_bypass_flag(self, tmp_path):
        harness = CodexCliHarness(_cfg(harness="codex", codex_approval="danger"))
        with (
            patch("brad.adapters.harness.codex_cli_harness.subprocess.run") as run,
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            harness.run("x", str(tmp_path))
        argv = run.call_args.args[0]
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert "--full-auto" not in argv

    def test_sandbox_only_approval_mode_passes_sandbox_flag(self, tmp_path):
        harness = CodexCliHarness(_cfg(harness="codex", codex_approval="sandbox-only"))
        with (
            patch("brad.adapters.harness.codex_cli_harness.subprocess.run") as run,
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            harness.run("x", str(tmp_path))
        argv = run.call_args.args[0]
        assert "--sandbox" in argv
        assert "workspace-write" in argv
        assert "--full-auto" not in argv

    def test_omits_model_flag_when_codex_model_unset(self, tmp_path):
        """When CODEX_MODEL is not set, codex should fall back to ~/.codex/config.toml."""
        harness = CodexCliHarness(_cfg(harness="codex", codex_model=None))
        with (
            patch("brad.adapters.harness.codex_cli_harness.subprocess.run") as run,
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            harness.run("x", str(tmp_path))
        argv = run.call_args.args[0]
        assert "--model" not in argv

    def test_passes_model_flag_when_codex_model_set(self, tmp_path):
        harness = CodexCliHarness(_cfg(harness="codex", codex_model="gpt-5.4"))
        with (
            patch("brad.adapters.harness.codex_cli_harness.subprocess.run") as run,
            patch(
                "brad.adapters.harness.codex_cli_harness.shutil.which",
                return_value="/usr/bin/codex",
            ),
        ):
            run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            harness.run("x", str(tmp_path))
        argv = run.call_args.args[0]
        assert "--model" in argv
        assert "gpt-5.4" in argv

    def test_missing_binary_returns_error(self, tmp_path):
        harness = CodexCliHarness(
            _cfg(harness="codex", codex_bin="definitely-not-codex")
        )
        with patch(
            "brad.adapters.harness.codex_cli_harness.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            result = harness.run(
                task_prompt="x", repo_path=str(tmp_path), system_prompt=""
            )
        assert result.text.startswith("ERROR: codex binary not found")
