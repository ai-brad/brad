import pytest
from unittest.mock import Mock, patch
from brad.adapters.code_repository.github_adapter import GitHubAdapter
from brad import db
from test_helpers import make_test_config


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    return make_test_config(
        tmp_path,
        github_brad_author_logins=["brad-bot"],
    )


@pytest.fixture
def github_client(mock_config):
    """Create a GitHub adapter instance."""
    return GitHubAdapter(mock_config)


def test_github_client_initialization(github_client):
    """Test GitHub client initialization."""
    assert github_client.base_url == "https://api.github.com/repos/owner/repo"
    assert "token gh-token" in github_client.headers["Authorization"]
    assert github_client.repo == "owner/repo"


def test_github_client_uses_bearer_for_github_app(tmp_path):
    """GitHub App installation tokens should use Bearer auth for REST calls."""
    cfg = make_test_config(
        tmp_path,
        github_token="",
        github_app_id="123",
        github_app_installation_id="456",
        github_app_private_key="-----BEGIN PRIVATE KEY-----\\ntest\\n-----END PRIVATE KEY-----\\n",
    )

    with patch("brad.adapters.code_repository.github_adapter.build_github_token_provider") as mock_build:
        provider = Mock()
        provider.get_headers.return_value = {
            "Authorization": "Bearer installation-token",
            "Accept": "application/vnd.github+json",
        }
        mock_build.return_value = provider

        client = GitHubAdapter(cfg)

    assert client.headers["Authorization"] == "Bearer installation-token"


def test_open_pr_success(github_client):
    """Test opening a PR successfully."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "number": 123,
        "html_url": "https://github.com/owner/repo/pull/123",
        "title": "Test PR",
    }
    mock_response.raise_for_status = Mock()

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.post",
        return_value=mock_response,
    ) as mock_post:
        pr = github_client.open_pr(
            branch="feature-branch", title="Test PR", body="Test body"
        )

        assert pr["number"] == 123
        assert pr["html_url"] == "https://github.com/owner/repo/pull/123"

        # Verify API call
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]
        assert payload["head"] == "feature-branch"
        assert payload["base"] == "main"
        assert "Test PR" in payload["title"]


def test_open_pr_custom_base(github_client):
    """Test opening a PR with custom base branch."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "number": 123,
        "html_url": "https://github.com/owner/repo/pull/123",
    }
    mock_response.raise_for_status = Mock()

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.post",
        return_value=mock_response,
    ) as mock_post:
        github_client.open_pr(
            branch="feature", title="Test", body="Body", base="develop"
        )

        payload = mock_post.call_args.kwargs["json"]
        assert payload["base"] == "develop"


def test_open_pr_failure(github_client):
    """Test handling PR creation failure."""
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = Exception("API Error")

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.post",
        return_value=mock_response,
    ):
        with pytest.raises(Exception):
            github_client.open_pr("branch", "title", "body")


def test_get_pr_success(github_client):
    """Test fetching PR information."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "number": 123,
        "title": "Test PR",
        "state": "open",
    }
    mock_response.raise_for_status = Mock()

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.get",
        return_value=mock_response,
    ):
        pr = github_client.get_pr(123)

        assert pr["number"] == 123
        assert pr["state"] == "open"


def test_pr_exists_for_branch_true(github_client):
    """Test checking for existing PR when it exists."""
    mock_response = Mock()
    mock_response.json.return_value = [
        {"number": 123, "head": {"ref": "feature-branch"}}
    ]
    mock_response.raise_for_status = Mock()

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.get",
        return_value=mock_response,
    ):
        pr_number = github_client.pr_exists_for_branch("feature-branch")

        assert pr_number == 123


def test_pr_exists_for_branch_false(github_client):
    """Test checking for existing PR when it doesn't exist."""
    mock_response = Mock()
    mock_response.json.return_value = []
    mock_response.raise_for_status = Mock()

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.get",
        return_value=mock_response,
    ):
        pr_number = github_client.pr_exists_for_branch("feature-branch")

        assert pr_number is None


def test_pr_exists_for_branch_error(github_client):
    """Test checking for existing PR when API fails."""
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = Exception("API Error")

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.get",
        return_value=mock_response,
    ):
        pr_number = github_client.pr_exists_for_branch("feature-branch")

        # Should return None on error, not raise
        assert pr_number is None


def test_fetch_review_comments(github_client):
    """Test fetching review comments for a PR."""
    mock_response = Mock()
    mock_response.json.return_value = [
        {"id": 1, "body": "Please fix this"},
        {"id": 2, "body": "Looks good"},
    ]
    mock_response.raise_for_status = Mock()

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.get",
        return_value=mock_response,
    ):
        comments = github_client.fetch_review_comments(123)

        assert len(comments) == 2
        assert comments[0]["body"] == "Please fix this"


def test_get_issue_comments_skips_human_comment_after_brad_rebase_notice(github_client):
    """Regression: a Brad-authored rebase notice ('Brad auto-resolved...') sitting
    immediately after a human comment must be recognised as a Brad response and
    NOT cause the human comment to be re-flagged forever (caused 100+ reply
    loops on flaerobotics/bea PR #2355)."""
    issue_comments = [
        # Original human comment
        {
            "id": 1,
            "body": "Tested on dev2 and it works",
            "user": {"login": "human-user"},
            "created_at": "2026-04-29T19:10:31Z",
        },
        # Brad-authored notice that does NOT start with 'Brad reaction:' or 'Brad checking'
        {
            "id": 2,
            "body": "Brad auto-resolved rebase conflicts and force-pushed the rebased branch.",
            "user": {"login": "brad-bot"},
            "created_at": "2026-05-04T08:24:09Z",
        },
        # Brad already responded substantively as well
        {
            "id": 3,
            "body": "Brad reaction: Acknowledged dev2 verification.",
            "user": {"login": "brad-bot"},
            "created_at": "2026-05-04T10:30:00Z",
        },
    ]

    with patch.object(
        github_client, "fetch_issue_comments", return_value=issue_comments
    ):
        needs_response = github_client.get_issue_comments_needing_response(2355)

    assert needs_response == [], (
        "Human comment must be deduped when ANY Brad-authored ('Brad ...') "
        "comment follows it; otherwise Brad re-replies in an infinite loop."
    )


def test_get_issue_comments_flags_truly_unanswered_comment(github_client):
    """A human comment with no following Brad reply still gets flagged."""
    issue_comments = [
        {
            "id": 1,
            "body": "Brad reaction: previously addressed.",
            "user": {"login": "brad-bot"},
            "created_at": "2026-05-01T00:00:00Z",
        },
        {
            "id": 2,
            "body": "Please also handle the empty list case",
            "user": {"login": "human-user"},
            "created_at": "2026-05-02T00:00:00Z",
        },
    ]

    with patch.object(
        github_client, "fetch_issue_comments", return_value=issue_comments
    ):
        needs_response = github_client.get_issue_comments_needing_response(2355)

    assert [c["id"] for c in needs_response] == [2]


def test_get_issue_comments_does_not_trust_human_brad_prefix_when_identity_required(github_client):
    """A human 'Brad checking...' artifact must not suppress the real request."""
    issue_comments = [
        {
            "id": 1,
            "body": "Please make the button match the forward action",
            "user": {"login": "sebastian-dix-flaerobotics-ai"},
            "created_at": "2026-05-14T10:25:15Z",
            "performed_via_github_app": None,
        },
        {
            "id": 2,
            "body": "Brad checking...",
            "user": {"login": "sebastian-dix-flaerobotics-ai"},
            "created_at": "2026-05-14T10:27:58Z",
            "performed_via_github_app": None,
        },
    ]

    with patch.object(
        github_client, "fetch_issue_comments", return_value=issue_comments
    ):
        needs_response = github_client.get_issue_comments_needing_response(2375)

    assert [c["id"] for c in needs_response] == [1]


def test_get_issue_comments_accepts_app_authored_brad_reply(github_client):
    """App-authored comments from this Brad app should count as Brad responses."""
    issue_comments = [
        {
            "id": 1,
            "body": "Please make the button match the forward action",
            "user": {"login": "human-user"},
            "created_at": "2026-05-14T10:25:15Z",
            "performed_via_github_app": None,
        },
        {
            "id": 2,
            "body": "<!-- brad:status -->\n<!-- brad:source-comment-id: 1 -->\n\n> Please make the button match the forward action\n\n---\n\nBrad checking...",
            "user": {"login": "flaero-brad-bot[bot]"},
            "created_at": "2026-05-14T10:27:58Z",
            "performed_via_github_app": {"id": 123},
        },
    ]
    github_client._github_app_id = "123"

    with patch.object(
        github_client, "fetch_issue_comments", return_value=issue_comments
    ):
        needs_response = github_client.get_issue_comments_needing_response(2375)

    assert needs_response == []


def test_reply_to_issue_comment_appends_hidden_marker(github_client):
    """Brad issue comments should carry typed markers and quote the source comment."""
    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = {"id": 123}
    issue_comments = [
        {
            "id": 1,
            "body": "Please make the button match the forward action",
            "user": {"login": "human-user"},
            "created_at": "2026-05-14T10:25:15Z",
            "performed_via_github_app": None,
        }
    ]

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.post",
        return_value=mock_response,
    ) as mock_post, patch.object(
        github_client, "fetch_issue_comments", return_value=issue_comments
    ):
        github_client.reply_to_issue_comment(2375, 1, "Brad checking...")

    body = mock_post.call_args.kwargs["json"]["body"]
    assert body.startswith("<!-- brad:status -->")
    assert "<!-- brad:source-comment-id: 1 -->" in body
    assert "> Please make the button match the forward action" in body
    assert body.rstrip().endswith("Brad checking...")


def test_get_issue_comments_resumes_after_interrupted_checking_reply(github_client):
    """A claim-only issue reply should not suppress follow-up after a restart."""
    issue_comments = [
        {
            "id": 1,
            "body": "Please make the button match the forward action",
            "user": {"login": "human-user"},
            "created_at": "2026-05-14T10:25:15Z",
            "performed_via_github_app": None,
        },
        {
            "id": 2,
            "body": "<!-- brad:status -->\n<!-- brad:source-comment-id: 1 -->\n\n> Please make the button match the forward action\n\n---\n\nBrad checking...",
            "user": {"login": "flaero-brad-bot[bot]"},
            "created_at": "2026-05-14T10:27:58Z",
            "performed_via_github_app": {"id": 123},
        },
    ]
    github_client._github_app_id = "123"

    with patch.object(
        github_client, "fetch_issue_comments", return_value=issue_comments
    ), patch.object(db, "pr_belongs_to_brad", return_value=True), patch.object(
        db, "has_ongoing_work_for_pr", return_value=False
    ):
        needs_response = github_client.get_issue_comments_needing_response(2375)

    assert [c["id"] for c in needs_response] == [1]


def test_quote_markdown_preserves_blank_lines(github_client):
    quoted = github_client.quote_markdown("Line one\n\nLine two")

    assert quoted == "> Line one\n>\n> Line two"


def test_is_brad_comment_uses_machine_marker_and_identity(github_client):
    github_client._github_app_id = "123"
    comment = {
        "id": 9,
        "body": "<!-- brad:review-result -->\n<!-- brad:source-comment-id: 4 -->\n\n> Original\n\n---\n\nBrad reaction: Fixed in latest push.",
        "user": {"login": "flaero-brad-bot[bot]"},
        "performed_via_github_app": {"id": 123},
    }

    assert github_client.get_brad_comment_type(comment["body"]) == "review-result"
    assert github_client._extract_brad_source_comment_id(comment["body"]) == 4
    assert github_client._normalized_brad_body(comment["body"]) == "Brad reaction: Fixed in latest push."
    assert github_client.is_brad_comment(comment) is True


def test_human_comment_with_brad_text_is_not_brad_when_identity_required(github_client):
    comment = {
        "id": 10,
        "body": "Brad checking...",
        "user": {"login": "sebastian-dix-flaerobotics-ai"},
        "performed_via_github_app": None,
    }

    assert github_client.is_brad_comment(comment) is False


def test_reply_to_review_comment_quotes_source(github_client):
    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = {"id": 456}
    review_comments = [
        {
            "id": 42,
            "body": "Please add a regression test here.",
            "user": {"login": "human-user"},
            "created_at": "2026-05-15T08:00:00Z",
        }
    ]

    with patch(
        "brad.adapters.code_repository.github_adapter.requests.post",
        return_value=mock_response,
    ) as mock_post, patch.object(
        github_client, "fetch_review_comments", return_value=review_comments
    ):
        github_client.reply_to_review_comment(2375, 42, "Brad reaction: Fixed in latest push.")

    body = mock_post.call_args.kwargs["json"]["body"]
    assert body.startswith("<!-- brad:review-result -->")
    assert "<!-- brad:source-comment-id: 42 -->" in body
    assert "> Please add a regression test here." in body
    assert body.rstrip().endswith("Brad reaction: Fixed in latest push.")
