"""GitHub code repository adapter."""
import requests
from typing import List, Dict, Optional
from brad.adapters.code_repository.base import CodeRepositoryAdapter
from brad.logging_config import get_logger


class GitHubAdapter(CodeRepositoryAdapter):
    """GitHub implementation of the CodeRepositoryAdapter interface."""

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.token = cfg.github_token
        self.repo = cfg.github_repo
        self.base_url = f"https://api.github.com/repos/{self.repo}"
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
        }
        self.logger.info(f"Initialized GitHub adapter for {self.repo}")

    # -------------------------
    # Open Pull Request
    # -------------------------
    def open_pr(self, branch: str, title: str, body: str, base: str = "main") -> Dict:
        self.logger.info(f"Opening PR: {branch} -> {base}")
        self.logger.debug(f"PR title: {title}")

        if not title.startswith("Brad: "):
            title = f"Brad: {title}"

        payload = {
            "title": title,
            "head": branch,
            "base": base,
            "body": body,
            "maintainer_can_modify": True,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/pulls",
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            pr_data = resp.json()
            self.logger.info(f"Successfully opened PR #{pr_data['number']}: {pr_data['html_url']}")
            return pr_data
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to open PR: {e}")
            if hasattr(e.response, 'text'):
                self.logger.error(f"Response: {e.response.text}")
            raise

    # -------------------------
    # Fetch PR review comments
    # -------------------------
    def fetch_review_comments(self, pr_number: int) -> List[Dict]:
        resp = requests.get(
            f"{self.base_url}/pulls/{pr_number}/comments",
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # -------------------------
    # Get PR info
    # -------------------------
    def get_pr(self, pr_number: int) -> Dict:
        self.logger.debug(f"Fetching PR #{pr_number}")
        try:
            resp = requests.get(
                f"{self.base_url}/pulls/{pr_number}",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to fetch PR #{pr_number}: {e}")
            raise

    def pr_exists_for_branch(self, branch: str) -> Optional[int]:
        """Check if a PR already exists for the given branch. Returns PR number if exists, None otherwise."""
        self.logger.debug(f"Checking for existing PR for branch: {branch}")
        try:
            resp = requests.get(
                f"{self.base_url}/pulls",
                headers=self.headers,
                params={"head": f"{self.repo.split('/')[0]}:{branch}", "state": "open"},
                timeout=30,
            )
            resp.raise_for_status()
            prs = resp.json()
            if prs:
                pr_number = prs[0]['number']
                self.logger.info(f"Found existing PR #{pr_number} for branch {branch}")
                return pr_number
            return None
        except Exception as e:
            self.logger.error(f"Failed to check for existing PR: {e}")
            return None

    def get_brad_prs(self) -> List[Dict]:
        """Get all open PRs created by Brad (prefix 'Brad: ')."""
        self.logger.debug("Fetching Brad's PRs")
        try:
            resp = requests.get(
                f"{self.base_url}/pulls",
                headers=self.headers,
                params={"state": "open"},
                timeout=30,
            )
            resp.raise_for_status()
            all_prs = resp.json()
            brad_prs = [pr for pr in all_prs if pr['title'].startswith('Brad: ')]
            self.logger.info(f"Found {len(brad_prs)} Brad PRs")
            return brad_prs
        except Exception as e:
            self.logger.error(f"Failed to fetch Brad PRs: {e}")
            return []

    def get_review_comments_needing_response(self, pr_number: int) -> List[Dict]:
        """Get review comments that don't have a Brad response yet."""
        self.logger.debug(f"Checking PR #{pr_number} for unresponded review comments")
        try:
            comments = self.fetch_review_comments(pr_number)
            needs_response = []

            self.logger.info(f"PR #{pr_number}: Found {len(comments)} total review comments")

            for comment in comments:
                comment_id = comment['id']
                comment_author = comment.get('user', {}).get('login', 'unknown')
                comment_body_preview = comment.get('body', '')[:100]

                # Skip if comment itself is from Brad
                if comment.get('body', '').startswith('Brad checking'):
                    self.logger.debug(f"Comment {comment_id} is Brad's own response - skipping")
                    continue

                # Check if there are any Brad replies to this comment
                replies = self.get_comment_replies(pr_number, comment_id)
                has_brad_response = any(
                    reply.get('body', '').startswith('Brad checking')
                    for reply in replies
                )

                if has_brad_response:
                    self.logger.debug(f"Comment {comment_id} by {comment_author} already has Brad response - skipping")
                else:
                    self.logger.info(f"Comment {comment_id} by {comment_author} NEEDS response: {comment_body_preview}...")
                    needs_response.append(comment)

            self.logger.info(f"PR #{pr_number}: {len(needs_response)} comments need response")
            return needs_response
        except Exception as e:
            self.logger.error(f"Failed to get review comments: {e}")
            return []

    def get_comment_replies(self, pr_number: int, comment_id: int) -> List[Dict]:
        """Get replies to a specific review comment."""
        try:
            resp = requests.get(
                f"{self.base_url}/pulls/{pr_number}/comments",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            all_comments = resp.json()
            return [
                c for c in all_comments
                if c.get('in_reply_to_id') == comment_id
            ]
        except Exception as e:
            self.logger.error(f"Failed to get comment replies: {e}")
            return []

    def reply_to_review_comment(self, pr_number: int, comment_id: int, body: str) -> Dict:
        """Reply to a review comment."""
        self.logger.info(f"Replying to comment {comment_id} on PR #{pr_number}")
        try:
            payload = {
                "body": body,
                "in_reply_to": comment_id,
            }
            resp = requests.post(
                f"{self.base_url}/pulls/{pr_number}/comments",
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self.logger.error(f"Failed to reply to comment: {e}")
            raise

    def close_pr(self, pr_number: int, comment: Optional[str] = None) -> None:
        """Close a pull request, optionally leaving a comment first."""
        self.logger.info(f"Closing PR #{pr_number}")
        try:
            if comment:
                requests.post(
                    f"{self.base_url}/issues/{pr_number}/comments",
                    headers=self.headers,
                    json={"body": comment},
                    timeout=30,
                )
            resp = requests.patch(
                f"{self.base_url}/pulls/{pr_number}",
                headers=self.headers,
                json={"state": "closed"},
                timeout=30,
            )
            resp.raise_for_status()
            self.logger.info(f"Successfully closed PR #{pr_number}")
        except Exception as e:
            self.logger.error(f"Failed to close PR #{pr_number}: {e}")
            raise

    def get_pr_details(self, pr_number: int) -> Dict:
        """Get enriched PR details including status, reviews, and check runs."""
        pr = self.get_pr(pr_number)
        state = pr.get("state", "open")
        if pr.get("merged"):
            state = "merged"

        reviews = self.get_pr_reviews(pr_number)
        checks = self.get_pr_checks(pr_number)

        return {
            "number": pr_number,
            "title": pr.get("title", ""),
            "state": state,
            "html_url": pr.get("html_url", ""),
            "created_at": pr.get("created_at", ""),
            "updated_at": pr.get("updated_at", ""),
            "merged_at": pr.get("merged_at"),
            "head_branch": pr.get("head", {}).get("ref", ""),
            "reviews": reviews,
            "checks": checks,
        }

    def get_pr_reviews(self, pr_number: int) -> List[Dict]:
        """Get all reviews on a PR with reviewer and state."""
        try:
            resp = requests.get(
                f"{self.base_url}/pulls/{pr_number}/reviews",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            raw = resp.json()
            return [
                {
                    "user": r.get("user", {}).get("login", "unknown"),
                    "state": r.get("state", "PENDING"),
                    "submitted_at": r.get("submitted_at", ""),
                    "body": (r.get("body") or "")[:200],
                }
                for r in raw
            ]
        except Exception as e:
            self.logger.error(f"Failed to get reviews for PR #{pr_number}: {e}")
            return []

    def get_pr_checks(self, pr_number: int) -> List[Dict]:
        """Get CI check runs for a PR's head commit."""
        try:
            pr = self.get_pr(pr_number)
            sha = pr.get("head", {}).get("sha", "")
            if not sha:
                return []
            resp = requests.get(
                f"{self.base_url}/commits/{sha}/check-runs",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            runs = resp.json().get("check_runs", [])
            return [
                {
                    "name": r.get("name", ""),
                    "status": r.get("status", "queued"),
                    "conclusion": r.get("conclusion"),
                    "started_at": r.get("started_at", ""),
                    "completed_at": r.get("completed_at"),
                    "html_url": r.get("html_url", ""),
                }
                for r in runs
            ]
        except Exception as e:
            self.logger.error(f"Failed to get checks for PR #{pr_number}: {e}")
            return []
