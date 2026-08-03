"""SQLModel tables for remediation tracking."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class RemediationStatus(StrEnum):
    QUEUED = "queued"
    SESSION_CREATED = "session_created"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    FAILED = "failed"


ACTIVE_STATUSES = (
    RemediationStatus.QUEUED,
    RemediationStatus.SESSION_CREATED,
    RemediationStatus.RUNNING,
    RemediationStatus.WAITING_FOR_INPUT,
)


class PullRequestState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class WebhookDelivery(SQLModel, table=True):
    """One row per accepted GitHub delivery; the PK enforces idempotency."""

    __tablename__ = "webhook_delivery"

    delivery_id: str = Field(primary_key=True)
    event: str
    received_at: datetime = Field(default_factory=utcnow)


class Remediation(SQLModel, table=True):
    __tablename__ = "remediation"

    id: int | None = Field(default=None, primary_key=True)
    delivery_id: str = Field(index=True)
    repo_full_name: str
    issue_number: int = Field(index=True)
    issue_title: str
    issue_url: str
    issue_body: str = ""
    label: str = ""

    status: RemediationStatus = Field(default=RemediationStatus.QUEUED, index=True)
    dry_run: bool = False

    session_id: str | None = Field(default=None, index=True)
    session_url: str | None = None
    session_status: str | None = None
    session_status_detail: str | None = None

    pr_url: str | None = None
    pr_state: PullRequestState | None = None
    pr_checks: str | None = None
    pr_review_state: str | None = None
    pr_additions: int | None = None
    pr_deletions: int | None = None
    pr_changed_files: int | None = None
    pr_merged_at: datetime | None = None
    pr_checked_at: datetime | None = None

    acus_consumed: float | None = None
    error: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    session_started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.session_started_at is None:
            return None
        end = self.finished_at or utcnow()
        start = self.session_started_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        return (end - start).total_seconds()

    @property
    def diff_summary(self) -> str | None:
        if self.pr_additions is None or self.pr_deletions is None:
            return None
        files = self.pr_changed_files or 0
        return f"+{self.pr_additions} −{self.pr_deletions} · {files} file{'s' if files != 1 else ''}"
