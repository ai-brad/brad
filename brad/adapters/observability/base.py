"""Abstract base class for observability adapters."""
from abc import ABC, abstractmethod
from typing import Dict, Optional


class ObservabilityAdapter(ABC):
    """
    Abstract interface for observability/log systems (e.g. Azure AKS, Datadog, Grafana).

    To add a new observability adapter:
    1. Create a new file in brad/adapters/observability/
    2. Subclass ObservabilityAdapter and implement all abstract methods
    3. Register it in brad/config.py so it can be selected via configuration
    See CONTRIBUTING.md for detailed instructions.
    """

    @abstractmethod
    def get_environment_diagnostics(self, environment: str, tail_lines: int = 200, since: str = "10m") -> Dict:
        """Fetch diagnostics (logs, pod status, etc.) for an environment."""
        ...

    @abstractmethod
    def format_diagnostics_summary(self, diagnostics: Dict) -> str:
        """Format raw diagnostics into a human-readable summary."""
        ...
