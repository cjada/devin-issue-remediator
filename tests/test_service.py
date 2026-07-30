import pytest

from app.config import get_settings
from app.devin_client import CreatedSession, DevinAPIError, SessionState
from app.github import IssueLabelEvent
from app.models import Remediation, RemediationStatus
from app.service import (
    DuplicateDelivery,
    IgnoredEvent,
    accept_event,
    build_client,
    record_delivery,
    refresh_active,
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
