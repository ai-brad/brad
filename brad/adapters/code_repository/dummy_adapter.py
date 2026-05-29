"""Local-only code repository adapter for smoke tests and offline development.

All write operations (open_pr, close_pr, reply_to_*) are logged to a file and
no-op'd. All read operations return empty/null values that represent a clean
slate with no existing PRs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from brad.adapters.code_repository.base import CodeRepositoryAdapter
from brad.logging_config import get_logger


class DummyCodeRepositoryAdapter(CodeRepositoryAdapter):
    """A GitHub-shaped adapter that never touches the network.

    - pr_exists_for_branch always returns None (no existing PR)
    - get_brad_prs returns [] (no open Brad PRs to process)
    - open_pr records the call and returns a fake PR dict
    - All comment/reply operations are logged only
    - CI check queries return [] (no CI runs to wait on)
    """

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.log_path = Path(
            getattr(cfg, "dummy_code_repo_log_path", None) or Path.cwd() / "dummy_code_repo.log"
        )
        self._next_pr_number = 1
        self._open_prs: Dict[int, Dict] = {}
        self.logger.info(f"DummyCodeRepositoryAdapter initialised (log={self.log_path})")

    # ------------------------------------------------------------------
    # PR lifecycle
    # ------------------------------------------------------------------

    def open_pr(self, branch: str, title: str, body: str, base: str = "main") -> Dict:
        pr_number = self._next_pr_number
        self._next_pr_number += 1
        pr = {
            "number": pr_number,
            "html_url": f"https://github.com/dummy/repo/pull/{pr_number}",
            "state": "open",
            "title": title,
            "head": {"ref": branch},
            "base": {"ref": base},
        }
        self._open_prs[pr_number] = pr
        self._log({"event": "open_pr", "branch": branch, "title": title, "pr_number": pr_number})
        self.logger.info(f"DummyCodeRepo: opened PR #{pr_number} for branch '{branch}'")
        return pr

    def get_pr(self, pr_number: int) -> Dict:
        self._log({"event": "get_pr", "pr_number": pr_number})
        return self._open_prs.get(pr_number, {
            "number": pr_number,
            "state": "open",
            "html_url": f"https://github.com/dummy/repo/pull/{pr_number}",
            "head": {"ref": "unknown"},
            "base": {"ref": "main"},
        })

    def pr_exists_for_branch(self, branch: str) -> Optional[int]:
        self._log({"event": "pr_exists_for_branch", "branch": branch})
        for pr in self._open_prs.values():
            if pr.get("head", {}).get("ref") == branch:
                return pr["number"]
        return None

    def close_pr(self, pr_number: int, comment: Optional[str] = None) -> None:
        self._open_prs.pop(pr_number, None)
        self._log({"event": "close_pr", "pr_number": pr_number, "comment": comment})

    def get_brad_prs(self) -> List[Dict]:
        self._log({"event": "get_brad_prs"})
        return []

    def get_pr_details(self, pr_number: int) -> Dict:
        self._log({"event": "get_pr_details", "pr_number": pr_number})
        return self.get_pr(pr_number)

    # ------------------------------------------------------------------
    # Reviews / comments — all no-ops that log
    # ------------------------------------------------------------------

    def fetch_review_comments(self, pr_number: int) -> List[Dict]:
        self._log({"event": "fetch_review_comments", "pr_number": pr_number})
        return []

    def get_review_comments_needing_response(self, pr_number: int) -> List[Dict]:
        self._log({"event": "get_review_comments_needing_response", "pr_number": pr_number})
        return []

    def reply_to_review_comment(self, pr_number: int, comment_id: int, body: str) -> Dict:
        self._log({"event": "reply_to_review_comment", "pr_number": pr_number,
                   "comment_id": comment_id, "body": body})
        return {}

    def get_pr_reviews(self, pr_number: int) -> List[Dict]:
        self._log({"event": "get_pr_reviews", "pr_number": pr_number})
        return []

    def get_comment_replies(self, pr_number: int, comment_id: int) -> List[Dict]:
        self._log({"event": "get_comment_replies", "pr_number": pr_number,
                   "comment_id": comment_id})
        return []

    def fetch_issue_comments(self, pr_number: int) -> List[Dict]:
        self._log({"event": "fetch_issue_comments", "pr_number": pr_number})
        return []

    def get_issue_comments_needing_response(self, pr_number: int) -> List[Dict]:
        self._log({"event": "get_issue_comments_needing_response", "pr_number": pr_number})
        return []

    def reply_to_issue_comment(self, pr_number: int, comment_id: int, body: str) -> Dict:
        self._log({"event": "reply_to_issue_comment", "pr_number": pr_number,
                   "comment_id": comment_id, "body": body})
        return {}

    def get_review_level_comments_needing_response(self, pr_number: int) -> List[Dict]:
        self._log({"event": "get_review_level_comments_needing_response",
                   "pr_number": pr_number})
        return []

    # ------------------------------------------------------------------
    # CI checks — always empty (no CI in dummy mode)
    # ------------------------------------------------------------------

    def get_pr_checks(self, pr_number: int) -> List[Dict]:
        self._log({"event": "get_pr_checks", "pr_number": pr_number})
        return []

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _log(self, event: Dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": self._now(), **event}
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
