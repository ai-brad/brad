"""Azure OpenAI LLM adapter with tool-calling agent loop."""
import json
import os
import re
import subprocess
import sys
import time
import requests as http_requests
from typing import Dict, List, Optional, Any
from pathlib import Path
from brad.adapters.llm.base import LLMAdapter, LLMResult, LLMUsage
from brad.logging_config import get_logger


class AzureOpenAIAdapter(LLMAdapter):
    """
    Azure OpenAI Responses API implementation of the LLMAdapter interface.
    Provides tools for file I/O, command execution, and code search.
    Runs an agentic loop until the model completes the task.
    """

    TOOL_DEFINITIONS = [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read the contents of a file. Path is relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root"}
                },
                "required": ["path"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "write_file",
            "description": "Create or overwrite a file with the given content. Parent dirs are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root"},
                    "content": {"type": "string", "description": "Full file content"}
                },
                "required": ["path", "content"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "edit_file",
            "description": "Edit a file by replacing exact text. old_text must match exactly (including whitespace).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root"},
                    "old_text": {"type": "string", "description": "Exact text to find"},
                    "new_text": {"type": "string", "description": "Replacement text"}
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "run_command",
            "description": "Run a shell command. Returns stdout, stderr, and exit code. Use for tests, git, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "cwd": {"type": "string", "description": "Working directory relative to repo root (default: repo root)"}
                },
                "required": ["command"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "search_files",
            "description": "Search for a regex pattern in files. Returns matching file:line: content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search (relative to repo root, default '.')"},
                    "include": {"type": "string", "description": "Glob filter e.g. '*.py' (optional)"}
                },
                "required": ["pattern"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "list_directory",
            "description": "List files and subdirectories at a path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to repo root (default '.')"}
                },
                "required": [],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "find_files",
            "description": "Recursively find files matching a glob pattern (e.g. '*.py', '*test*').",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "path": {"type": "string", "description": "Directory to search (default '.')"}
                },
                "required": ["pattern"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "multi_edit_file",
            "description": "Apply multiple edits to one or more files in a single call. Each edit replaces old_text with new_text. Use this instead of multiple edit_file calls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "Array of edit operations",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "File path relative to repo root"},
                                "old_text": {"type": "string", "description": "Exact text to find"},
                                "new_text": {"type": "string", "description": "Replacement text"}
                            },
                            "required": ["path", "old_text", "new_text"]
                        }
                    }
                },
                "required": ["edits"],
                "additionalProperties": False
            }
        },
    ]

    SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.mypy_cache', '.pytest_cache',
                 'dist', 'build', '.tox', '.eggs', '*.egg-info', 'venv', '.venv'}

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.endpoint = cfg.azure_openai_endpoint
        self.api_key = cfg.azure_openai_api_key
        self.model = cfg.azure_openai_model
        self.max_iterations = 200
        self.api_timeout = 180  # seconds per API call
        self.logger.info(f"AzureOpenAIAdapter initialized: model={self.model}")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(
        self,
        task_prompt: str,
        repo_path: str,
        system_prompt: str = "",
        previous_response_id: Optional[str] = None,
    ) -> LLMResult:
        """
        Run an agentic task. Returns LLMResult with text, response_id, and usage.
        """
        self.logger.info(f"=== AzureOpenAI task start in {repo_path} ===")
        self.logger.info(f"Task preview: {task_prompt[:300]}...")

        # Accumulate usage across iterations
        total_usage = LLMUsage()

        # Build initial input
        input_messages: List[Any] = []
        if system_prompt:
            input_messages.append({"role": "developer", "content": system_prompt})
        input_messages.append({"role": "user", "content": task_prompt})

        prev_id = previous_response_id
        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1
            self.logger.info(f"--- Iteration {iteration}/{self.max_iterations} ---")

            api_resp = self._call_api(input_messages, previous_response_id=prev_id)
            if api_resp is None:
                return LLMResult(
                    text="ERROR: Azure OpenAI API call failed — check logs for details.",
                    response_id=prev_id,
                    usage=total_usage,
                )

            # Accumulate usage
            usage_data = api_resp.get("usage", {})
            if usage_data:
                total_usage.prompt_tokens += usage_data.get("input_tokens", 0)
                total_usage.completion_tokens += usage_data.get("output_tokens", 0)
                total_usage.total_tokens = total_usage.prompt_tokens + total_usage.completion_tokens
                # Track cached (prompt-cache-hit) tokens — they cost less
                input_details = usage_data.get("input_tokens_details", {})
                if input_details:
                    total_usage.cached_tokens += input_details.get("cached_tokens", 0)

            prev_id = api_resp.get("id")
            output_items = api_resp.get("output", [])
            status = api_resp.get("status", "unknown")
            self.logger.info(f"Response id={prev_id}, status={status}, items={len(output_items)}")

            # Separate tool calls from text
            tool_calls = []
            text_parts = []
            for item in output_items:
                t = item.get("type")
                if t == "function_call":
                    tool_calls.append(item)
                elif t == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            text_parts.append(c.get("text", ""))
                elif t == "text":
                    text_parts.append(item.get("text", ""))

            # If no tool calls, we're done
            if not tool_calls:
                final = "\n".join(text_parts)
                self.logger.info(f"=== Agent finished after {iteration} iterations ({len(final)} chars) ===")
                self.logger.info(f"Total usage: prompt={total_usage.prompt_tokens}, completion={total_usage.completion_tokens}")
                return LLMResult(text=final, response_id=prev_id, usage=total_usage)

            # Execute every tool call, build the next input
            input_messages = []
            for tc in tool_calls:
                fname = tc.get("name", "")
                call_id = tc.get("call_id", tc.get("id", ""))
                raw_args = tc.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}

                short_args = ", ".join(f"{k}={repr(v)[:60]}" for k, v in args.items())
                self.logger.info(f"  Tool: {fname}({short_args})")

                result = self._execute_tool(fname, args, repo_path)
                result_preview = result[:200].replace("\n", "\\n") if result else "(empty)"
                self.logger.debug(f"  Result preview: {result_preview}")

                input_messages.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(result)[:100_000],
                })

        self.logger.error(f"Agent hit max iterations ({self.max_iterations})")
        return LLMResult(
            text="ERROR: Agent exceeded maximum iteration limit.",
            response_id=prev_id,
            usage=total_usage,
        )

    # ------------------------------------------------------------------
    # API call
    # ------------------------------------------------------------------
    def _call_api(self, input_data, previous_response_id=None) -> Optional[Dict]:
        body: Dict[str, Any] = {
            "model": self.model,
            "input": input_data,
            "tools": self.TOOL_DEFINITIONS,
            "parallel_tool_calls": True,
            "max_output_tokens": 16384,
        }
        if previous_response_id:
            body["previous_response_id"] = previous_response_id

        headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key,
        }

        max_retries = 10
        for attempt in range(max_retries):
            try:
                resp = http_requests.post(
                    self.endpoint, headers=headers, json=body, timeout=self.api_timeout
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", str(min(10 * (2 ** attempt), 120))))
                    self.logger.warning(f"Rate limited, waiting {wait}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    self.logger.error(f"API {resp.status_code}: {resp.text[:500]}")
                    return None
                return resp.json()
            except (http_requests.exceptions.Timeout, http_requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"API {type(e).__name__} (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    wait = min(10 * (2 ** attempt), 120)
                    self.logger.warning(f"Retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                self.logger.error(f"API call failed after {max_retries} attempts: {e}")
                return None
            except Exception as e:
                self.logger.error(f"API call error: {e}")
                return None
        self.logger.error(f"API call failed after {max_retries} attempts")
        return None

    # ------------------------------------------------------------------
    # Tool dispatcher
    # ------------------------------------------------------------------
    def _execute_tool(self, name: str, args: Dict, repo_path: str) -> str:
        try:
            if name == "read_file":
                return self._tool_read_file(repo_path, args.get("path", ""))
            elif name == "write_file":
                return self._tool_write_file(repo_path, args.get("path", ""), args.get("content", ""))
            elif name == "edit_file":
                return self._tool_edit_file(repo_path, args.get("path", ""),
                                            args.get("old_text", ""), args.get("new_text", ""))
            elif name == "run_command":
                return self._tool_run_command(repo_path, args.get("command", ""), args.get("cwd", ""))
            elif name == "search_files":
                return self._tool_search_files(repo_path, args.get("pattern", ""),
                                               args.get("path", "."), args.get("include", ""))
            elif name == "list_directory":
                return self._tool_list_directory(repo_path, args.get("path", "."))
            elif name == "find_files":
                return self._tool_find_files(repo_path, args.get("pattern", ""), args.get("path", "."))
            elif name == "multi_edit_file":
                return self._tool_multi_edit_file(repo_path, args.get("edits", []))
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            self.logger.error(f"Tool {name} error: {e}", exc_info=True)
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------
    def _tool_read_file(self, repo_path: str, path: str) -> str:
        fp = Path(repo_path) / path
        if not fp.exists():
            return f"Error: File not found: {path}"
        if not fp.is_file():
            return f"Error: Not a file: {path}"
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            if len(text) > 120_000:
                text = text[:120_000] + "\n... [truncated — file too large]"
            return text
        except Exception as e:
            return f"Error reading {path}: {e}"

    def _tool_write_file(self, repo_path: str, path: str, content: str) -> str:
        fp = Path(repo_path) / path
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return f"OK: wrote {len(content)} chars to {path}"
        except Exception as e:
            return f"Error writing {path}: {e}"

    def _tool_edit_file(self, repo_path: str, path: str, old_text: str, new_text: str) -> str:
        fp = Path(repo_path) / path
        if not fp.exists():
            return f"Error: File not found: {path}"
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
            if old_text not in content:
                return (f"Error: old_text not found in {path}. "
                        f"File has {len(content)} chars. Make sure whitespace matches exactly.")
            count = content.count(old_text)
            content = content.replace(old_text, new_text, 1)
            fp.write_text(content, encoding="utf-8")
            note = f" (WARNING: found {count} occurrences, replaced first only)" if count > 1 else ""
            return f"OK: edited {path}{note}"
        except Exception as e:
            return f"Error editing {path}: {e}"

    def _tool_run_command(self, repo_path: str, command: str, cwd: str = "") -> str:
        work_dir = str(Path(repo_path) / cwd) if cwd else repo_path

        # Translate Unix-style env var prefixes (VAR=val cmd) to Windows (set VAR=val && cmd)
        if sys.platform == "win32":
            command = self._translate_unix_env_prefix(command)

        self.logger.info(f"  Running: {command} (cwd={work_dir})")
        try:
            result = subprocess.run(
                command, shell=True,
                capture_output=True, text=True,
                timeout=1800, cwd=work_dir,
                encoding="utf-8", errors="replace",
            )
            out = ""
            if result.stdout:
                out += result.stdout
            if result.stderr:
                out += "\nSTDERR:\n" + result.stderr
            out += f"\n[exit code: {result.returncode}]"
            if len(out) > 80_000:
                out = out[:40_000] + "\n...[truncated]...\n" + out[-40_000:]
            return out
        except subprocess.TimeoutExpired:
            return "Error: command timed out (1800s limit)"
        except Exception as e:
            return f"Error: {e}"

    @staticmethod
    def _translate_unix_env_prefix(command: str) -> str:
        """Translate 'VAR=val VAR2=val2 cmd args...' to 'cmd /c \"set VAR=val && set VAR2=val2 && cmd args...\"'."""
        env_pattern = re.compile(r'^([A-Z_][A-Z0-9_]*)=(\S+)\s+')
        env_vars = []
        remaining = command
        while True:
            m = env_pattern.match(remaining)
            if not m:
                break
            env_vars.append(f"set {m.group(1)}={m.group(2)}")
            remaining = remaining[m.end():]
        if env_vars:
            sets = " && ".join(env_vars)
            return f'cmd /c "{sets} && {remaining}"'
        return command

    def _tool_search_files(self, repo_path: str, pattern: str,
                           path: str = ".", include: str = "") -> str:
        search_dir = Path(repo_path) / path
        if not search_dir.exists():
            return f"Error: directory not found: {path}"
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Invalid regex: {e}"

        glob_pat = include if include else "*"
        results: List[str] = []
        for fp in search_dir.rglob(glob_pat):
            if not fp.is_file():
                continue
            if any(skip in fp.parts for skip in self.SKIP_DIRS):
                continue
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        rel = fp.relative_to(repo_path)
                        results.append(f"{rel}:{i}: {line.rstrip()}")
                        if len(results) >= 300:
                            results.append("... [truncated — too many matches]")
                            return "\n".join(results)
            except Exception:
                continue
        return "\n".join(results) if results else "No matches found."

    def _tool_list_directory(self, repo_path: str, path: str = ".") -> str:
        dp = Path(repo_path) / path
        if not dp.exists():
            return f"Error: not found: {path}"
        try:
            entries = sorted(dp.iterdir())
            lines: List[str] = []
            for e in entries[:300]:
                if e.name.startswith('.') and e.name in ('.git',):
                    continue
                tag = "DIR " if e.is_dir() else "FILE"
                size = f" ({e.stat().st_size}B)" if e.is_file() else ""
                lines.append(f"[{tag}] {e.name}{size}")
            return "\n".join(lines) if lines else "(empty)"
        except Exception as e:
            return f"Error: {e}"

    def _tool_multi_edit_file(self, repo_path: str, edits: List[Dict]) -> str:
        if not edits:
            return "Error: no edits provided"
        results = []
        for i, edit in enumerate(edits):
            path = edit.get("path", "")
            old_text = edit.get("old_text", "")
            new_text = edit.get("new_text", "")
            if not path or not old_text:
                results.append(f"Edit {i+1}: Error: missing path or old_text")
                continue
            result = self._tool_edit_file(repo_path, path, old_text, new_text)
            results.append(f"Edit {i+1} ({path}): {result}")
        return "\n".join(results)

    def _tool_find_files(self, repo_path: str, pattern: str, path: str = ".") -> str:
        dp = Path(repo_path) / path
        if not dp.exists():
            return f"Error: not found: {path}"
        try:
            matches = []
            for m in dp.rglob(pattern):
                if any(skip in m.parts for skip in self.SKIP_DIRS):
                    continue
                matches.append(str(m.relative_to(repo_path)))
                if len(matches) >= 200:
                    matches.append("... [truncated]")
                    break
            return "\n".join(matches) if matches else "No files found."
        except Exception as e:
            return f"Error: {e}"
