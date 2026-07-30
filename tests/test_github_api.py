from datetime import UTC, datetime

import httpx
import pytest

from app.github_api import (
    GitHubAPIError,
    GitHubClient,
    RateLimited,
    parse_pr_url,
    summarize_checks,
    summarize_reviews,
)
from app.models import PullRequestState


def test_parse_pr_url():
    assert parse_pr_url("https://github.com/cjada/superset/pull/4") == ("cjada", "superset", 4)
    assert parse_pr_url("https://github.com/cjada/superset/issues/4") is None


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], None),
        ([{"status": "completed", "conclusion": "success"}], "passing"),
        (
            [
                {"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "failure"},
            ],
            "failing",
        ),
        (
            [
                {"status": "in_progress", "conclusion": None},
                {"status": "completed", "conclusion": "success"},
            ],
            "pending",
        ),
        ([{"status": "completed", "conclusion": "skipped"}], "neutral"),
    ],
)
def test_summarize_checks(runs, expected):
    assert summarize_checks(runs) == expected


def test_summarize_reviews_prefers_the_blocking_state():
    reviews = [{"state": "APPROVED"}, {"state": "CHANGES_REQUESTED"}, {"state": "COMMENTED"}]
    assert summarize_reviews(reviews) == "changes requested"
    assert summarize_reviews([{"state": "COMMENTED"}]) == "commented"
    assert summarize_reviews([]) is None


def _client(handler) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    return GitHubClient(
        token="t",
        client=httpx.Client(transport=transport, base_url="https://api.github.com"),
    )


def test_fetch_pull_request_reports_checks_reviews_and_diff():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/4"):
            assert request.headers["Authorization"] == "Bearer t"
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "merged": False,
                    "merged_at": None,
                    "additions": 55,
                    "deletions": 4,
                    "changed_files": 2,
                    "head": {"sha": "abc"},
                },
            )
        if "check-runs" in request.url.path:
            return httpx.Response(
                200, json={"check_runs": [{"status": "completed", "conclusion": "success"}]}
            )
        return httpx.Response(200, json=[{"state": "APPROVED"}])

    outcome = _client(handler).fetch_pull_request("https://github.com/cjada/superset/pull/4")
    assert outcome is not None
    assert outcome.state is PullRequestState.OPEN
    assert outcome.checks == "passing"
    assert outcome.review_state == "approved"
    assert (outcome.additions, outcome.deletions, outcome.changed_files) == (55, 4, 2)


def test_closed_without_merge_is_distinct_from_merged():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/9"):
            return httpx.Response(
                200,
                json={
                    "state": "closed",
                    "merged": False,
                    "additions": 1,
                    "deletions": 1,
                    "changed_files": 1,
                    "head": {"sha": "abc"},
                },
            )
        if "check-runs" in request.url.path:
            return httpx.Response(200, json={"check_runs": []})
        return httpx.Response(200, json=[])

    outcome = _client(handler).fetch_pull_request("https://github.com/cjada/superset/pull/9")
    assert outcome is not None
    assert outcome.state is PullRequestState.CLOSED
    assert outcome.checks is None


def test_unparseable_url_is_skipped():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("should not hit the API")

    assert _client(handler).fetch_pull_request("https://example.com/nope") is None


def test_api_errors_are_raised():
    outcome = _client(lambda request: httpx.Response(404, text="nope"))
    with pytest.raises(GitHubAPIError):
        outcome.fetch_pull_request("https://github.com/cjada/superset/pull/4")


def test_token_is_optional():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "state": "open",
                "merged": False,
                "additions": 0,
                "deletions": 0,
                "changed_files": 0,
                "head": {},
            },
        )

    client = GitHubClient(
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    )
    outcome = client.fetch_pull_request("https://github.com/cjada/superset/pull/1")
    assert outcome is not None
    assert outcome.state is PullRequestState.OPEN


def _rate_limited(reset: str | None = "4102444800") -> httpx.Response:
    headers = {"x-ratelimit-remaining": "0"}
    if reset:
        headers["x-ratelimit-reset"] = reset
    return httpx.Response(403, json={"message": "API rate limit exceeded"}, headers=headers)


def test_rate_limit_is_distinguished_from_other_errors():
    client = _client(lambda request: _rate_limited())
    with pytest.raises(RateLimited) as exc:
        client.fetch_pull_request("https://github.com/cjada/superset/pull/4")
    assert exc.value.reset_at == datetime(2100, 1, 1, tzinfo=UTC)
    assert client.rate_limited is True


def test_rate_limit_expires():
    client = _client(lambda request: _rate_limited(reset="1"))
    with pytest.raises(RateLimited):
        client.fetch_pull_request("https://github.com/cjada/superset/pull/4")
    assert client.rate_limited is False


def test_a_plain_403_is_not_a_rate_limit():
    client = _client(lambda request: httpx.Response(403, text="no access"))
    with pytest.raises(GitHubAPIError) as exc:
        client.fetch_pull_request("https://github.com/cjada/superset/pull/4")
    assert not isinstance(exc.value, RateLimited)
    assert client.rate_limited is False


def test_terminal_pull_requests_cost_a_single_request():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "state": "closed",
                "merged": True,
                "merged_at": "2026-07-30T22:15:00Z",
                "additions": 1,
                "deletions": 1,
                "changed_files": 1,
                "head": {"sha": "abc"},
            },
        )

    outcome = _client(handler).fetch_pull_request("https://github.com/cjada/superset/pull/4")
    assert outcome is not None and outcome.state is PullRequestState.MERGED
    assert outcome.merged_at == datetime(2026, 7, 30, 22, 15, tzinfo=UTC)
    assert calls == ["/repos/cjada/superset/pulls/4"]
