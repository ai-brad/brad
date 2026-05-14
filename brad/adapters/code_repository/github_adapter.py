"""GitHub code repository adapter."""
import time
import requests
from typing import List, Dict, Optional
from brad.adapters.code_repository.base import CodeRepositoryAdapter
from brad.github_auth import build_github_token_provider
from brad.logging_config import get_logger
from brad import db


class GitHubAdapter(CodeRepositoryAdapter):
    """GitHub implementation of the CodeRepositoryAdapter interface."""

    _BRAD_PREFIXES = ("Brad ",)
    _REVIEW_CHECKING_MESSAGE = "Brad reaction: checking..."
    _ISSUE_CHECKING_MESSAGE = "Brad checking..."
    _BRAD_COMMENT_MARKER = "<!-- brad:comment -->"

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.repo = cfg.github_repo
        self.github = build_github_token_provider(cfg)
        self.base_url = f"https://api.github.com/repos/{self.repo}"
        self._max_retries = 5
        self._base_wait = 5  # seconds
        self._github_app_id = str(getattr(cfg, "github_app_id", "") or "") or None
        self._require_brad_author_identity = bool(
            getattr(cfg, "github_require_brad_author_identity", True)
        )
        self._brad_author_logins = {
            login.strip()
            for login in (getattr(cfg, "github_brad_author_logins", []) or [])
            if login and login.strip()
        }
        self.logger.info(f"Initialized GitHub adapter for {self.repo}")

    @property
    def headers(self):
        return self.github.get_headers()

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
            self.logger.exception("Failed to open PR")
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
            self.logger.exception(f"Failed to fetch PR #{pr_number}")
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
            self.logger.exception("Failed to check for existing PR")
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
            self.logger.exception("Failed to fetch Brad PRs")
            return []

    def _comment_body(self, comment: Dict) -> str:
        return (comment.get("body") or "").strip()

    def _comment_author_login(self, comment: Dict) -> str:
        return ((comment.get("user") or {}).get("login") or "").strip()

    def _is_brad_authored_via_app(self, comment: Dict) -> bool:
        via_app = comment.get("performed_via_github_app") or {}
        via_app_id = via_app.get("id")
        return bool(self._github_app_id and via_app_id is not None and str(via_app_id) == self._github_app_id)

    def _is_known_brad_author(self, comment: Dict) -> bool:
        return self._comment_author_login(comment) in self._brad_author_logins

    def _has_brad_signature(self, body: str) -> bool:
        return any(body.startswith(prefix) for prefix in self._BRAD_PREFIXES) or self._BRAD_COMMENT_MARKER in body

    def is_brad_comment(self, comment: Dict) -> bool:
        """Return True when a GitHub comment/review can be trusted as Brad-authored."""
        body = self._comment_body(comment)
        if not self._has_brad_signature(body):
            return False
        if not self._require_brad_author_identity:
            return True
        return self._is_brad_authored_via_app(comment) or self._is_known_brad_author(comment)

    def _format_brad_comment(self, body: str) -> str:
        if self._BRAD_COMMENT_MARKER in body:
            return body
        return f"{body}\n\n{self._BRAD_COMMENT_MARKER}"

    def get_review_comments_needing_response(self, pr_number: int) -> List[Dict]:
        """Get review comments that don't have a Brad response yet.

        A thread needs a response when:
        - It has no Brad reply at all, OR
        - The LAST reply in the thread is NOT from Brad (a human replied after Brad), OR
        - The LAST reply is ONLY "Brad reaction: checking..." AND this PR belongs to Brad AND there's no ongoing work
          (indicating Brad was interrupted and should resume)
        """
        self.logger.debug(f"Checking PR #{pr_number} for unresponded review comments")
        try:
            all_comments = self.fetch_review_comments(pr_number)
            self.logger.info(f"PR #{pr_number}: Found {len(all_comments)} total review comments")

            # Check PR ownership and ongoing work status
            pr_belongs_to_brad = db.pr_belongs_to_brad(pr_number)
            has_ongoing_work = db.has_ongoing_work_for_pr(pr_number)

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
                if self.is_brad_comment(comment):
                    self.logger.debug(f"Comment {comment_id} is Brad's own response - skipping")
                    continue

                # Check the thread conversation
                thread_replies = replies_by_parent.get(comment_id, [])
                if thread_replies:
                    last_reply_body = self._comment_body(thread_replies[-1])
                    brad_spoke_last = self.is_brad_comment(thread_replies[-1])
                    
                    # Detect interrupted work: last reply is ONLY the checking message
                    is_interrupted_work = (
                        last_reply_body == self._REVIEW_CHECKING_MESSAGE and
                        pr_belongs_to_brad and
                        not has_ongoing_work
                    )
                    
                    if is_interrupted_work:
                        self.logger.info(f"Comment {comment_id} — INTERRUPTED WORK detected (checking message, Brad's PR, no ongoing work) — RESUMING")
                        needs_response.append(comment)
                        continue
                    
                    # Detect ongoing conversation: find all Brad substantive responses
                    brad_substantive_indices = []
                    for i, reply in enumerate(thread_replies):
                        reply_body = self._comment_body(reply)
                        if self.is_brad_comment(reply) and reply_body != self._REVIEW_CHECKING_MESSAGE:
                            brad_substantive_indices.append(i)
                    
                    # If Brad has replied substantively, check if there are human replies after the SECOND-TO-LAST Brad response
                    # This catches cases where Brad replied, human objected, and Brad needs to try again
                    if len(brad_substantive_indices) >= 2:
                        second_to_last_brad_idx = brad_substantive_indices[-2]
                        last_brad_idx = brad_substantive_indices[-1]
                        
                        # Check if there's a human reply between the second-to-last and last Brad responses
                        human_replied_between = False
                        for i in range(second_to_last_brad_idx + 1, last_brad_idx):
                            if not self.is_brad_comment(thread_replies[i]):
                                human_replied_between = True
                                break
                        
                        if human_replied_between:
                            # There was a back-and-forth - check if the last Brad response might have missed a rework request
                            # by seeing if it was just a reply without indication of code changes
                            last_brad_body = thread_replies[last_brad_idx].get('body', '')
                            # If the response doesn't mention fixing/changing code, Brad might have misunderstood
                            if not any(keyword in last_brad_body.lower() for keyword in ['fixed', 'changed', 'updated', 'committed', 'push']):
                                self.logger.info(f"Comment {comment_id} — Back-and-forth detected, last Brad response appears to be explanation-only — RE-ENGAGING")
                                needs_response.append(comment)
                                continue
                    
                    # Also check if Brad's LAST response explicitly indicates incomplete work
                    if len(brad_substantive_indices) >= 1:
                        last_brad_idx = brad_substantive_indices[-1]
                        last_brad_body = thread_replies[last_brad_idx].get('body', '')
                        incomplete_phrases = ['have not yet', 'will do', 'need to', 'should', 'plan to', 'intend to']
                        if any(phrase in last_brad_body.lower() for phrase in incomplete_phrases):
                            self.logger.info(f"Comment {comment_id} — Brad's last response indicates INCOMPLETE work — RE-ENGAGING to finish")
                            needs_response.append(comment)
                            continue
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
            self.logger.exception(f"Failed to get review comments for PR #{pr_number}")
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
            self.logger.exception("Failed to get comment replies")
            return []

    def reply_to_review_comment(self, pr_number: int, comment_id: int, body: str) -> Dict:
        """Reply to a review comment in-thread.

        Uses GitHub's dedicated reply endpoint (``POST .../comments/{id}/replies``)
        which is the only reliable way to thread a reply.  The legacy
        ``POST .../comments`` + ``in_reply_to`` form silently creates a detached
        top-level comment when threading fails, which manifests as "replies
        never appear" on the PR.
        """
        self.logger.info(f"Replying to comment {comment_id} on PR #{pr_number}")
        try:
            resp = self._request_with_retry(
                "post",
                f"{self.base_url}/pulls/{pr_number}/comments/{comment_id}/replies",
                json={"body": self._format_brad_comment(body)},
            )
            resp.raise_for_status()
            data = resp.json()
            self.logger.info(
                f"Successfully replied to comment {comment_id} (new id={data.get('id')})"
            )
            return data
        except Exception:
            self.logger.exception(
                f"Failed to reply to comment {comment_id} on PR #{pr_number}"
            )
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
            self.logger.exception(f"Failed to close PR #{pr_number}")
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
            self.logger.exception(f"Failed to rebase PR #{pr_number}")
            return {"rebased": False, "error": str(e)}

    def get_prs_for_commit(self, commit_sha: str) -> List[Dict]:
        """Return the list of PRs that contain ``commit_sha``.

        Used by conflict-context gathering to map a blame SHA back to the PR
        (and from there to the Jira ticket via branch / title). Best-effort:
        returns ``[]`` on any failure rather than raising — the caller logs
        and continues.
        """
        if not commit_sha:
            return []
        try:
            # The "List pull requests associated with a commit" endpoint is in
            # preview; the groot media type is required to enable it.
            headers = self.github.get_headers(
                accept="application/vnd.github.groot-preview+json"
            )
            resp = self._request_with_retry(
                "get",
                f"{self.base_url}/commits/{commit_sha}/pulls",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json() or []
        except Exception as e:
            self.logger.warning(
                f"Could not fetch PRs for commit {commit_sha[:8]}: {e}"
            )
            return []

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
            self.logger.exception(f"Failed to update PR #{pr_number} body")
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
            self.logger.exception(f"Failed to get reviews for PR #{pr_number}")
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
            self.logger.exception(f"Failed to get checks for PR #{pr_number}")
            return []

    def fetch_issue_comments(self, pr_number: int) -> List[Dict]:
        """Fetch all general issue comments on a PR (not review comments)."""
        try:
            return self._fetch_all_pages(f"{self.base_url}/issues/{pr_number}/comments")
        except Exception as e:
            self.logger.exception(f"Failed to fetch issue comments for PR #{pr_number}")
            return []

    def get_issue_comments_needing_response(self, pr_number: int) -> List[Dict]:
        """Get issue comments that haven't been responded to by Brad.
        
        For issue comments (PR-level comments without file/line context),
        we check if there's a Brad response AFTER each human comment.
        """
        self.logger.debug(f"Checking PR #{pr_number} for unresponded issue comments")
        try:
            all_comments = self.fetch_issue_comments(pr_number)
            self.logger.info(f"PR #{pr_number}: Found {len(all_comments)} total issue comments")

            _BOT_SUFFIXES = ('[bot]',)
            
            needs_response = []
            
            # Sort comments by created_at to ensure chronological order
            sorted_comments = sorted(all_comments, key=lambda c: c.get('created_at', ''))
            
            for i, comment in enumerate(sorted_comments):
                comment_id = comment['id']
                comment_author = comment.get('user', {}).get('login', 'unknown')
                comment_body = comment.get('body', '')

                # Skip bot comments
                if any(comment_author.endswith(s) for s in _BOT_SUFFIXES):
                    self.logger.debug(f"Issue comment {comment_id} by bot '{comment_author}' - skipping")
                    continue

                # Skip if comment itself is from Brad
                if self.is_brad_comment(comment):
                    self.logger.debug(f"Issue comment {comment_id} is Brad's own response - skipping")
                    continue

                if comment_body in (self._ISSUE_CHECKING_MESSAGE, self._REVIEW_CHECKING_MESSAGE):
                    self.logger.warning(
                        "Issue comment %s looks like a stale/manual Brad claim message from %s; ignoring it",
                        comment_id,
                        comment_author,
                    )
                    continue

                # Check if there's a Brad response after this comment
                brad_responded = False
                for j in range(i + 1, len(sorted_comments)):
                    next_comment = sorted_comments[j]
                    if self.is_brad_comment(next_comment):
                        brad_responded = True
                        break
                    # If we hit another human comment before a Brad response, stop looking
                    if not self.is_brad_comment(next_comment):
                        break
                
                if not brad_responded:
                    self.logger.info(f"Issue comment {comment_id} by {comment_author} NEEDS response: {comment_body[:100]}...")
                    needs_response.append(comment)
                else:
                    self.logger.debug(f"Issue comment {comment_id} already has Brad response - skipping")

            self.logger.info(f"PR #{pr_number}: {len(needs_response)} issue comments need response")
            return needs_response
        except Exception as e:
            self.logger.exception("Failed to get issue comments needing response")
            return []

    def delete_issue_comment(self, comment_id: int) -> bool:
        """Delete an issue comment by ID."""
        self.logger.info(f"Deleting issue comment {comment_id}")
        try:
            resp = self._request_with_retry(
                "delete",
                f"{self.base_url}/issues/comments/{comment_id}",
            )
            return resp.status_code == 204
        except Exception as e:
            self.logger.exception(f"Failed to delete comment {comment_id}")
            return False

    def reply_to_issue_comment(self, pr_number: int, comment_id: int, body: str) -> Dict:
        """Reply to a specific issue comment (general PR comment)."""
        self.logger.info(f"Replying to issue comment {comment_id} on PR #{pr_number}")
        try:
            payload = {"body": self._format_brad_comment(body)}
            resp = self._request_with_retry(
                "post",
                f"{self.base_url}/issues/{pr_number}/comments",
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self.logger.exception("Failed to reply to issue comment")
            raise

    def get_review_level_comments_needing_response(self, pr_number: int) -> List[Dict]:
        """Get general PR review comments (review body, not line-specific) that need response.
        
        These are comments submitted as part of a PR review but not tied to specific lines.
        Filters out bot reviews (e.g., windsurf-bot, github-actions[bot]).
        """
        self.logger.debug(f"Checking PR #{pr_number} for unresponded review-level comments")
        try:
            resp = self._request_with_retry(
                "get",
                f"{self.base_url}/pulls/{pr_number}/reviews",
            )
            resp.raise_for_status()
            all_reviews = resp.json()
            self.logger.info(f"PR #{pr_number}: Found {len(all_reviews)} total reviews")

            _BOT_SUFFIXES = ('[bot]',)

            needs_response = []

            # Fetch issue comments ONCE for all reviews
            all_issue_comments = self.fetch_issue_comments(pr_number)

            for review in all_reviews:
                review_id = review.get('id')
                reviewer = review.get('user', {}).get('login', 'unknown')
                review_body = review.get('body', '').strip()

                # Skip if no body content
                if not review_body:
                    self.logger.debug(f"Review {review_id} has no body - skipping")
                    continue

                # Skip bot reviews (windsurf-bot[bot], github-actions[bot], etc.)
                if any(reviewer.endswith(s) for s in _BOT_SUFFIXES):
                    self.logger.debug(f"Review {review_id} by bot '{reviewer}' - skipping")
                    continue

                # Skip if review itself is from Brad
                if self.is_brad_comment(review):
                    self.logger.debug(f"Review {review_id} is Brad's own review - skipping")
                    continue

                # Check if there's a Brad response after this review in issue comments
                review_time = review.get('submitted_at', '')

                brad_responded = False
                for comment in all_issue_comments:
                    comment_time = comment.get('created_at', '')
                    if comment_time > review_time and self.is_brad_comment(comment):
                        brad_responded = True
                        break

                if not brad_responded:
                    self.logger.info(f"Review {review_id} by {reviewer} NEEDS response: {review_body[:100]}...")
                    needs_response.append({
                        'id': review_id,
                        'user': {'login': reviewer},
                        'body': review_body,
                        'created_at': review_time,
                        'path': 'N/A',
                        'line': 'N/A',
                        'diff_hunk': '',
                        '_is_review_level': True,
                    })
                else:
                    self.logger.debug(f"Review {review_id} already has Brad response - skipping")

            self.logger.info(f"PR #{pr_number}: {len(needs_response)} review-level comments need response")
            return needs_response
        except Exception as e:
            self.logger.exception("Failed to get review-level comments")
            return []
