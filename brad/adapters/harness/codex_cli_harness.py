"""Codex CLI harness — delegates the agentic loop to OpenAI's ``codex`` binary.

This harness shells out to the ``codex exec`` CLI for each task.  Codex brings
its own tools (file edit, shell, search) and its own model auth (typically
``codex login`` or ``OPENAI_API_KEY``), so the inner :class:`LLMProvider` layer
is *not* used here.

Assumes a recent ``codex`` CLI is on ``PATH`` (>= the version that ships
``--output-last-message``; falls back to stdout if the flag is unsupported).

Warm-start (``previous_response_id``) is intentionally a no-op for v1: each
task is a fresh session.  The ``codex resume`` mechanism can be wired later if
useful — see TODO at the end of the file.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
import tomllib

from brad.adapters.harness.base import AgentHarness, LLMResult, LLMUsage
from brad.logging_config import get_logger


# Sentinel suffix appended to the user prompt so existing parsers in
# ``brad/agents/interface.py`` (which key off "fixed"/"stuck"/"ready") still
# work against Codex's free-form output.  Codex is asked to end its final
# message with a recognisable status line.
_STATUS_SENTINEL_INSTRUCTIONS = """

---
When you are completely finished with this task, end your final assistant
message with EXACTLY one of the following lines (no extra prose after it):

  BRAD_STATUS: FIXED
  BRAD_STATUS: STUCK
  BRAD_STATUS: READY
  BRAD_STATUS: IN_PROGRESS

Use FIXED for completed CI fixes / review fixes / local-review fixes,
READY for completed implementations or requirements analyses,
STUCK if you cannot proceed, and IN_PROGRESS only if you are pausing mid-task.
"""


class CodexCliHarness(AgentHarness):
    """Shells out to ``codex exec`` for each agentic task."""

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.cfg = cfg
        self.bin = getattr(cfg, "codex_bin", None) or "codex"
        # ``codex_model`` is optional. When None/empty we omit ``--model`` so
        # codex falls back to ~/.codex/config.toml (model + provider + auth).
        self.model = getattr(cfg, "codex_model", None) or None
        self._resolved_model_name = self._resolve_model_name()
        self.sandbox = getattr(cfg, "codex_sandbox", None) or "workspace-write"
        # ``codex_approval`` controls how aggressively Codex auto-approves shell
        # commands.  For non-interactive ``codex exec`` runs the relevant knobs
        # are ``--full-auto`` (sandboxed, no prompts) and
        # ``--dangerously-bypass-approvals-and-sandbox`` (no sandbox, no prompts).
        # The legacy ``--ask-for-approval`` flag does NOT exist on ``codex exec``.
        #
        # Default is ``danger`` because brad's implementation prompt instructs
        # the agent to ``git push`` and ``gh pr create`` itself — both require
        # network egress and ``.git`` writes that ``--full-auto`` blocks.
        self.approval = (getattr(cfg, "codex_approval", None) or "danger").lower()
        self.timeout = int(getattr(cfg, "codex_timeout", 0) or 3600)

        if shutil.which(self.bin) is None:
            self.logger.warning(
                f"CodexCliHarness configured but '{self.bin}' not found on PATH. "
                f"Install Codex CLI (https://github.com/openai/codex) before running."
            )
        self.logger.info(
            f"CodexCliHarness initialized: bin={self.bin} "
            f"model={self.model or self._resolved_model_name or '<from ~/.codex/config.toml>'} "
            f"sandbox={self.sandbox} approval={self.approval}"
        )

    @property
    def model_name(self) -> str:
        """Return the model name used by Codex for cost calculation."""
        return self._resolved_model_name or self.model or "gpt-5-codex"

    def run(
        self,
        task_prompt: str,
        repo_path: str,
        system_prompt: str = "",
        previous_response_id: Optional[str] = None,
    ) -> LLMResult:
        if previous_response_id:
            # Documented limitation; see TODO at the bottom of the file.
            self.logger.debug(
                "CodexCliHarness ignores previous_response_id (warm-start not yet wired)."
            )

        full_prompt = self._build_prompt(system_prompt, task_prompt)

        with tempfile.TemporaryDirectory(prefix="brad-codex-") as tmpdir:
            last_msg_path = Path(tmpdir) / "last_message.txt"
            argv = [
                self.bin, "exec",
                "--cd", repo_path,
                "--skip-git-repo-check",
                "--output-last-message", str(last_msg_path),
                "--json",  # stream JSONL events on stdout for live visibility
            ]
            if self.model:
                argv.extend(["--model", self.model])
            argv += self._approval_flags()
            argv.append("-")  # read prompt from stdin

            self.logger.info(f"=== CodexCliHarness invoking: {' '.join(argv[:-1])} (stdin) ===")
            self.logger.info(f"Task preview: {task_prompt[:300]}...")

            session_id = uuid.uuid4().hex  # placeholder so callers can correlate logs
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=repo_path,
                    env=os.environ.copy(),
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,  # line-buffered
                )
            except FileNotFoundError:
                msg = (
                    f"ERROR: codex binary not found ({self.bin!r}). "
                    f"Install Codex CLI or set CODEX_BIN."
                )
                self.logger.error(msg)
                return LLMResult(text=msg, response_id=None, usage=LLMUsage())

            # Drain stderr in a background thread so codex doesn't block on a
            # full pipe; we keep the tail for error reporting.
            stderr_buf: List[str] = []

            def _drain_stderr():
                assert proc.stderr is not None
                for line in proc.stderr:
                    stderr_buf.append(line)

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            # Send the prompt then close stdin so codex starts processing.
            try:
                assert proc.stdin is not None
                proc.stdin.write(full_prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass

            usage = LLMUsage()
            try:
                self._stream_events(proc, usage)
                proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                msg = f"ERROR: codex exec timed out after {self.timeout}s"
                self.logger.error(msg)
                return LLMResult(text=msg, response_id=session_id, usage=usage)
            finally:
                stderr_thread.join(timeout=2)

            text = self._extract_final_text(last_msg_path)

            if proc.returncode != 0 and not text.strip():
                stderr_tail = "".join(stderr_buf).strip()[-2000:]
                text = (
                    f"ERROR: codex exec exited with code {proc.returncode}.\n"
                    f"STDERR (tail):\n{stderr_tail}"
                )
                self.logger.error(text)

            self.logger.info(
                f"=== CodexCliHarness finished (exit={proc.returncode}, "
                f"final={len(text)} chars, "
                f"tokens in={usage.prompt_tokens}/cached={usage.cached_tokens}/"
                f"out={usage.completion_tokens}) ==="
            )
            return LLMResult(text=text, response_id=session_id, usage=usage)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _approval_flags(self) -> list:
        """Translate the high-level ``codex_approval`` knob to ``codex exec`` flags.

        ``codex exec`` is non-interactive and does NOT accept the legacy
        ``--ask-for-approval`` flag.  The supported modes are:

        * ``full-auto`` (default) — sandboxed, no prompts (``--full-auto``).
        * ``danger`` / ``dangerously-bypass`` — no sandbox, no prompts
          (``--dangerously-bypass-approvals-and-sandbox``).
        * ``sandbox-only`` — pass only ``--sandbox <mode>`` and let Codex use
          its config defaults.
        """
        approval = self.approval
        if approval in ("danger", "dangerously-bypass", "dangerously_bypass",
                        "dangerously-bypass-approvals-and-sandbox", "bypass"):
            return ["--dangerously-bypass-approvals-and-sandbox"]
        if approval in ("sandbox-only", "sandbox_only", "sandbox"):
            return ["--sandbox", self.sandbox]
        if approval in ("full-auto", "full_auto", "auto"):
            return ["--full-auto"]
        # Default: danger (no sandbox, full network) — see __init__ for rationale.
        if approval not in ("danger", "never", ""):
            self.logger.warning(
                f"Unknown codex_approval={approval!r}; falling back to "
                f"--dangerously-bypass-approvals-and-sandbox"
            )
        return ["--dangerously-bypass-approvals-and-sandbox"]

    @staticmethod
    def _read_codex_config_model(config_path: Path) -> Optional[str]:
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        model = data.get("model")
        return model.strip() if isinstance(model, str) and model.strip() else None

    def _resolve_model_name(self) -> Optional[str]:
        if self.model:
            return self.model
        env_model = os.environ.get("CODEX_MODEL")
        if env_model and env_model.strip():
            return env_model.strip()
        config_model = self._read_codex_config_model(Path.home() / ".codex" / "config.toml")
        if config_model:
            return config_model
        return None

    @staticmethod
    def _build_prompt(system_prompt: str, task_prompt: str) -> str:
        """Concatenate developer/system context, the task, and the status sentinel.

        Codex CLI's ``exec`` mode accepts a single user prompt.  We prepend the
        codebase-map / system prompt so the agent has the same context the
        Brad harness would have provided as a developer message.
        """
        parts = []
        if system_prompt:
            parts.append("# Repository context\n\n" + system_prompt.strip())
        parts.append("# Task\n\n" + task_prompt.strip())
        parts.append(_STATUS_SENTINEL_INSTRUCTIONS.strip())
        return "\n\n".join(parts) + "\n"

    @staticmethod
    def _extract_final_text(last_msg_path: Path) -> str:
        """Read the final assistant message from ``--output-last-message``.

        With ``--json`` mode active, codex still writes the final message to
        the path passed via ``--output-last-message``.  Stdout in that mode is
        the JSONL event stream and is consumed by :meth:`_stream_events`.
        """
        try:
            if last_msg_path.exists():
                content = last_msg_path.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    return content
        except Exception:
            pass
        return ""

    def _stream_events(self, proc: subprocess.Popen, usage: LLMUsage) -> None:
        """Consume codex's JSONL stdout, mirroring key events to brad's logger.

        Codex emits one JSON object per line under ``--json``.  Two schemas
        are supported:

        * **Current** (codex-cli >= 0.120ish): top-level ``type`` is one of
          ``thread.started`` / ``turn.started`` / ``turn.completed`` /
          ``item.started`` / ``item.completed`` / ``error``.  Item events
          carry an ``item`` object whose own ``type`` is ``agent_message``,
          ``agent_reasoning``, ``command_execution``, ``file_change``, ...
          ``turn.completed`` carries a ``usage`` block.
        * **Legacy**: ``payload.type`` is ``agent_message`` /
          ``exec_command_begin`` / ``patch_apply_begin`` / ``token_count`` /
          ``error``.

        Any unparseable lines are ignored silently — codex may emit non-JSON
        diagnostics on stdout in some failure modes.
        """
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = evt.get("type") or ""

            # ---- Current schema: item.* / turn.* / thread.* / error ------
            if etype.startswith("item."):
                item: Dict[str, Any] = evt.get("item") or {}
                itype = item.get("type") or ""
                # Only log agent_message on completion to avoid duplicates;
                # log command/file actions on start so progress is visible
                # while they run.
                if itype == "agent_message" and etype == "item.completed":
                    text = (item.get("text") or "").strip()
                    if text:
                        self.logger.info(f"[codex] {text[:500]}")
                elif itype == "agent_reasoning" and etype == "item.completed":
                    text = (item.get("text") or "").strip()
                    if text:
                        self.logger.info(f"[codex:reasoning] {text[:300]}")
                elif itype == "command_execution" and etype == "item.started":
                    cmd = item.get("command") or ""
                    if isinstance(cmd, list):
                        cmd = " ".join(str(c) for c in cmd)
                    self.logger.info(f"[codex:$] {str(cmd)[:300]}")
                elif itype == "command_execution" and etype == "item.completed":
                    exit_code = item.get("exit_code")
                    if exit_code not in (0, None):
                        tail = (item.get("aggregated_output") or "").strip()[-300:]
                        self.logger.info(
                            f"[codex:$ exit={exit_code}] {tail}"
                        )
                elif itype == "file_change" and etype == "item.started":
                    changes = item.get("changes") or item.get("files") or []
                    if isinstance(changes, list):
                        names = [
                            c.get("path") if isinstance(c, dict) else str(c)
                            for c in changes
                        ]
                    elif isinstance(changes, dict):
                        names = list(changes.keys())
                    else:
                        names = [str(changes)]
                    names = [n for n in names if n]
                    if names:
                        self.logger.info(
                            f"[codex:patch] {', '.join(names[:5])[:300]}"
                        )
                continue

            if etype == "turn.completed":
                u = evt.get("usage") or {}
                if u:
                    input_tokens = u.get("input_tokens")
                    cached_input_tokens = u.get("cached_input_tokens")
                    if input_tokens is not None or cached_input_tokens is not None:
                        input_tokens = input_tokens or 0
                        cached_input_tokens = cached_input_tokens or 0
                        usage.cached_tokens = cached_input_tokens
                        usage.prompt_tokens = input_tokens + cached_input_tokens
                    usage.completion_tokens = (
                        u.get("output_tokens") or usage.completion_tokens
                    )
                    usage.total_tokens = (
                        (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
                    )
                continue

            if etype == "thread.started":
                tid = evt.get("thread_id")
                if tid:
                    self.logger.debug(f"[codex] thread {tid}")
                continue

            if etype == "error":
                err = evt.get("message") or json.dumps(evt)[:300]
                self.logger.error(f"[codex:error] {err}")
                continue

            # ---- Legacy schema fallback ----------------------------------
            payload: Dict[str, Any] = evt.get("payload") or {}
            ptype = payload.get("type") or ""

            if ptype == "agent_message":
                msg = (payload.get("message") or "").strip()
                if msg:
                    self.logger.info(f"[codex] {msg[:500]}")
            elif ptype == "agent_reasoning":
                text = (payload.get("text") or "").strip()
                if text:
                    self.logger.info(f"[codex:reasoning] {text[:300]}")
            elif ptype == "exec_command_begin":
                cmd = payload.get("command") or []
                if isinstance(cmd, list):
                    cmd_str = " ".join(str(c) for c in cmd)
                else:
                    cmd_str = str(cmd)
                self.logger.info(f"[codex:$] {cmd_str[:300]}")
            elif ptype == "patch_apply_begin":
                changes = payload.get("changes") or {}
                if isinstance(changes, dict) and changes:
                    self.logger.info(f"[codex:patch] {', '.join(list(changes.keys())[:5])[:300]}")
            elif ptype == "token_count":
                info = payload.get("info") or {}
                total = info.get("total_token_usage") or {}
                if total:
                    input_tokens = total.get("input_tokens")
                    cached_input_tokens = total.get("cached_input_tokens")
                    if input_tokens is not None or cached_input_tokens is not None:
                        input_tokens = input_tokens or 0
                        cached_input_tokens = cached_input_tokens or 0
                        usage.cached_tokens = cached_input_tokens
                        usage.prompt_tokens = input_tokens + cached_input_tokens
                    usage.completion_tokens = total.get("output_tokens") or usage.completion_tokens
                    usage.total_tokens = (
                        (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
                    )
            elif ptype == "error":
                err = payload.get("message") or json.dumps(payload)[:300]
                self.logger.error(f"[codex:error] {err}")


# TODO(harness/warm-start): wire `codex resume <session_id>` once we capture
# session ids reliably (likely via `--json` event stream).  When done,
# `previous_response_id` should round-trip through the resume flow so multi-turn
# phases (implementation -> review fix -> ci fix on the same ticket) reuse
# context the way BradHarness does today.
