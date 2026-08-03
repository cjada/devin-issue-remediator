import pytest

from app.config import get_settings
from app.devin_client import CreatedSession, DevinAPIError, SessionState
from app.github import IssueLabelEvent
from app.github_api import GitHubAPIError, PullRequestOutcome, RateLimited
from app.models import (
    ACTIVE_STATUSES,
    PullRequestState,
    Remediation,
    RemediationStatus,
    utcnow,
)
from app.service import (
    DuplicateDelivery,
    IgnoredEvent,
    accept_event,
    build_client,
    record_delivery,
    refresh_active,
    refresh_pull_requests,
    start_session,
)
from app.simulation import SimulatedDevinClient


def _event(number: int) -> IssueLabelEvent:
    return IssueLabelEvent(
        repo_full_name="cjada/superset",
        issue_number=number,
        issue_title="Broken export",
        issue_url="https://github.com/cjada/superset/issues/1",
        issue_body="body",
        label="devin-ready",
    )


class FailingClient:
    def create_session(self, prompt: str, title: str, repo: str) -> CreatedSession:
        raise DevinAPIError("boom")

    def get_session(self, session_id: str) -> SessionState:  # pragma: no cover - unused
        raise AssertionError


class RunningClient(SimulatedDevinClient):
    def create_session(self, prompt: str, title: str, repo: str) -> CreatedSession:
        created = super().create_session(prompt, title, repo)
        self._sessions[created.session_id] = SessionState(
            status="running", status_detail="working", acus_consumed=1.0, pr_urls=[]
        )
        return created


def test_dry_run_builds_simulated_client():
    assert isinstance(build_client(get_settings()), SimulatedDevinClient)


def test_duplicate_delivery_raises(db):
    record_delivery(db, "dup-1", "issues.labeled")
    with pytest.raises(DuplicateDelivery):
        record_delivery(db, "dup-1", "issues.labeled")


def test_accept_event_rejects_wrong_label(db):
    event = _event(200)
    event.label = "bug"
    with pytest.raises(IgnoredEvent):
        accept_event(db, get_settings(), "svc-200", event)


def test_failed_session_creation_is_recorded(db):
    accepted = accept_event(db, get_settings(), "svc-201", _event(201))
    start_session(db, FailingClient(), accepted.remediation_id)
    remediation = db.get(Remediation, accepted.remediation_id)
    assert remediation.status is RemediationStatus.FAILED
    assert "boom" in remediation.error
    assert remediation.finished_at is not None


def test_running_session_then_refresh_to_completed(db):
    client = RunningClient()
    accepted = accept_event(db, get_settings(), "svc-202", _event(202))
    start_session(db, client, accepted.remediation_id)
    remediation = db.get(Remediation, accepted.remediation_id)
    assert remediation.status is RemediationStatus.SESSION_CREATED

    assert refresh_active(db, client) >= 1
    db.refresh(remediation)
    assert remediation.status is RemediationStatus.RUNNING
    assert remediation.duration_seconds is not None

    client._sessions[remediation.session_id] = SessionState(
        status="exit", status_detail="finished", acus_consumed=4.0, pr_urls=["https://x/pull/3"]
    )
    refresh_active(db, client)
    db.refresh(remediation)
    assert remediation.status is RemediationStatus.COMPLETED
    assert remediation.pr_url == "https://x/pull/3"
    assert remediation.acus_consumed == 4.0


class WaitingClient(SimulatedDevinClient):
    """A live session that has gone idle pending a human reply."""

    def create_session(self, prompt: str, title: str, repo: str) -> CreatedSession:
        created = super().create_session(prompt, title, repo)
        self._sessions[created.session_id] = SessionState(
            status="running",
            status_detail="waiting_for_user",
            acus_consumed=2.0,
            pr_urls=["https://github.com/cjada/superset/pull/4"],
        )
        return created


def test_waiting_for_user_is_its_own_status(db):
    client = WaitingClient()
    accepted = accept_event(db, get_settings(), "svc-203", _event(203))
    start_session(db, client, accepted.remediation_id)
    refresh_active(db, client)

    remediation = db.get(Remediation, accepted.remediation_id)
    db.refresh(remediation)
    assert remediation.status is RemediationStatus.WAITING_FOR_INPUT
    assert remediation.session_status_detail == "waiting_for_user"
    # Still in flight, so the poller keeps picking it up.
    assert remediation.status in ACTIVE_STATUSES
    assert remediation.finished_at is None


class StubGitHub:
    def __init__(self, outcome=None, error: Exception | None = None) -> None:
        self.outcome = outcome
        self.error = error
        self.calls: list[str] = []
        self.rate_limited = False
        self.rate_limited_until = None

    def fetch_pull_request(self, pr_url: str):
        self.calls.append(pr_url)
        if self.error:
            raise self.error
        return self.outcome


def _outcome(state: PullRequestState) -> PullRequestOutcome:
    outcome = _merged_outcome()
    outcome.state = state
    return outcome


def _merged_outcome() -> PullRequestOutcome:
    return PullRequestOutcome(
        state=PullRequestState.MERGED,
        checks="passing",
        review_state="approved",
        additions=55,
        deletions=4,
        changed_files=2,
        merged_at=utcnow(),
    )


def test_pull_request_outcome_is_recorded(db):
    client = WaitingClient()
    accepted = accept_event(db, get_settings(), "svc-204", _event(204))
    start_session(db, client, accepted.remediation_id)
    refresh_active(db, client)
    remediation = db.get(Remediation, accepted.remediation_id)
    remediation.dry_run = False
    db.add(remediation)
    db.commit()

    github = StubGitHub(_merged_outcome())
    assert refresh_pull_requests(db, github) == 1
    db.refresh(remediation)
    assert remediation.pr_state is PullRequestState.MERGED
    assert remediation.pr_checks == "passing"
    assert remediation.pr_review_state == "approved"
    assert remediation.diff_summary == "+55 −4 · 2 files"

    # Terminal pull requests are not polled again.
    assert refresh_pull_requests(db, github) == 0


def test_pull_request_errors_do_not_break_the_poller(db):
    client = WaitingClient()
    accepted = accept_event(db, get_settings(), "svc-205", _event(205))
    start_session(db, client, accepted.remediation_id)
    refresh_active(db, client)
    remediation = db.get(Remediation, accepted.remediation_id)
    remediation.dry_run = False
    db.add(remediation)
    db.commit()

    github = StubGitHub(error=GitHubAPIError("rate limited"))
    assert refresh_pull_requests(db, github) == 1
    db.refresh(remediation)
    assert remediation.pr_state is None


def test_simulated_rows_are_never_polled(db):
    client = WaitingClient()
    accepted = accept_event(db, get_settings(), "svc-206", _event(206))
    start_session(db, client, accepted.remediation_id)
    refresh_active(db, client)

    github = StubGitHub(_merged_outcome())
    refresh_pull_requests(db, github)
    assert "pull/4" not in "".join(url for url in github.calls if str(accepted.remediation_id) in url)
    remediation = db.get(Remediation, accepted.remediation_id)
    assert remediation.dry_run is True
    assert remediation.pr_state is None


def _pr_row(db, delivery: str, number: int):
    client = WaitingClient()
    accepted = accept_event(db, get_settings(), delivery, _event(number))
    start_session(db, client, accepted.remediation_id)
    refresh_active(db, client)
    remediation = db.get(Remediation, accepted.remediation_id)
    remediation.dry_run = False
    db.add(remediation)
    db.commit()
    return remediation


def test_a_closed_pull_request_replaces_the_open_state(db):
    remediation = _pr_row(db, "svc-207", 207)
    refresh_pull_requests(db, StubGitHub(_outcome(PullRequestState.OPEN)))
    db.refresh(remediation)
    assert remediation.pr_state is PullRequestState.OPEN

    closed = PullRequestOutcome(
        state=PullRequestState.CLOSED,
        checks=None,
        review_state=None,
        additions=55,
        deletions=4,
        changed_files=2,
        merged_at=None,
    )
    assert refresh_pull_requests(db, StubGitHub(closed)) == 1
    db.refresh(remediation)
    assert remediation.pr_state is PullRequestState.CLOSED


def test_rate_limiting_stops_the_poll_without_losing_rows(db):
    _pr_row(db, "svc-208", 208)
    _pr_row(db, "svc-209", 209)

    limited = StubGitHub(error=RateLimited(None))
    assert refresh_pull_requests(db, limited) == 0
    # The first row is attempted, the rest are left for the next poll.
    assert len(limited.calls) == 1


def test_a_rate_limited_client_is_not_polled_at_all(db):
    _pr_row(db, "svc-210", 210)
    github = StubGitHub(_merged_outcome())
    github.rate_limited = True
    github.rate_limited_until = utcnow()

    assert refresh_pull_requests(db, github) == 0
    assert github.calls == []


def test_unchanged_pull_request_polls_do_not_bump_updated_at(db):
    client = WaitingClient()
    accepted = accept_event(db, get_settings(), "svc-311", _event(311))
    start_session(db, client, accepted.remediation_id)
    refresh_active(db, client)
    remediation = db.get(Remediation, accepted.remediation_id)
    remediation.dry_run = False
    db.add(remediation)
    db.commit()

    github = StubGitHub(_outcome(PullRequestState.OPEN))
    refresh_pull_requests(db, github)
    db.refresh(remediation)
    first_update = remediation.updated_at
    first_check = remediation.pr_checked_at

    refresh_pull_requests(db, github)
    db.refresh(remediation)
    assert remediation.updated_at == first_update
    assert remediation.pr_checked_at > first_check

    github.outcome = _merged_outcome()
    refresh_pull_requests(db, github)
    db.refresh(remediation)
    assert remediation.pr_state is PullRequestState.MERGED
    assert remediation.updated_at > first_update
