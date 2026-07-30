"""Reads the outcome of a Devin-authored pull request from the GitHub API.

The Devin API says whether a session finished; it cannot say whether the fix was
any good. Merge state, CI conclusion, review state and diff size are what turn
"Devin ran" into "the remediation worked".
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.models import PullRequestState

logger = logging.getLogger(__name__)

_PR_URL = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")

# Review states worth surfacing, most significant first.
_REVIEW_PRECEDENCE = ("CHANGES_REQUESTED", "APPROVED", "COMMENTED")


class GitHubAPIError(RuntimeError):
    pass


class RateLimited(GitHubAPIError):
    """Raised when GitHub refuses further requests until a reset time."""

    def __init__(self, reset_at: datetime | None) -> None:
        super().__init__(f"GitHub rate limit exceeded until {reset_at or 'an unknown time'}")
        self.reset_at = reset_at


@dataclass
class PullRequestOutcome:
    state: PullRequestState
    checks: str | None
    review_state: str | None
    additions: int
    deletions: int
    changed_files: int
    merged_at: datetime | None


def parse_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    match = _PR_URL.search(pr_url)
    if not match:
        return None
    return match["owner"], match["repo"], int(match["number"])


def summarize_checks(check_runs: list[dict]) -> str | None:
    """Collapse individual check runs into one word for the dashboard."""
    if not check_runs:
        return None
    conclusions = {run.get("conclusion") for run in check_runs}
    if any(run.get("status") != "completed" for run in check_runs):
        return "pending"
    if conclusions & {"failure", "timed_out", "cancelled", "action_required"}:
        return "failing"
    if conclusions & {"success"}:
        return "passing"
    return "neutral"


def summarize_reviews(reviews: list[dict]) -> str | None:
    states = {review.get("state") for review in reviews}
    for state in _REVIEW_PRECEDENCE:
        if state in states:
            return state.lower().replace("_", " ")
    return None


class GitHubClient:
    """Minimal read-only GitHub client.

    A token is optional: public repositories are readable unauthenticated, at a
    much lower rate limit.
    """

    def __init__(self, token: str = "", client: httpx.Client | None = None) -> None:
        self._token = token
        self._client = client or httpx.Client(timeout=30.0, base_url="https://api.github.com")
        self.rate_limited_until: datetime | None = None

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    @property
    def rate_limited(self) -> bool:
        """True while GitHub has told us to back off."""
        if self.rate_limited_until is None:
            return False
        if datetime.now(UTC) >= self.rate_limited_until:
            self.rate_limited_until = None
            return False
        return True

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str) -> dict | list:
        response = self._client.get(path, headers=self._headers)
        if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            self.rate_limited_until = _reset_time(response.headers.get("x-ratelimit-reset"))
            raise RateLimited(self.rate_limited_until)
        if response.status_code >= 400:
            raise GitHubAPIError(f"GitHub {path} failed ({response.status_code}): {response.text}")
        return response.json()

    def fetch_pull_request(self, pr_url: str) -> PullRequestOutcome | None:
        parsed = parse_pr_url(pr_url)
        if parsed is None:
            return None
        owner, repo, number = parsed

        pull = self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(pull, dict):
            raise GitHubAPIError(f"unexpected pull request payload for {pr_url}")

        if pull.get("merged"):
            state = PullRequestState.MERGED
        elif pull.get("state") == "closed":
            state = PullRequestState.CLOSED
        else:
            state = PullRequestState.OPEN

        # CI and review state only inform a decision that is still open; spending two extra
        # requests per poll on a merged or closed pull request just burns the rate limit.
        checks = None
        reviews: list = []
        if state is PullRequestState.OPEN:
            if sha := (pull.get("head") or {}).get("sha"):
                payload = self._get(f"/repos/{owner}/{repo}/commits/{sha}/check-runs")
                check_runs = payload.get("check_runs") if isinstance(payload, dict) else None
                checks = summarize_checks(check_runs or [])

            fetched = self._get(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
            if isinstance(fetched, list):
                reviews = fetched

        return PullRequestOutcome(
            state=state,
            checks=checks,
            review_state=summarize_reviews(reviews),
            additions=pull.get("additions", 0),
            deletions=pull.get("deletions", 0),
            changed_files=pull.get("changed_files", 0),
            merged_at=_parse_timestamp(pull.get("merged_at")),
        )


def _reset_time(header: str | None) -> datetime | None:
    if not header or not header.isdigit():
        return None
    return datetime.fromtimestamp(int(header), tz=UTC)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
