# Devin Issue Remediator

Event-driven service that turns a `devin-ready` label on a GitHub issue into a Devin session
that verifies the issue, implements a focused fix with tests, and opens a pull request — then
tracks the outcome on a dashboard.

Target repository for this deployment: **`cjada/superset`**.

## Architecture

```
GitHub issue labeled `devin-ready`
        │  webhook (issues.labeled, HMAC-SHA256 signed)
        ▼
FastAPI  /webhooks/github
  1. verify X-Hub-Signature-256          → 401 on mismatch
  2. insert X-GitHub-Delivery into       → duplicate delivery short-circuits (200)
     webhook_delivery (PK = delivery id)
  3. filter: event/action/label/repo     → 202 "ignored" when not a trigger
  4. persist Remediation(queued)         → 202 "accepted" returned immediately
  5. BackgroundTask ──────────────► Devin v3 API: POST /v3/organizations/{org}/sessions
                                          │
Background poller (asyncio, every        ▼
POLL_INTERVAL_SECONDS) ─────────► GET  /v3/organizations/{org}/sessions/{devin_id}
        │                                 status, acus_consumed, pull_requests[]
        ▼
SQLite (SQLModel)  ───────────►  Server-rendered dashboard at `/`
```

| Module | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI routes, background task wiring, dashboard rendering |
| `app/github.py` | Webhook signature verification and `issues.labeled` parsing |
| `app/service.py` | Idempotency, event filtering, session lifecycle, status refresh |
| `app/devin_client.py` | Devin v3 organization API client (service-user bearer auth) |
| `app/simulation.py` | Dry-run client that fabricates a finished session, no network |
| `app/prompt.py` | Builds the remediation prompt (issue + acceptance criteria + rules) |
| `app/models.py` | `Remediation` and `WebhookDelivery` SQLModel tables |

### Key design decisions

- **Idempotency via the database**, not memory: `webhook_delivery.delivery_id` is the primary
  key, so a redelivered webhook fails the insert and is reported as `duplicate`. This survives
  restarts and works for multiple workers on the same SQLite file.
- **Prompt-acknowledge split**: the webhook handler only writes rows and returns `202` within
  milliseconds; the Devin call happens in a FastAPI `BackgroundTask`. A separate asyncio poller
  reconciles in-flight sessions, so status is eventually correct even if the process restarts
  mid-session.
- **Devin v3 organization API with service-user auth** (`Authorization: Bearer <service user
  key>`, org-scoped URLs). Optional `create_as_user_id` attributes sessions to a human user.
- **One client interface, two implementations** (`DevinClient` / `SimulatedDevinClient`) chosen
  by `DRY_RUN`. Real mode refuses to start without `DEVIN_API_KEY` and `DEVIN_ORG_ID`, so it can
  never silently fall back to simulation.
- **Server-rendered Jinja2 dashboard** with a 30s meta-refresh — no frontend build step.

## Setup

Requires Python 3.12.

```bash
cp .env.example .env      # then fill in the values
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000 for the dashboard.

### Configuration

All credentials come from environment variables (`.env` is git-ignored; never commit it).

| Variable | Default | Purpose |
| --- | --- | --- |
| `GITHUB_WEBHOOK_SECRET` | – | Shared secret for `X-Hub-Signature-256` verification (required) |
| `TRIGGER_LABEL` | `devin-ready` | Label that triggers remediation |
| `ALLOWED_REPOS` | `cjada/superset` | Comma-separated allow-list of repositories |
| `DEVIN_API_BASE` | `https://api.devin.ai` | Devin API base URL |
| `DEVIN_API_KEY` | – | Service-user API key (required in real mode) |
| `DEVIN_ORG_ID` | – | Devin organization ID, `org-…` (required in real mode) |
| `DEVIN_CREATE_AS_USER_ID` | unset | Attribute sessions to a human user (needs `ImpersonateOrgSessions`) |
| `DEVIN_MAX_ACU_LIMIT` | unset | Per-session ACU cap |
| `DEVIN_SESSION_TAGS` | `issue-remediator` | Tags applied to created sessions |
| `DRY_RUN` | `true` | `true` simulates Devin; `false` calls the real API |
| `POLL_INTERVAL_SECONDS` | `60` | Session status refresh interval |
| `DATABASE_URL` | `sqlite:///./data/remediator.db` | SQLite location |

## Docker

```bash
cp .env.example .env      # fill in values
docker compose up --build
```

The SQLite file is persisted to `./data` via a bind mount. Health check: `GET /healthz`.

## GitHub webhook configuration

In `cjada/superset` → **Settings → Webhooks → Add webhook**:

- **Payload URL**: `https://<your-host>/webhooks/github`
- **Content type**: `application/json`
- **Secret**: the same value as `GITHUB_WEBHOOK_SECRET`
- **Events**: *Let me select individual events* → **Issues** only
- Create the `devin-ready` label in the repository.

For local testing, expose port 8000 with a tunnel (e.g. `cloudflared tunnel --url
http://localhost:8000`) and use the tunnel URL as the payload URL.

## Devin configuration

1. Create a **service user** in Devin (Settings → Service Users) with a role that allows
   creating and reading organization sessions; add `ImpersonateOrgSessions` only if you want
   `DEVIN_CREATE_AS_USER_ID`.
2. Create an API key for the service user and set `DEVIN_API_KEY`; copy the org ID into
   `DEVIN_ORG_ID`.
3. Give the service user's organization GitHub access to `cjada/superset` so Devin can push
   branches and open pull requests.
4. Optionally set `DEVIN_MAX_ACU_LIMIT` as a cost guardrail.

## Demonstration

Dry run, without touching GitHub or Devin:

```bash
DRY_RUN=true GITHUB_WEBHOOK_SECRET=demo-secret .venv/bin/uvicorn app.main:app &
GITHUB_WEBHOOK_SECRET=demo-secret .venv/bin/python scripts/send_test_webhook.py --issue 1
```

The script signs the payload exactly like GitHub does. The dashboard then shows a completed
remediation with a simulated session and PR link. Re-running with the same `--delivery` value
demonstrates duplicate suppression.

To run for real against `cjada/superset`: set `DRY_RUN=false` plus the Devin credentials,
restart, then label an issue `devin-ready`.

## Testing

```bash
.venv/bin/python -m pytest      # 25 tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Coverage includes signature verification (valid/invalid/missing), delivery idempotency, label
and repository filtering, the full accept → session → PR flow in dry run, Devin API request
shape and response mapping against a mocked transport, failure recording, and dashboard
rendering.

## Limitations

- Single-process SQLite; fine for one instance, not for horizontal scaling.
- Background work lives in-process. A crash between accepting a webhook and creating the
  session leaves a `queued` row that the poller does not retry (there is no retry/back-off yet).
- Status is polled rather than pushed; ACU and PR data are as fresh as the last poll.
- No authentication on the dashboard, and no pagination.
- Only `issues.labeled` is handled; issue edits, unlabeling, and PR review feedback are ignored.
- "Effectiveness" is limited to what the API exposes (status, ACUs, PR presence, duration); it
  does not track whether the PR was merged or whether CI passed.

## Production extensions

- Replace the in-process background work with a real queue (Redis/RQ, Celery, or Postgres
  `SELECT … FOR UPDATE SKIP LOCKED`) with retries, back-off, and a dead-letter path.
- Move to Postgres, add Alembic migrations, and run multiple stateless replicas.
- Post progress back to the issue as comments, and reconcile PR merge/CI outcomes via
  `pull_request` and `check_suite` webhooks for true effectiveness metrics.
- Add auth (SSO/OIDC) to the dashboard, structured logging, and metrics/tracing.
- Enforce per-repo concurrency limits and org-level ACU budgets before creating sessions.
- Store secrets in a manager (AWS Secrets Manager, Vault) instead of a `.env` file.
