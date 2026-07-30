"""Core orchestration: webhook intake, Devin session start, status refresh."""

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import Settings
from app.devin_client import CreatedSession, DevinClient, SessionState
from app.github import IssueLabelEvent
from app.github_api import GitHubClient, RateLimited
from app.models import (
    ACTIVE_STATUSES,
    PullRequestState,
    Remediation,
    RemediationStatus,
    WebhookDelivery,
    utcnow,
)
from app.prompt import build_prompt
from app.simulation import SimulatedDevinClient

logger = logging.getLogger(__name__)


class DevinClientProtocol(Protocol):
    def create_session(self, prompt: str, title: str, repo: str) -> CreatedSession: ...

    def get_session(self, session_id: str) -> SessionState: ...


def build_client(settings: Settings) -> DevinClientProtocol:
    if settings.dry_run:
        return SimulatedDevinClient()
    return DevinClient(settings)


class DuplicateDelivery(Exception):
    """Raised when a webhook delivery ID has already been recorded."""


class IgnoredEvent(Exception):
    """Raised when a valid webhook does not warrant remediation."""


@dataclass
class AcceptedDelivery:
    remediation_id: int


def record_delivery(db: Session, delivery_id: str, event: str) -> None:
    db.add(WebhookDelivery(delivery_id=delivery_id, event=event))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DuplicateDelivery(delivery_id) from exc


def accept_event(
    db: Session,
    settings: Settings,
    delivery_id: str,
    event: IssueLabelEvent,
) -> AcceptedDelivery:
    """Validate the event, persist a queued remediation, and return its id."""
    if event.label.lower() != settings.trigger_label.lower():
        raise IgnoredEvent(f"label {event.label!r} is not the trigger label")
    allowed = settings.allowed_repo_set
    if allowed and event.repo_full_name.lower() not in allowed:
        raise IgnoredEvent(f"repository {event.repo_full_name} is not allowed")

    record_delivery(db, delivery_id, "issues.labeled")

    remediation = Remediation(
        delivery_id=delivery_id,
        repo_full_name=event.repo_full_name,
        issue_number=event.issue_number,
        issue_title=event.issue_title,
        issue_url=event.issue_url,
        issue_body=event.issue_body,
        label=event.label,
        dry_run=settings.dry_run,
    )
    db.add(remediation)
    db.commit()
    db.refresh(remediation)
    assert remediation.id is not None
    return AcceptedDelivery(remediation_id=remediation.id)


def start_session(db: Session, client: DevinClientProtocol, remediation_id: int) -> None:
    """Create the Devin session for a queued remediation."""
    remediation = db.get(Remediation, remediation_id)
    if remediation is None or remediation.status is not RemediationStatus.QUEUED:
        return
    try:
        created = client.create_session(
            prompt=build_prompt(remediation),
            title=f"Remediate {remediation.repo_full_name}#{remediation.issue_number}: "
            f"{remediation.issue_title}"[:200],
            repo=remediation.repo_full_name,
        )
    except Exception as exc:  # noqa: BLE001 - persisted for the dashboard
        logger.exception("Failed to create Devin session for remediation %s", remediation_id)
        _fail(db, remediation, str(exc))
        return

    remediation.session_id = created.session_id
    remediation.session_url = created.url
    remediation.status = RemediationStatus.SESSION_CREATED
    remediation.session_started_at = utcnow()
    remediation.updated_at = utcnow()
    db.add(remediation)
    db.commit()


def refresh_remediation(db: Session, client: DevinClientProtocol, remediation: Remediation) -> None:
    if not remediation.session_id:
        return
    try:
        state = client.get_session(remediation.session_id)
    except Exception as exc:  # noqa: BLE001 - persisted for the dashboard
        logger.warning("Failed to refresh session %s: %s", remediation.session_id, exc)
        remediation.error = str(exc)
        remediation.updated_at = utcnow()
        db.add(remediation)
        db.commit()
        return

    remediation.session_status = state.status
    remediation.session_status_detail = state.status_detail
    remediation.acus_consumed = state.acus_consumed
    if state.pr_urls:
        remediation.pr_url = state.pr_urls[0]
    if state.is_terminal:
        remediation.status = RemediationStatus.FAILED if state.failed else RemediationStatus.COMPLETED
        remediation.finished_at = remediation.finished_at or utcnow()
        if state.failed:
            remediation.error = state.status_detail or "Devin session ended in error"
    elif state.awaiting_input:
        remediation.status = RemediationStatus.WAITING_FOR_INPUT
    else:
        remediation.status = RemediationStatus.RUNNING
    remediation.updated_at = utcnow()
    db.add(remediation)
    db.commit()


def refresh_active(db: Session, client: DevinClientProtocol) -> int:
    """Refresh every remediation that still has an in-flight Devin session."""
    statement = select(Remediation).where(
        Remediation.status.in_(ACTIVE_STATUSES),  # type: ignore[attr-defined]
        Remediation.session_id.is_not(None),  # type: ignore[union-attr]
    )
    active = list(db.exec(statement))
    for remediation in active:
        refresh_remediation(db, client, remediation)
    return len(active)


def refresh_pull_request(db: Session, github: GitHubClient, remediation: Remediation) -> None:
    """Record how a Devin-authored pull request actually fared."""
    if not remediation.pr_url:
        return
    try:
        outcome = github.fetch_pull_request(remediation.pr_url)
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 - PR enrichment must never break the poller
        logger.warning("Failed to read %s: %s", remediation.pr_url, exc)
        return
    if outcome is None:
        return

    remediation.pr_state = outcome.state
    remediation.pr_checks = outcome.checks
    remediation.pr_review_state = outcome.review_state
    remediation.pr_additions = outcome.additions
    remediation.pr_deletions = outcome.deletions
    remediation.pr_changed_files = outcome.changed_files
    remediation.pr_merged_at = outcome.merged_at
    remediation.updated_at = utcnow()
    db.add(remediation)
    db.commit()


def refresh_pull_requests(db: Session, github: GitHubClient) -> int:
    """Poll every pull request that has not reached a terminal state."""
    statement = select(Remediation).where(
        Remediation.pr_url.is_not(None),  # type: ignore[union-attr]
        Remediation.dry_run == False,  # noqa: E712 - SQL comparison, not a Python bool
        Remediation.pr_state.not_in(  # type: ignore[attr-defined]
            (PullRequestState.MERGED, PullRequestState.CLOSED)
        )
        | Remediation.pr_state.is_(None),  # type: ignore[union-attr]
    )
    if github.rate_limited:
        logger.info("Skipping pull request poll until %s", github.rate_limited_until)
        return 0

    pending = list(db.exec(statement))
    for index, remediation in enumerate(pending):
        try:
            refresh_pull_request(db, github, remediation)
        except RateLimited as exc:
            logger.warning("%s; %s pull requests left unpolled", exc, len(pending) - index)
            return index
    return len(pending)


def _fail(db: Session, remediation: Remediation, error: str) -> None:
    remediation.status = RemediationStatus.FAILED
    remediation.error = error
    remediation.finished_at = utcnow()
    remediation.updated_at = utcnow()
    db.add(remediation)
    db.commit()
