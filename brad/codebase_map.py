"""
Codebase map generator for Brad.

Generates a compact summary of the target repository's structure and key
symbols (classes, functions) so the AI agent can skip exploratory iterations.

The map is cached by the HEAD commit of the main branch — regenerated only
when main changes.
"""

import ast
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional
from brad.logging_config import get_logger
from brad.phase_cache import CACHE_DIR

logger = get_logger(__name__)

MAX_MAP_CHARS = 12_000

SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.mypy_cache', '.pytest_cache',
             'dist', 'build', '.tox', '.eggs', 'venv', '.venv', '.brad_cache'}


def _get_main_commit(repo_path: str) -> str:
    """Get short SHA of origin/main HEAD."""
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--short", "origin/main"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _cache_path(repo_path: str, commit: str) -> Path:
    """Cache file path for the codebase map."""
    repo_name = Path(repo_path).name
    return CACHE_DIR / f"_codebase_map_{repo_name}_{commit}.json"


def _extract_python_symbols(file_path: Path) -> List[str]:
    """Extract top-level class and function names from a Python file."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError, Exception):
        return []

    symbols = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = [n.name for n in ast.iter_child_nodes(node)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and not n.name.startswith("_")]
            if methods:
                symbols.append(f"class {node.name}({', '.join(methods[:8])})")
            else:
                symbols.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                symbols.append(f"def {node.name}")
    return symbols


def _build_tree(repo_path: str) -> Dict[str, any]:
    """Walk the repo and build a compact map."""
    root = Path(repo_path)
    dir_stats: Dict[str, int] = {}
    symbols: Dict[str, List[str]] = {}

    for py_file in root.rglob("*.py"):
        if any(skip in py_file.parts for skip in SKIP_DIRS):
            continue
        rel = py_file.relative_to(root)
        parent_rel = str(rel.parent).replace("\\", "/")

        dir_stats[parent_rel] = dir_stats.get(parent_rel, 0) + 1

        syms = _extract_python_symbols(py_file)
        if syms:
            symbols[str(rel).replace("\\", "/")] = syms

    tree_lines = []
    for d in sorted(dir_stats):
        count = dir_stats[d]
        tree_lines.append(f"{d}/  ({count} py)")

    return {"tree": tree_lines, "symbols": symbols}


def _format_map(data: Dict) -> str:
    """Format the map dict into a compact text block for prompt injection."""
    lines = ["CODEBASE MAP (auto-generated — do NOT list_directory or find_files for structure discovery):\n"]

    lines.append("Directory structure:")
    for t in data["tree"]:
        lines.append(f"  {t}")

    lines.append("\nKey symbols:")
    budget = MAX_MAP_CHARS - sum(len(l) for l in lines) - 200
    for fpath, syms in sorted(data["symbols"].items()):
        entry = f"  {fpath}: {'; '.join(syms)}"
        if len(entry) > budget:
            lines.append("  ... (truncated)")
            break
        lines.append(entry)
        budget -= len(entry)

    return "\n".join(lines)


def get_codebase_map(repo_path: str, main_commit: Optional[str] = None) -> str:
    """Return a compact codebase map string, cached by main commit."""
    if not main_commit:
        main_commit = _get_main_commit(repo_path)

    cache_file = _cache_path(repo_path, main_commit)

    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            text = _format_map(data)
            logger.info(f"Codebase map cache HIT (commit={main_commit}, {len(text)} chars)")
            return text
        except Exception as e:
            logger.warning(f"Codebase map cache read error: {e}")

    logger.info(f"Generating codebase map for {repo_path} (commit={main_commit})")
    data = _build_tree(repo_path)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    repo_name = Path(repo_path).name
    for old in CACHE_DIR.glob(f"_codebase_map_{repo_name}_*.json"):
        try:
            old.unlink()
        except OSError:
            pass

    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    text = _format_map(data)
    logger.info(f"Codebase map generated ({len(data['tree'])} dirs, {len(data['symbols'])} files, {len(text)} chars)")
    return text
