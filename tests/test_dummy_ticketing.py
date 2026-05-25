from pathlib import Path

from brad.adapters.ticketing.dummy_adapter import DummyTicketingAdapter
from test_helpers import make_test_config


def test_dummy_ticketing_round_trip(tmp_path):
    cfg = make_test_config(
        tmp_path,
        ticketing_adapter="dummy",
        dummy_ticket_path=str(Path(__file__).parent / "fixtures" / "dummy_ticket.txt"),
        dummy_ticket_log_path=str(tmp_path / "dummy_ticket.log"),
    )
    adapter = DummyTicketingAdapter(cfg)

    issues = adapter.fetch_issues_with_label("BradReview")
    assert len(issues) == 1
    issue = issues[0]
    assert issue["key"] == "DUMMY-42"
    assert issue["fields"]["summary"] == "Summarization smoke ticket"

    fetched = adapter.fetch_issue("DUMMY-42")
    assert fetched is not None
    assert fetched["fields"]["status"]["name"] == "Open"

    adapter.comment("DUMMY-42", "Hello from the smoke test")
    adapter.set_status("DUMMY-42", "In Progress")
    adapter.add_label("DUMMY-42", "smoke")
    adapter.remove_label("DUMMY-42", "dummy")

    fetched = adapter.fetch_issue("DUMMY-42")
    assert fetched["fields"]["status"]["name"] == "In Progress"
    assert "smoke" in fetched["fields"]["labels"]
    assert "dummy" not in fetched["fields"]["labels"]

    log_text = Path(cfg.dummy_ticket_log_path).read_text(encoding="utf-8")
    assert "fetch_issues_with_label" in log_text
    assert "comment" in log_text
    assert "set_status" in log_text
