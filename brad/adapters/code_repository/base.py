"""Abstract base class for code repository adapters."""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional


class CodeRepositoryAdapter(ABC):
    """
    Abstract interface for code repository platforms (e.g. GitHub, GitLab).

    To add a new code repository adapter:
    1. Create a new file in brad/adapters/code_repository/
    2. Subclass CodeRepositoryAdapter and implement all abstract methods
    3. Register it in brad/config.py so it can be selected via configuration
    See CONTRIBUTING.md for detailed instructions.
    """

    @abstractmethod
    def open_pr(self, branch: str, title: str, body: str, base: str = "main") -> Dict:
        """Open a pull request. Returns PR data dict with at least 'number' and 'html_url'."""
        ...

    @abstractmethod
    def get_pr(self, pr_number: int) -> Dict:
        """Get pull request info by number."""
        ...

    @abstractmethod
    def pr_exists_for_branch(self, branch: str) -> Optional[int]:
        """Check if an open PR exists for the branch. Returns PR number or None."""
        ...

    @abstractmethod
    def fetch_review_comments(self, pr_number: int) -> List[Dict]:
        """Fetch all review comments on a PR."""
        ...

    @abstractmethod
    def get_review_comments_needing_response(self, pr_number: int) -> List[Dict]:
        """Get review comments that haven't been responded to by Brad."""
        ...

    @abstractmethod
    def reply_to_review_comment(self, pr_number: int, comment_id: int, body: str) -> Dict:
        """Reply to a specific review comment."""
        ...

    @abstractmethod
    def get_brad_prs(self) -> List[Dict]:
        """Get all open PRs created by Brad."""
        ...

    @abstractmethod
    def get_comment_replies(self, pr_number: int, comment_id: int) -> List[Dict]:
        """Get replies to a specific review comment."""
        ...

    @abstractmethod
    def close_pr(self, pr_number: int, comment: Optional[str] = None) -> None:
        """Close a pull request, optionally leaving a comment."""
        ...

    @abstractmethod
    def get_pr_details(self, pr_number: int) -> Dict:
        """Get enriched PR details including status, reviews, and check runs."""
        ...

    @abstractmethod
    def get_pr_reviews(self, pr_number: int) -> List[Dict]:
        """Get all reviews on a PR."""
        ...

    @abstractmethod
    def get_pr_checks(self, pr_number: int) -> List[Dict]:
        """Get CI check runs for a PR's head commit."""
        ...
