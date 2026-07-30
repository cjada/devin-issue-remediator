"""FastAPI application: GitHub webhook intake plus a server-rendered dashboard."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.db import engine, get_session, init_db
from app.github import parse_issue_label_event, verify_signature
from app.github_api import GitHubClient
from app.models import ACTIVE_STATUSES, PullRequestState, Remediation, RemediationStatus
from app.service import (
    DevinClientProtocol,
    DuplicateDelivery,
    IgnoredEvent,
    accept_event,
    build_client,
    refresh_active,
    refresh_pull_requests,
    start_session,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ASSETS = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(ASSETS / "templates"))


def relative_time(value: datetime) -> str:
    """Render a timestamp as a compact age, e.g. `4m ago`."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - value).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def humanize_duration(seconds: float) -> str:
    """Render a duration as `42s`, `5m 03s` or `1h 12m`."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def iso_utc(value: datetime) -> str:
    """Serialise a timestamp for the browser, assuming naive values are UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


TEMPLATES.env.filters["relative_time"] = relative_time
TEMPLATES.env.filters["humanize_duration"] = humanize_duration
TEMPLATES.env.filters["iso_utc"] = iso_utc

# Cache-busting token so restyled assets are picked up without a hard refresh.
ASSET_VERSION = str(int(max(path.stat().st_mtime for path in (ASSETS / "static").iterdir())))


async def _poller(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(settings.poll_interval_seconds)
        try:
            await asyncio.to_thread(_refresh_once, app)
        except Exception:  # noqa: BLE001 - the poller must never die
            logger.exception("Status poll failed")


def _refresh_once(app: FastAPI) -> None:
    with Session(engine) as db:
        refresh_active(db, app.state.devin_client)
        refresh_pull_requests(db, app.state.github_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    init_db()
    app.state.devin_client = build_client(settings)
    app.state.github_client = GitHubClient(settings.github_token)
    logger.info("Started in %s mode", "DRY_RUN" if settings.dry_run else "REAL")
    task = asyncio.create_task(_poller(app))
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Devin Issue Remediator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ASSETS / "static")), name="static")


def get_client(request: Request) -> DevinClientProtocol:
    return request.app.state.devin_client


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


@app.get("/healthz")
def healthz(settings: Settings = Depends(get_app_settings)) -> dict[str, object]:
    return {"status": "ok", "dry_run": settings.dry_run}


@app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    client: DevinClientProtocol = Depends(get_client),
) -> JSONResponse:
    raw_body = await request.body()

    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(raw_body, signature, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery")
    if not delivery_id:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery header")
    if event != "issues":
        return JSONResponse({"status": "ignored", "reason": f"event {event!r}"}, status_code=202)

    try:
        payload = _decode_payload(raw_body, request.headers.get("content-type", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    parsed = parse_issue_label_event(payload)
    if parsed is None:
        return JSONResponse({"status": "ignored", "reason": "not an issues.labeled event"}, status_code=202)

    try:
        accepted = accept_event(db, settings, delivery_id, parsed)
    except DuplicateDelivery:
        return JSONResponse({"status": "duplicate", "delivery_id": delivery_id}, status_code=200)
    except IgnoredEvent as exc:
        return JSONResponse({"status": "ignored", "reason": str(exc)}, status_code=202)

    background_tasks.add_task(_process, accepted.remediation_id, client)
    return JSONResponse(
        {"status": "accepted", "remediation_id": accepted.remediation_id, "dry_run": settings.dry_run},
        status_code=202,
    )


def _decode_payload(raw_body: bytes, content_type: str) -> dict:
    """Decode a webhook body sent as JSON or as GitHub's default form encoding."""
    if content_type.startswith("application/x-www-form-urlencoded"):
        fields = parse_qs(raw_body.decode())
        if "payload" not in fields:
            raise ValueError("form body without a payload field")
        return json.loads(fields["payload"][0])
    return json.loads(raw_body)


def _process(remediation_id: int, client: DevinClientProtocol) -> None:
    """Runs outside the request/response cycle: create the session, then poll once."""
    with Session(engine) as db:
        start_session(db, client, remediation_id)
        remediation = db.get(Remediation, remediation_id)
        if remediation is not None and remediation.session_id:
            from app.service import refresh_remediation

            refresh_remediation(db, client, remediation)


def get_github_client(request: Request) -> GitHubClient:
    return request.app.state.github_client


@app.post("/api/refresh")
def refresh_now(
    db: Session = Depends(get_session),
    client: DevinClientProtocol = Depends(get_client),
    github: GitHubClient = Depends(get_github_client),
) -> dict[str, int]:
    return {
        "refreshed": refresh_active(db, client),
        "pull_requests": refresh_pull_requests(db, github),
    }


@app.get("/api/remediations")
def list_remediations(db: Session = Depends(get_session)) -> list[Remediation]:
    return list(db.exec(select(Remediation).order_by(Remediation.id.desc())))  # type: ignore[union-attr]


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    github: GitHubClient = Depends(get_github_client),
) -> HTMLResponse:
    remediations = list(db.exec(select(Remediation).order_by(Remediation.id.desc())))  # type: ignore[union-attr]
    waiting = [r for r in remediations if r.status is RemediationStatus.WAITING_FOR_INPUT]
    active = [
        r
        for r in remediations
        if r.status in ACTIVE_STATUSES and r.status is not RemediationStatus.WAITING_FOR_INPUT
    ]
    completed = [r for r in remediations if r.status is RemediationStatus.COMPLETED]
    failed = [r for r in remediations if r.status is RemediationStatus.FAILED]
    with_pr = [r for r in completed if r.pr_url]
    merged = [r for r in remediations if r.pr_state is PullRequestState.MERGED]
    tracked_prs = [r for r in remediations if r.pr_state is not None]
    total_acus = sum(r.acus_consumed or 0 for r in remediations)
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "dry_run": settings.dry_run,
            "trigger_label": settings.trigger_label,
            "repos": sorted(settings.allowed_repo_set),
            "asset_version": ASSET_VERSION,
            "generated_at": datetime.now(UTC),
            "active": active,
            "waiting": waiting,
            "completed": completed,
            "failed": failed,
            "total": len(remediations),
            "pr_rate": (100 * len(with_pr) / len(completed)) if completed else None,
            "merged": len(merged),
            "merge_rate": (100 * len(merged) / len(tracked_prs)) if tracked_prs else None,
            "total_acus": round(total_acus, 2),
            "pr_polling_paused_until": github.rate_limited_until if github.rate_limited else None,
            "github_authenticated": github.authenticated,
        },
    )
