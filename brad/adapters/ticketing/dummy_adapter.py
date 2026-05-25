"""Local-only ticketing adapter for smoke tests and offline development."""
from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from brad.adapters.ticketing.base import TicketingAdapter
from brad.logging_config import get_logger


class DummyTicketingAdapter(TicketingAdapter):
    """A Jira-shaped adapter backed by a local text fixture and log file.

    It never talks to an external service. Updates are applied to an in-memory
    issue snapshot and appended to a log file for inspection.
    """

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.issue_fixture_path = Path(
            getattr(cfg, "dummy_ticket_path", None) or Path.cwd() / "dummy_ticket.txt"
        )
        self.log_path = Path(
            getattr(cfg, "dummy_ticket_log_path", None) or Path.cwd() / "dummy_ticket.log"
        )
        self.attachments_dir = Path(cfg.attachments_dir)
        self._issues: Dict[str, Dict] = {}
        self._load_issues()
        self.logger.info(
            f"Initialized DummyTicketingAdapter with fixture={self.issue_fixture_path} "
            f"log={self.log_path}"
        )

    def fetch_issues_with_label(self, label: str) -> List[Dict]:
        label = (label or "").strip()
        self._append_log({"event": "fetch_issues_with_label", "label": label})
        if not label:
            return []
        issues = [
            copy.deepcopy(issue)
            for issue in self._issues.values()
            if label in (issue.get("fields", {}).get("labels") or [])
        ]
        issues.sort(key=lambda issue: issue.get("key", ""))
        return issues

    def fetch_issue(self, issue_key: str) -> Optional[Dict]:
        issue_key = (issue_key or "").strip()
        self._append_log({"event": "fetch_issue", "issue_key": issue_key})
        issue = self._issues.get(issue_key)
        return copy.deepcopy(issue) if issue else None

    def remove_label(self, issue_key: str, label: str) -> None:
        self._mutate_labels(issue_key, label, remove=True)
        self._append_log({"event": "remove_label", "issue_key": issue_key, "label": label})

    def add_label(self, issue_key: str, label: str) -> None:
        self._mutate_labels(issue_key, label, remove=False)
        self._append_log({"event": "add_label", "issue_key": issue_key, "label": label})

    def comment(self, issue_key: str, text: str) -> None:
        self._append_log({"event": "comment", "issue_key": issue_key, "text": text})

    def set_status(self, issue_key: str, target_status: str) -> None:
        issue = self._require_issue(issue_key)
        issue["fields"]["status"] = {"name": target_status}
        self._append_log(
            {"event": "set_status", "issue_key": issue_key, "target_status": target_status}
        )

    def download_attachments(self, issue_key: str, attachments: List[Dict]) -> List[str]:
        issue_key = (issue_key or "").strip()
        if not attachments:
            self._append_log({"event": "download_attachments", "issue_key": issue_key, "count": 0})
            return []

        issue_dir = self.attachments_dir / issue_key
        issue_dir.mkdir(parents=True, exist_ok=True)
        downloaded_paths: List[str] = []

        for idx, attachment in enumerate(attachments, start=1):
            filename = attachment.get("filename") or attachment.get("name") or f"attachment-{idx}"
            target = issue_dir / filename
            source_path = attachment.get("path")
            content = attachment.get("content")

            if source_path and Path(source_path).exists():
                shutil.copyfile(source_path, target)
            elif isinstance(content, str):
                target.write_text(content, encoding="utf-8")
            else:
                target.touch()

            downloaded_paths.append(str(target))

        self._append_log(
            {
                "event": "download_attachments",
                "issue_key": issue_key,
                "count": len(downloaded_paths),
                "paths": downloaded_paths,
            }
        )
        return downloaded_paths

    def _load_issues(self) -> None:
        issue = self._parse_fixture(self.issue_fixture_path.read_text(encoding="utf-8")) if self.issue_fixture_path.exists() else self._default_issue()
        self._issues[issue["key"]] = issue

    def _parse_fixture(self, text: str) -> Dict:
        headers: Dict[str, str] = {}
        body_lines: List[str] = []
        in_body = False
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\n")
            if not in_body and not line.strip():
                in_body = True
                continue
            if not in_body and ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
            else:
                in_body = True
                body_lines.append(line)

        key = headers.get("key") or "DUMMY-1"
        summary = headers.get("summary") or "Dummy ticket"
        labels = [part.strip() for part in headers.get("labels", "BradReview").split(",") if part.strip()]
        status = headers.get("status") or "Open"
        project = headers.get("project") or "DUMMY"
        updated = headers.get("updated") or self._now()
        created = headers.get("created") or updated

        return {
            "key": key,
            "fields": {
                "summary": summary,
                "description": "\n".join(body_lines).strip(),
                "attachment": [],
                "status": {"name": status},
                "labels": labels,
                "project": {"key": project},
                "created": created,
                "updated": updated,
            },
        }

    def _default_issue(self) -> Dict:
        return self._parse_fixture(
            "\n".join(
                [
                    "key: DUMMY-1",
                    "summary: Smoke-test summary flow",
                    "labels: BradReview,dummy",
                    "status: Open",
                    "project: DUMMY",
                    "",
                    "This is a local-only smoke ticket.",
                    "It exists to exercise Brad's prompt, summary, and persistence paths.",
                ]
            )
        )

    def _mutate_labels(self, issue_key: str, label: str, *, remove: bool) -> None:
        issue = self._require_issue(issue_key)
        labels = issue["fields"].setdefault("labels", [])
        normalized = (label or "").strip()
        if not normalized:
            return
        if remove:
            issue["fields"]["labels"] = [existing for existing in labels if existing != normalized]
        elif normalized not in labels:
            labels.append(normalized)

    def _require_issue(self, issue_key: str) -> Dict:
        issue_key = (issue_key or "").strip()
        if issue_key not in self._issues:
            raise KeyError(f"Unknown dummy issue: {issue_key}")
        return self._issues[issue_key]

    def _append_log(self, event: Dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": self._now(),
            **event,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
