"""Abstract base class for ticketing system adapters."""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional


class TicketingAdapter(ABC):
    """
    Abstract interface for ticketing systems (e.g. Jira, Linear, Azure DevOps).

    To add a new ticketing adapter:
    1. Create a new file in brad/adapters/ticketing/
    2. Subclass TicketingAdapter and implement all abstract methods
    3. Register it in brad/config.py so it can be selected via configuration
    See CONTRIBUTING.md for detailed instructions.
    """

    @abstractmethod
    def fetch_issues_with_label(self, label: str) -> List[Dict]:
        """Fetch issues that have the given label. Returns list of issue dicts."""
        ...

    @abstractmethod
    def remove_label(self, issue_key: str, label: str) -> None:
        """Remove a label from an issue."""
        ...

    @abstractmethod
    def add_label(self, issue_key: str, label: str) -> None:
        """Add a label to an issue."""
        ...

    @abstractmethod
    def comment(self, issue_key: str, text: str) -> None:
        """Post a comment on an issue."""
        ...

    @abstractmethod
    def set_status(self, issue_key: str, target_status: str) -> None:
        """Transition an issue to a new status."""
        ...

    @abstractmethod
    def download_attachments(self, issue_key: str, attachments: List[Dict]) -> List[str]:
        """Download attachments and return list of local file paths."""
        ...
