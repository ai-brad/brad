"""Ticketing system adapter (e.g. Jira)."""
from brad.adapters.ticketing.base import TicketingAdapter

def build_ticketing_adapter(cfg) -> TicketingAdapter:
    """Construct the configured ticketing adapter."""
    name = (getattr(cfg, "ticketing_adapter", "") or "jira").strip().lower()
    if name in ("jira", "atlassian"):
        from brad.adapters.ticketing.jira_adapter import JiraAdapter
        return JiraAdapter(cfg)
    if name in ("dummy", "local", "null"):
        from brad.adapters.ticketing.dummy_adapter import DummyTicketingAdapter
        return DummyTicketingAdapter(cfg)
    raise ValueError(
        f"Unknown BRAD_TICKETING={name!r}. Known adapters: 'jira', 'dummy'."
    )


__all__ = ["TicketingAdapter", "build_ticketing_adapter"]
