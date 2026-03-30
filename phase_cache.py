"""
Phase-level caching for Brad.

Caches expensive AI phase outputs (e.g. requirements analysis) keyed on
a composite of:
  a) HEAD commit of the base branch (main)
  b) Jira ticket last-updated timestamp
  c) Model identity (endpoint + model/deployment name)

Cache is stored as JSON files under .brad_cache/<issue_key>/.
"""

import json
import hashlib
from pathlib import Path
from typing import Dict, Optional
from logging_config import get_logger

logger = get_logger(__name__)

CACHE_DIR = Path(".brad_cache")


def _make_key(main_commit: str, jira_updated: str, model_identity: str) -> str:
    """Deterministic cache key from the three components."""
    raw = f"{main_commit}|{jira_updated}|{model_identity}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def get_cached_phase(
    issue_key: str,
    phase: str,
    main_commit: str,
    jira_updated: str,
    model_identity: str,
) -> Optional[Dict]:
    """Return cached phase result if it exists and key matches, else None."""
    key = _make_key(main_commit, jira_updated, model_identity)
    cache_file = CACHE_DIR / issue_key / f"{phase}_{key}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            logger.info(f"Cache HIT for {issue_key}/{phase} (key={key})")
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Cache read error for {issue_key}/{phase}: {e}")
    return None


def set_cached_phase(
    issue_key: str,
    phase: str,
    main_commit: str,
    jira_updated: str,
    model_identity: str,
    result: Dict,
) -> None:
    """Write phase result to cache. Overwrites any stale entries for the same phase."""
    key = _make_key(main_commit, jira_updated, model_identity)
    issue_dir = CACHE_DIR / issue_key
    issue_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale cache files for this phase (different keys)
    for old in issue_dir.glob(f"{phase}_*.json"):
        try:
            old.unlink()
        except OSError:
            pass

    cache_file = issue_dir / f"{phase}_{key}.json"
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Cache SET for {issue_key}/{phase} (key={key})")
