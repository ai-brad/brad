"""Local-only CI/CD adapter for smoke tests and offline development.

All operations are no-ops. wait_for_pr immediately returns success so the
orchestrator flow completes without blocking.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from brad.adapters.ci_cd.base import CICDAdapter, CIResult, DeploymentInfo
from brad.logging_config import get_logger


class DummyCICDAdapter(CICDAdapter):
    """A CI/CD adapter that never touches the network.

    - wait_for_pr returns an immediate success result
    - All log / health-check queries return empty/healthy defaults
    """

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.log_path = Path(
            getattr(cfg, "dummy_ci_log_path", None) or Path.cwd() / "dummy_ci.log"
        )
        self.logger.info(f"DummyCICDAdapter initialised (log={self.log_path})")

    def wait_for_pr(
        self,
        pr_number: int,
        poll_interval: int = 60,
        timeout: int = 3600,
        issue_key: Optional[str] = None,
    ) -> CIResult:
        self._log({"event": "wait_for_pr", "pr_number": pr_number, "issue_key": issue_key})
        self.logger.info(f"DummyCI: wait_for_pr #{pr_number} — returning immediate success")
        return CIResult(success=True, logs="", failed_jobs=[])

    def resolve_deployment_env(
        self,
        pr_number: Optional[int] = None,
        branch: Optional[str] = None,
    ) -> Optional[DeploymentInfo]:
        self._log({"event": "resolve_deployment_env", "pr_number": pr_number, "branch": branch})
        return None

    def check_deployment_health(
        self,
        pr_number: Optional[int] = None,
        branch: Optional[str] = None,
        max_attempts: int = 30,
        wait_seconds: int = 10,
        issue_key: Optional[str] = None,
    ) -> Dict:
        self._log({"event": "check_deployment_health", "pr_number": pr_number, "branch": branch})
        return {"healthy": True, "detail": "dummy — no deployment to check"}

    def get_job_logs(self, run_id: int, failed_only: bool = True) -> str:
        self._log({"event": "get_job_logs", "run_id": run_id})
        return ""

    def get_failed_run_ids(self, pr_number: int) -> List[int]:
        self._log({"event": "get_failed_run_ids", "pr_number": pr_number})
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
