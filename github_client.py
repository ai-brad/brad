import requests
from typing import Dict, List, Optional
from logging_config import get_logger


class GitHubClient:
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.token = cfg.github_token
        self.repo = cfg.github_repo
        self.base_url = f"https://api.github.com/repos/{self.repo}"
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
        }
        self.logger.info(f"Initialized GitHub client for {self.repo}")

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
        return resp.json()  # each comment dict includes 'body', 'path', etc.

    # -------------------------
    # Resolve review threads
    # -------------------------
    def mark_review_thread_resolved(self, comment_id: int):
        payload = {"body": "Resolved by Brad."}
        # GitHub does not allow direct 'resolve' via comment API; you need
        # to PATCH the review comment to mark resolved
        # Placeholder for implementation if using GraphQL or Checks API
        pass  # For now, we can log as resolved in PR comment

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
            
            for comment in comments:
                comment_id = comment['id']
                replies = self.get_comment_replies(pr_number, comment_id)
                has_brad_response = any(
                    reply.get('body', '').startswith('Brad checking') 
                    for reply in replies
                )
                
                if not has_brad_response and not comment.get('body', '').startswith('Brad checking'):
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
