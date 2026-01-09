import requests
from typing import List, Dict, Optional
from logging_config import get_logger
from pathlib import Path
import time


class JiraClient:
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.base_url = cfg.jira_url.rstrip("/")
        self.auth = (cfg.jira_user, cfg.jira_token)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.attachments_dir = Path(cfg.attachments_dir)
        self.logger.info(f"Initialized JIRA client for {self.base_url}")

    # -------------------------
    # Issue fetching
    # -------------------------

    def fetch_issues_with_label(self, label: str) -> List[Dict]:
        jql = f'labels = "{label}" ORDER BY created ASC'
        self.logger.info(f"Fetching issues with label: {label}")
        self.logger.debug(f"JQL query: {jql}")

        try:
            resp = requests.post(
                f"{self.base_url}/rest/api/3/search/jql",
                auth=self.auth,
                headers=self.headers,
                json={
                    "jql": jql,
                    "fields": ["summary", "description", "attachment", "status", "assignee", "created", "updated"],
                },
                timeout=30,
            )
            resp.raise_for_status()

            data = resp.json()
            issues = data.get("issues", [])
            self.logger.info(f"Found {len(issues)} issues with label '{label}'")
            
            for issue in issues:
                self.logger.debug(f"  - {issue['key']}: {issue['fields'].get('summary', 'No summary')}")
            
            return issues
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to fetch issues: {e}")
            raise

    # -------------------------
    # Labels
    # -------------------------

    def remove_label(self, issue_key: str, label: str):
        self.logger.info(f"Removing label '{label}' from {issue_key}")
        payload = {
            "update": {
                "labels": [
                    {"remove": label}
                ]
            }
        }

        try:
            self._put_issue(issue_key, payload)
            self.logger.info(f"Successfully removed label '{label}' from {issue_key}")
        except Exception as e:
            self.logger.error(f"Failed to remove label '{label}' from {issue_key}: {e}")
            raise

    def add_label(self, issue_key: str, label: str):
        self.logger.info(f"Adding label '{label}' to {issue_key}")
        payload = {
            "update": {
                "labels": [
                    {"add": label}
                ]
            }
        }

        try:
            self._put_issue(issue_key, payload)
            self.logger.info(f"Successfully added label '{label}' to {issue_key}")
        except Exception as e:
            self.logger.error(f"Failed to add label '{label}' to {issue_key}: {e}")
            raise

    # -------------------------
    # Comments
    # -------------------------

    def comment(self, issue_key: str, text: str):
        self.logger.info(f"Adding comment to {issue_key}")
        self.logger.debug(f"Comment text (first 100 chars): {text[:100]}...")
        
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": text,
                            }
                        ],
                    }
                ],
            }
        }

        try:
            resp = requests.post(
                f"{self.base_url}/rest/api/3/issue/{issue_key}/comment",
                auth=self.auth,
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            self.logger.info(f"Successfully added comment to {issue_key}")
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to add comment to {issue_key}: {e}")
            raise

    # -------------------------
    # Status transitions
    # -------------------------

    def set_status(self, issue_key: str, target_status: str):
        self.logger.info(f"Setting {issue_key} status to '{target_status}'")
        
        try:
            transitions = self._get_transitions(issue_key)
            self.logger.debug(f"Available transitions: {[t['to']['name'] for t in transitions]}")

            transition_id = None
            for t in transitions:
                if t["to"]["name"].lower() == target_status.lower():
                    transition_id = t["id"]
                    break

            if not transition_id:
                available = [t["to"]["name"] for t in transitions]
                self.logger.error(f"No transition to '{target_status}' for {issue_key}. Available: {available}")
                raise RuntimeError(
                    f"No transition to status '{target_status}' "
                    f"for issue {issue_key}. Available: {available}"
                )

            payload = {
                "transition": {
                    "id": transition_id
                }
            }

            resp = requests.post(
                f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions",
                auth=self.auth,
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            self.logger.info(f"Successfully set {issue_key} status to '{target_status}'")
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to set status for {issue_key}: {e}")
            raise

    # -------------------------
    # Internal helpers
    # -------------------------

    def _get_transitions(self, issue_key: str) -> List[Dict]:
        resp = requests.get(
            f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions",
            auth=self.auth,
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()

        return resp.json().get("transitions", [])

    def download_attachments(self, issue_key: str, attachments: List[Dict]) -> List[str]:
        """
        Download all attachments from a JIRA issue to local directory.
        Returns list of local file paths.
        """
        if not attachments:
            self.logger.info(f"{issue_key}: No attachments to download")
            return []
        
        self.logger.info(f"Downloading {len(attachments)} attachments from {issue_key}")
        issue_dir = self.attachments_dir / issue_key
        issue_dir.mkdir(parents=True, exist_ok=True)
        
        downloaded_paths = []
        
        for attachment in attachments:
            filename = attachment.get("filename", "unknown")
            content_url = attachment.get("content")
            
            if not content_url:
                self.logger.warning(f"No content URL for attachment: {filename}")
                continue
            
            local_path = issue_dir / filename
            
            try:
                self.logger.debug(f"Downloading {filename} from {content_url}")
                resp = requests.get(
                    content_url,
                    auth=self.auth,
                    timeout=120,
                    stream=True
                )
                resp.raise_for_status()
                
                with open(local_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                self.logger.info(f"Downloaded {filename} to {local_path}")
                downloaded_paths.append(str(local_path))
                
            except Exception as e:
                self.logger.error(f"Failed to download {filename}: {e}")
        
        return downloaded_paths
    
    def _put_issue(self, issue_key: str, payload: Dict):
        resp = requests.put(
            f"{self.base_url}/rest/api/3/issue/{issue_key}",
            auth=self.auth,
            headers=self.headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
