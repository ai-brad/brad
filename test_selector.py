"""
Smart test selection for Brad.

Given a feature branch, determines which test files/directories are relevant
to the source files that changed, so the agent only runs those tests locally
instead of the full suite.
"""

import subprocess
from pathlib import Path
from typing import List, Optional
from logging_config import get_logger

logger = get_logger(__name__)


def get_changed_files(repo_path: str, base_branch: str = "main") -> List[str]:
    """Return list of files changed in the current branch vs base_branch."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
            capture_output=True, text=True, cwd=repo_path, timeout=30,
        )
        if result.returncode != 0:
            # Fallback: diff against working tree
            result = subprocess.run(
                ["git", "diff", "--name-only", base_branch],
                capture_output=True, text=True, cwd=repo_path, timeout=30,
            )
        files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
        logger.info(f"Changed files vs {base_branch}: {len(files)} files")
        return files
    except Exception as e:
        logger.warning(f"Failed to get changed files: {e}")
        return []


def map_source_to_test_dirs(changed_files: List[str]) -> List[str]:
    """
    Map changed source files to the test directories that likely cover them.

    Heuristic:
      src/bea/modules/text_agent/foo.py  →  tests/unit/modules/text_agent/
      src/bea/base/services/bar.py       →  tests/unit/base/services/ (if exists)
      src/bea/common/baz.py              →  tests/unit/common/

    Returns de-duplicated list of test directory paths (relative to repo root).
    """
    test_dirs = set()
    for f in changed_files:
        # Normalize to forward slashes
        f = f.replace("\\", "/")
        parts = f.split("/")

        # Only map Python source files
        if not f.endswith(".py"):
            continue

        # Map src/bea/... to tests/unit/...
        if len(parts) >= 3 and parts[0] == "src" and parts[1] == "bea":
            # e.g. src/bea/modules/text_agent/foo.py → tests/unit/modules/text_agent/
            module_parts = parts[2:-1]  # strip src/bea/ prefix and filename
            if module_parts:
                test_dir = "/".join(["tests", "unit"] + module_parts)
                test_dirs.add(test_dir)

        # Also include test files themselves if they changed
        if f.startswith("tests/"):
            # Add the parent directory of the changed test file
            test_dir = "/".join(parts[:-1])
            test_dirs.add(test_dir)

    return sorted(test_dirs)


def build_pytest_target(repo_path: str, base_branch: str = "main") -> Optional[str]:
    """
    Build the pytest target string for running only relevant tests.

    Returns e.g. "tests/unit/modules/text_agent/ tests/unit/base/services/"
    or None if we can't determine (fallback to nothing).
    """
    changed = get_changed_files(repo_path, base_branch)
    if not changed:
        return None

    test_dirs = map_source_to_test_dirs(changed)
    if not test_dirs:
        return None

    # Filter to dirs that actually exist in the repo
    existing = []
    for d in test_dirs:
        full = Path(repo_path) / d
        if full.exists() and full.is_dir():
            existing.append(d)
        else:
            logger.debug(f"Test dir not found, skipping: {d}")

    if not existing:
        return None

    target = " ".join(existing)
    logger.info(f"Smart test target: {target}")
    return target


def extract_failed_tests_from_ci_logs(ci_logs: str) -> Optional[str]:
    """
    Parse CI logs to extract specific failing test paths/names.

    Looks for pytest's short test summary output like:
      FAILED tests/unit/modules/text_agent/test_core.py::test_something
      FAILED tests/unit/base/test_foo.py::TestBar::test_baz

    Returns a space-separated string of failed test identifiers,
    or None if we can't parse them.
    """
    import re

    # pytest FAILED lines
    failed = re.findall(
        r"FAILED\s+(tests/\S+::\S+)", ci_logs
    )
    if failed:
        # Deduplicate, keep order
        seen = set()
        unique = []
        for t in failed:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        target = " ".join(unique)
        logger.info(f"Extracted {len(unique)} failed tests from CI logs")
        return target

    # Fallback: look for file-level failures
    file_failed = re.findall(
        r"FAILED\s+(tests/\S+\.py)", ci_logs
    )
    if file_failed:
        unique = sorted(set(file_failed))
        target = " ".join(unique)
        logger.info(f"Extracted {len(unique)} failed test files from CI logs")
        return target

    return None
