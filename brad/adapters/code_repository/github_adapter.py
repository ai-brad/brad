"""GitHub code repository adapter."""
import time
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
        self._max_retries = 5
        self._base_wait = 5  # seconds
        self.logger.info(f"Initialized GitHub adapter for {self.repo}")

    def _request_with_retry(self, method: str, url: str, **kwargs):
        """HTTP request with retry on transient network errors."""
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("headers", self.headers)
        func = getattr(requests, method)
        for attempt in range(self._max_retries):
            try:
                resp = func(url, **kwargs)
                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt == self._max_retries - 1:
                    raise
                wait = self._base_wait * (2 ** attempt)
                self.logger.warning(f"Network error ({type(e).__name__}), retrying in {wait}s (attempt {attempt+1}/{self._max_retries})")
                time.sleep(wait)

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
            resp = self._request_with_retry(
                "post",
                f"{self.base_url}/pulls",
                json=payload,
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
        return self._fetch_all_pages(f"{self.base_url}/pulls/{pr_number}/comments")

    def _fetch_all_pages(self, url: str, params: dict = None) -> List[Dict]:
        """Fetch all pages from a paginated GitHub API endpoint."""
        results = []
        p = dict(params or {})
        p.setdefault("per_page", 100)
        page = 1
        while True:
            p["page"] = page
            resp = self._request_with_retry("get", url, params=p)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            results.extend(data)
            if len(data) < p["per_page"]:
                break
            page += 1
        return results

    # -------------------------
    # Get PR info
    # -------------------------
    def get_pr(self, pr_number: int) -> Dict:
        self.logger.debug(f"Fetching PR #{pr_number}")
        try:
            resp = self._request_with_retry(
                "get",
                f"{self.base_url}/pulls/{pr_number}",
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
            resp = self._request_with_retry(
                "get",
                f"{self.base_url}/pulls",
                params={"head": f"{self.repo.split('/')[0]}:{branch}", "state": "open"},
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
            resp = self._request_with_retry(
                "get",
                f"{self.base_url}/pulls",
                params={"state": "open"},
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
        """Get review comments that don't have a Brad response yet.

        A thread needs a response when:
        - It has no Brad reply at all, OR
        - The LAST reply in the thread is NOT from Brad (a human replied after Brad).
        """
        self.logger.debug(f"Checking PR #{pr_number} for unresponded review comments")
        try:
            all_comments = self.fetch_review_comments(pr_number)
            self.logger.info(f"PR #{pr_number}: Found {len(all_comments)} total review comments")

            _BRAD_PREFIXES = ('Brad reaction: ', 'Brad checking')

            # Group: top-level comments and replies per parent
            top_level = []
            replies_by_parent: Dict[int, List[Dict]] = {}
            for c in all_comments:
                parent_id = c.get('in_reply_to_id')
                if parent_id:
                    replies_by_parent.setdefault(parent_id, []).append(c)
                else:
                    top_level.append(c)

            needs_response = []
            for comment in top_level:
                comment_id = comment['id']
                comment_author = comment.get('user', {}).get('login', 'unknown')
                comment_body = comment.get('body', '')

                # Skip if comment itself is from Brad
                if any(comment_body.startswith(p) for p in _BRAD_PREFIXES):
                    self.logger.debug(f"Comment {comment_id} is Brad's own response - skipping")
                    continue

                # Check the LAST reply in the thread
                thread_replies = replies_by_parent.get(comment_id, [])
                if thread_replies:
                    last_reply_body = thread_replies[-1].get('body', '')
                    brad_spoke_last = any(last_reply_body.startswith(p) for p in _BRAD_PREFIXES)
                else:
                    brad_spoke_last = False

                if brad_spoke_last:
                    self.logger.debug(f"Comment {comment_id} by {comment_author} — Brad spoke last, skipping")
                    continue

                self.logger.info(f"Comment {comment_id} by {comment_author} NEEDS response: {comment_body[:100]}...")
                needs_response.append(comment)

            self.logger.info(f"PR #{pr_number}: {len(needs_response)} comments need response")
            return needs_response
        except Exception as e:
            self.logger.error(f"Failed to get review comments: {e}")
            return []

    def get_comment_replies(self, pr_number: int, comment_id: int) -> List[Dict]:
        """Get replies to a specific review comment."""
        try:
            all_comments = self._fetch_all_pages(
                f"{self.base_url}/pulls/{pr_number}/comments"
            )
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
            resp = self._request_with_retry(
                "post",
                f"{self.base_url}/pulls/{pr_number}/comments",
                json=payload,
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
                self._request_with_retry(
                    "post",
                    f"{self.base_url}/issues/{pr_number}/comments",
                    json={"body": comment},
                )
            resp = self._request_with_retry(
                "patch",
                f"{self.base_url}/pulls/{pr_number}",
                json={"state": "closed"},
            )
            resp.raise_for_status()
            self.logger.info(f"Successfully closed PR #{pr_number}")
        except Exception as e:
            self.logger.error(f"Failed to close PR #{pr_number}: {e}")
            raise

    def rebase_pr(self, pr_number: int) -> dict:
        """Attempt to update a PR branch with latest base branch changes.

        Uses GitHub's update-branch API. Returns a dict with:
        - 'rebased': True if branch was updated
        - 'up_to_date': True if already up to date
        - 'conflict': True if merge conflicts prevent rebase
        - 'error': error message if something else went wrong
        """
        self.logger.info(f"Attempting to rebase PR #{pr_number}")
        try:
            # First get PR to check if it needs rebasing
            pr = self.get_pr(pr_number)
            if pr.get("state") != "open":
                return {"rebased": False, "up_to_date": False, "error": "PR is not open"}

            head_sha = pr.get("head", {}).get("sha", "")
            mergeable = pr.get("mergeable")

            # If GitHub already determined there are conflicts, skip
            if mergeable is False:
                self.logger.info(f"PR #{pr_number} has merge conflicts, skipping rebase")
                return {"rebased": False, "conflict": True}

            # Check if behind base branch
            base_branch = pr.get("base", {}).get("ref", "main")
            head_branch = pr.get("head", {}).get("ref", "")
            try:
                compare_resp = self._request_with_retry(
                    "get",
                    f"{self.base_url}/compare/{head_branch}...{base_branch}",
                )
                compare_resp.raise_for_status()
                compare_data = compare_resp.json()
                behind_by = compare_data.get("ahead_by", 0)  # commits base is ahead of head
                if behind_by == 0:
                    self.logger.info(f"PR #{pr_number} is already up to date")
                    return {"rebased": False, "up_to_date": True}
            except Exception:
                pass  # If compare fails, try the rebase anyway

            # Attempt update-branch
            resp = self._request_with_retry(
                "put",
                f"{self.base_url}/pulls/{pr_number}/update-branch",
                json={"expected_head_sha": head_sha},
            )
            if resp.status_code == 202:
                self.logger.info(f"Successfully rebased PR #{pr_number}")
                return {"rebased": True}
            elif resp.status_code == 422:
                body = resp.json()
                msg = body.get("message", "")
                if "merge conflict" in msg.lower() or "conflict" in msg.lower():
                    self.logger.info(f"PR #{pr_number} has conflicts, cannot rebase")
                    return {"rebased": False, "conflict": True}
                # Could be "already up to date" or similar
                self.logger.info(f"PR #{pr_number} update-branch returned 422: {msg}")
                return {"rebased": False, "up_to_date": True}
            else:
                resp.raise_for_status()
                return {"rebased": False, "error": f"Unexpected status {resp.status_code}"}
        except Exception as e:
            self.logger.error(f"Failed to rebase PR #{pr_number}: {e}")
            return {"rebased": False, "error": str(e)}

    def update_pr_body(self, pr_number: int, body: str) -> None:
        """Update the body/description of a pull request."""
        self.logger.info(f"Updating body of PR #{pr_number}")
        try:
            resp = self._request_with_retry(
                "patch",
                f"{self.base_url}/pulls/{pr_number}",
                json={"body": body},
            )
            resp.raise_for_status()
        except Exception as e:
            self.logger.error(f"Failed to update PR #{pr_number} body: {e}")
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
            resp = self._request_with_retry(
                "get",
                f"{self.base_url}/pulls/{pr_number}/reviews",
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
            resp = self._request_with_retry(
                "get",
                f"{self.base_url}/commits/{sha}/check-runs",
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
