"""AI-driven commit sanitizer.

Runs before push to remove throwaway artifacts Brad may have committed
(PRDs, plans/summaries, debug scripts, playwright logs, build output, etc.).

Flow:
  1. Diff branch vs origin/main, collect files Brad ADDED.
  2. Filter to "suspicious" candidates using cheap heuristics.
  3. Ask the LLM to classify each candidate keep/remove (single call).
  4. git rm the ones flagged "remove" and append a cleanup commit.

Fails open: on any error, keeps everything.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from brad.logging_config import get_logger


# Path / name patterns that make an added file a *candidate* for review.
# These are NOT auto-delete rules — the LLM still decides per-file.
_SUSPICIOUS_NAME_PATTERNS = [
    "*.md",
    "*.log",
    "*.tmp",
    "*.bak",
    "*.swp",
    "*.orig",
    "log.html",
    "trace.zip",
    "*.ps1",
    "*.bat",
]

_SUSPICIOUS_PATH_PREFIXES = [
    "playwright-report/",
    "test-results/",
    "coverage/",
    "dist/",
    "build/",
    "tmp/",
    ".tmp/",
    ".brad/",
    ".brad_cache/",
    "node_modules/",
]

_SUSPICIOUS_NAME_SUBSTRINGS_CI = [
    "prd",
    "plan",
    "summary",
    "notes",
    "progress",
    "scratch",
    "debug",
    "repro",
    "acceptance_criteria",
    "implementation_plan",
    "fix_summary",
    "changes_summary",
]

_ROOT_SCRIPT_EXTENSIONS = {".py", ".sh", ".ps1", ".bat", ".js", ".ts"}
_ROOT_SCRIPT_NAME_PREFIXES = ("debug_", "repro_", "tmp_", "scratch_", "check_", "run_once", "fix_")


@dataclass
class _Candidate:
    path: str
    size: int
    content: str  # head+tail excerpt

    def to_prompt_section(self) -> str:
        return f"--- FILE: {self.path} ({self.size} bytes) ---\n{self.content}\n--- END {self.path} ---"


class CommitSanitizer:
    """Inspects a branch for throwaway files Brad shouldn't have committed and removes them."""

    # Max bytes of file content included in the LLM prompt (head+tail combined)
    MAX_EXCERPT_CHARS = 4000
    HEAD_LINES = 60
    TAIL_LINES = 20
    # Cap number of candidates fed to the LLM in one call
    MAX_CANDIDATES = 20

    def __init__(self, repo_manager, llm_adapter, base_branch: str = "main"):
        self.logger = get_logger(__name__)
        self.repo = repo_manager
        self.llm = llm_adapter
        self.base_branch = base_branch
        self._prompt_template: Optional[str] = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self, issue_key: str, issue_description: str, branch_name: str) -> dict:
        """Sanitize the current branch. Returns a summary dict."""
        summary = {"candidates": 0, "removed": [], "kept": [], "error": None}
        try:
            added = self._added_files()
            if not added:
                self.logger.info(f"[sanitizer] {issue_key}: no added files on {branch_name}, skipping")
                return summary

            candidates = self._pick_candidates(added)
            summary["candidates"] = len(candidates)
            if not candidates:
                self.logger.info(
                    f"[sanitizer] {issue_key}: {len(added)} added files, none suspicious"
                )
                return summary

            self.logger.info(
                f"[sanitizer] {issue_key}: {len(candidates)} suspicious candidates "
                f"out of {len(added)} added files"
            )

            decisions = self._classify(issue_key, issue_description, branch_name, candidates)
            if decisions is None:
                self.logger.warning(f"[sanitizer] {issue_key}: classification failed, keeping all")
                summary["error"] = "classification_failed"
                return summary

            to_remove = [d for d in decisions if d.get("action") == "remove" and d.get("path")]
            to_keep = [d for d in decisions if d.get("action") != "remove"]
            summary["kept"] = [d["path"] for d in to_keep if d.get("path")]

            for d in to_remove:
                self.logger.info(
                    f"[sanitizer] {issue_key}: REMOVE {d['path']} — {d.get('reason', '')}"
                )
            for d in to_keep:
                if d.get("path"):
                    self.logger.info(
                        f"[sanitizer] {issue_key}: KEEP   {d['path']} — {d.get('reason', '')}"
                    )

            if to_remove:
                removed = self._apply_removals([d["path"] for d in to_remove], issue_key)
                summary["removed"] = removed
            return summary
        except Exception as e:
            self.logger.warning(f"[sanitizer] {issue_key}: error (keeping all): {e}")
            summary["error"] = str(e)
            return summary

    # ------------------------------------------------------------------
    # Step 1: enumerate added files on the branch
    # ------------------------------------------------------------------
    def _added_files(self) -> List[str]:
        # Make sure we know origin/base
        self.repo._run_git("fetch", "origin", self.base_branch, check=False)
        result = self.repo._run_git(
            "diff", "--name-status", f"origin/{self.base_branch}...HEAD", check=False
        )
        if result.returncode != 0:
            return []
        added: List[str] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0].strip()
            # A = added. Also treat copied (C) as added for our purposes.
            if status.startswith("A") or status.startswith("C"):
                added.append(parts[-1])
        return added

    # ------------------------------------------------------------------
    # Step 2: narrow to "suspicious" candidates
    # ------------------------------------------------------------------
    def _pick_candidates(self, added_paths: List[str]) -> List[_Candidate]:
        candidates: List[_Candidate] = []
        for path in added_paths:
            if not self._is_suspicious(path):
                continue
            c = self._build_candidate(path)
            if c is not None:
                candidates.append(c)
            if len(candidates) >= self.MAX_CANDIDATES:
                break
        return candidates

    @staticmethod
    def _is_suspicious(path: str) -> bool:
        p = path.replace("\\", "/")
        lower = p.lower()
        name = lower.rsplit("/", 1)[-1]
        # Path prefix match
        for prefix in _SUSPICIOUS_PATH_PREFIXES:
            if lower.startswith(prefix) or f"/{prefix}" in f"/{lower}":
                return True
        # Glob match on basename
        for pat in _SUSPICIOUS_NAME_PATTERNS:
            if fnmatch.fnmatch(name, pat):
                return True
        # Substring match on basename (case-insensitive)
        for sub in _SUSPICIOUS_NAME_SUBSTRINGS_CI:
            if sub in name:
                return True
        # Root-level ad-hoc scripts: single-segment path with script extension & tell-tale prefix
        if "/" not in p:
            ext = Path(name).suffix
            if ext in _ROOT_SCRIPT_EXTENSIONS and name.startswith(_ROOT_SCRIPT_NAME_PREFIXES):
                return True
        return False

    def _build_candidate(self, path: str) -> Optional[_Candidate]:
        fp = Path(self.repo.repo_path) / path
        try:
            if not fp.exists() or not fp.is_file():
                return None
            size = fp.stat().st_size
            text = fp.read_text(encoding="utf-8", errors="replace")
            excerpt = self._excerpt(text)
            return _Candidate(path=path, size=size, content=excerpt)
        except Exception as e:
            self.logger.debug(f"[sanitizer] could not read {path}: {e}")
            return None

    def _excerpt(self, text: str) -> str:
        if len(text) <= self.MAX_EXCERPT_CHARS:
            return text
        lines = text.splitlines()
        if len(lines) <= self.HEAD_LINES + self.TAIL_LINES:
            return text[: self.MAX_EXCERPT_CHARS] + "\n... [truncated]"
        head = "\n".join(lines[: self.HEAD_LINES])
        tail = "\n".join(lines[-self.TAIL_LINES :])
        combined = f"{head}\n... [middle of file truncated, {len(lines)} lines total] ...\n{tail}"
        if len(combined) > self.MAX_EXCERPT_CHARS:
            combined = combined[: self.MAX_EXCERPT_CHARS] + "\n... [truncated]"
        return combined

    # ------------------------------------------------------------------
    # Step 3: LLM classification
    # ------------------------------------------------------------------
    def _load_prompt(self) -> str:
        if self._prompt_template is None:
            prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "sanitize_commits.txt"
            self._prompt_template = prompt_path.read_text(encoding="utf-8")
        return self._prompt_template

    def _classify(
        self,
        issue_key: str,
        issue_description: str,
        branch_name: str,
        candidates: List[_Candidate],
    ) -> Optional[List[dict]]:
        files_section = "\n\n".join(c.to_prompt_section() for c in candidates)
        description_excerpt = (issue_description or "").strip()
        if len(description_excerpt) > 2000:
            description_excerpt = description_excerpt[:2000] + "\n... [truncated]"
        prompt = self._load_prompt().format(
            issue_key=issue_key,
            branch_name=branch_name,
            issue_description=description_excerpt or "(no description)",
            files_section=files_section,
        )
        try:
            result = self.llm.run(prompt, str(self.repo.repo_path), system_prompt="")
        except Exception as e:
            self.logger.warning(f"[sanitizer] LLM call failed: {e}")
            return None

        text = (result.text or "").strip()
        if not text:
            return None
        decisions = self._parse_json_array(text)
        if decisions is None:
            self.logger.warning(f"[sanitizer] could not parse LLM JSON; raw: {text[:300]}")
            return None

        # Only honor decisions for paths we actually presented
        presented = {c.path for c in candidates}
        clean: List[dict] = []
        for d in decisions:
            if not isinstance(d, dict):
                continue
            path = d.get("path")
            if path in presented:
                clean.append(d)
        return clean

    @staticmethod
    def _parse_json_array(text: str) -> Optional[list]:
        # Strip code fences
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```\s*$", "", stripped)
        # Find first JSON array in the text
        match = re.search(r"\[.*\]", stripped, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # Step 4: apply removals and append a cleanup commit
    # ------------------------------------------------------------------
    def _apply_removals(self, paths: List[str], issue_key: str) -> List[str]:
        removed: List[str] = []
        for path in paths:
            fp = Path(self.repo.repo_path) / path
            try:
                result = self.repo._run_git("rm", "-f", "--", path, check=False)
                if result.returncode != 0:
                    # File may be untracked or otherwise odd — fall back to filesystem delete
                    if fp.exists():
                        try:
                            fp.unlink()
                        except Exception as e:
                            self.logger.warning(f"[sanitizer] could not unlink {path}: {e}")
                            continue
                removed.append(path)
            except Exception as e:
                self.logger.warning(f"[sanitizer] failed to remove {path}: {e}")

        if not removed:
            return removed

        # Any staged changes? commit them.
        status = self.repo._run_git("status", "--porcelain", check=False).stdout.strip()
        if not status:
            return removed

        msg = f"{issue_key}: remove unintended artifacts ({len(removed)} file(s))"
        self.repo._run_git("commit", "-m", msg)
        self.logger.info(f"[sanitizer] {issue_key}: committed removal of {len(removed)} file(s)")
        return removed
