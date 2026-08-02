---
name: testing-remediator
description: How to run and end-to-end test the Devin Issue Remediator (FastAPI + SQLite webhook → Devin session dashboard) locally in dry-run mode.
---

# Testing the Devin Issue Remediator

## Bring the app up (dry run, no Devin credentials needed)
```bash
cd <repo>
cp .env.example .env   # then set:
#   GITHUB_WEBHOOK_SECRET=demo-secret
#   DRY_RUN=true
#   ALLOWED_REPOS=cjada/superset
#   TRIGGER_LABEL=devin-ready
rm -f data/remediator.db          # start from a clean dashboard
docker compose up --build -d
curl -s localhost:8000/healthz    # {"status":"ok","dry_run":true}
```
Alternative without Docker: `uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"` then
`.venv/bin/uvicorn app.main:app --port 8000`.

Gotcha: port 8000 may already be held by a leftover `uvicorn`, or by a long-lived **live/demo container**
(possibly fronted by `cloudflared tunnel --url http://localhost:8000`) that you must not disturb. Check
`ss -ltnp | grep 8000` and `docker ps` first. To test a branch without touching it, do NOT edit `.env`
(the live container reads it) — run your own uvicorn with env vars inline and a separate DB:
```bash
DATABASE_URL=sqlite:////abs/path/mytest.db GITHUB_WEBHOOK_SECRET=demo-secret DRY_RUN=true \
  ALLOWED_REPOS=cjada/superset TRIGGER_LABEL=devin-ready PR_POLL_INTERVAL_SECONDS=5 \
  setsid nohup .venv/bin/uvicorn app.main:app --port 8010 > /tmp/appA.log 2>&1 < /dev/null &
```
Use `setsid nohup ... < /dev/null &` so the server outlives the shell. Avoid `pkill -f "<pattern>"` when the
pattern also matches your own command line — it kills the shell running it. Match on something narrower, or
look the pid up with `ss -ltnp` first.

`scripts/send_test_webhook.py` accepts `--url http://localhost:8010/webhooks/github` for non-default ports.

## Trigger the feature
```bash
GITHUB_WEBHOOK_SECRET=demo-secret .venv/bin/python scripts/send_test_webhook.py \
  --issue 4242 [--label bug] [--repo someone/other] [--delivery fixed-id]
```
Dry run uses `app/simulation.py`, which finishes instantly: expect status `completed/exit`, ACUs `3.5`,
duration `0s`, and PR URL `https://github.com/<repo>/pull/0`. Those constants are the easiest signal that
dry-run mode is really in use.

For adversarial header cases (bad/absent signature, missing `X-GitHub-Delivery`, non-`issues` event) use curl
with an HMAC computed as `printf '%s' "$BODY" | openssl dgst -sha256 -hmac demo-secret`.
Expected: 401 / 401 / 400 / 202-ignored; duplicate delivery id → 200 `{"status":"duplicate"}`.

## Testing GitHub pull-request polling
`refresh_pull_requests` (app/service.py) only polls rows where **`dry_run = 0`** and `pr_state` is not
MERGED/CLOSED. Simulated dry-run rows are `dry_run=1` with a placeholder `.../pull/0` URL, so PR polling never
fires in pure dry run. To exercise it, seed a row via webhook and then flip it with SQL:
```sql
UPDATE remediation SET dry_run=0, pr_url='https://github.com/<owner>/<repo>/pull/<n>' WHERE id=1;
```
With a real `GITHUB_TOKEN` in the environment this reads the live PR (state, additions/deletions/files, CI,
reviews). `pr_checked_at` moves on every poll; `updated_at` only moves when a field actually changes.

A 404 or malformed PR URL is swallowed per row (`service.py` logs `Failed to read ...`) and does not stop the
rest of the cycle — worth asserting explicitly by keeping one healthy and one broken row side by side.

Rate limiting and open→merged transitions cannot be produced on demand against real GitHub. Run a local stub
of `api.github.com` and point the app at it without touching app code, via a harness module on `PYTHONPATH`:
```python
# stub_harness.py — run with: uvicorn stub_harness:app --port 8020
import httpx, app.main as main
from app.github_api import GitHubClient


class Stubbed(GitHubClient):
    def __init__(self, token="", client=None):
        super().__init__(token, httpx.Client(timeout=10.0, base_url="http://127.0.0.1:9099"))


main.GitHubClient = Stubbed
app = main.app
```
`GitHubClient`'s base URL is hard-coded, so subclass-and-swap is the cheapest injection point. Returning
`403` with `x-ratelimit-remaining: 0` and `x-ratelimit-reset` triggers the back-off; the dashboard then shows a
"pull request status is stale / polling resumes at HH:MM" banner and the merged-% stat degrades to `—`.

## Schema upgrades
New columns are added at startup by `_add_missing_columns()` in `app/db.py` (nullable ALTER TABLE only, no
Alembic). To test backward compatibility, create the DB with the older revision in a **git worktree** so the
main checkout stays on the branch:
```bash
git worktree add /tmp/old <base-commit>
# run /tmp/old on its own port + DB file, seed rows, stop it, then start the branch on that same DB file
```
Compare `PRAGMA table_info(remediation)` column counts before/after and keep a `.bak` copy of the pre-upgrade file.

## UI checks
Dashboard is server-rendered at `/`. The meta refresh interval is **not** hard-coded: it comes from
`PR_POLL_INTERVAL_SECONDS` via the `page_refresh_seconds` template var, so a small value like 5 is a quick way to
confirm the wiring (`curl -s localhost:PORT/ | grep -E 'http-equiv="refresh"|auto-refreshes'`). Press F5 to force.
Stats row = total / active / completed / failed / % completed with PR / ACUs; rows are grouped into
Active, Completed, Failed tables. `GET /api/remediations` gives the raw JSON for cross-checking.
Persistence: `docker compose restart remediator` — data must survive via the `./data` bind mount.

Browser tip: typing into Chrome's omnibox can silently no-op right after page load; use `ctrl+l`, verify with a
screenshot that the text landed, then press Return.

## Forcing a failed remediation (to exercise the Failed section / red pill / error text)
Point the app at real mode with unreachable credentials, send a webhook, then restore dry run:
```bash
# .env: DRY_RUN=false DEVIN_API_KEY=bogus DEVIN_ORG_ID=org-bogus DEVIN_API_BASE=http://localhost:9
docker compose up -d --force-recreate
```
`service.py::_fail` records `status=failed` with the connection error in `error`, no session/PR. Note
`docker compose restart` does NOT pick up `.env` changes — use `up -d --force-recreate`.

A `running`/Active row is hard to observe: the dry-run client finishes instantly, so the Active section and the
pulsing-dot pill can normally only be seen empty. Inserting a row directly into SQLite would be needed to see it.

## Dark mode / responsive checks
The dashboard has no theme toggle; it uses `prefers-color-scheme`. Chrome's Settings → Appearance → Mode → Dark
only re-themes the browser UI, not the page — use DevTools → Ctrl+Shift+P → "Show Rendering" → "Emulate CSS media
feature prefers-color-scheme: dark". The emulation is dropped when DevTools closes, so screenshot with it open.
For narrow width, resize the window with `wmctrl -r :ACTIVE: -e 0,0,0,420,760` (remove maximized flags first).

## Devin Secrets Needed
None for dry-run testing. Real mode would require `DEVIN_API_KEY` and `DEVIN_ORG_ID` (out of scope by default).
