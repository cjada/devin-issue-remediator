from datetime import UTC, datetime, timedelta

from tests.helpers import form_encoded, issue_payload, signed_form_body, signed_headers


def test_rejects_invalid_signature(client):
    payload = issue_payload()
    body, headers = signed_headers(payload, "d-bad", secret="wrong-secret")
    response = client.post("/webhooks/github", content=body, headers=headers)
    assert response.status_code == 401


def test_rejects_missing_signature(client):
    body, headers = signed_headers(issue_payload(), "d-nosig")
    del headers["X-Hub-Signature-256"]
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 401


def test_requires_delivery_id(client):
    body, headers = signed_headers(issue_payload(), "d-x")
    del headers["X-GitHub-Delivery"]
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 400


def test_accepts_and_processes_labeled_issue(client):
    body, headers = signed_headers(issue_payload(number=101), "d-101")
    response = client.post("/webhooks/github", content=body, headers=headers)
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"

    listed = client.get("/api/remediations").json()
    row = next(r for r in listed if r["issue_number"] == 101)
    assert row["status"] == "completed"
    assert row["session_id"].startswith("devin-simulated-")
    assert row["pr_url"].startswith("https://github.com/cjada/superset/pull/")
    assert row["acus_consumed"] == 3.5


def test_duplicate_delivery_is_ignored(client):
    body, headers = signed_headers(issue_payload(number=102), "d-102")
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 202
    second = client.post("/webhooks/github", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    listed = client.get("/api/remediations").json()
    assert len([r for r in listed if r["issue_number"] == 102]) == 1


def test_accepts_form_encoded_delivery(client):
    """GitHub's default content type is application/x-www-form-urlencoded."""
    body, headers = signed_form_body(form_encoded(issue_payload(number=108)), "d-108")
    response = client.post("/webhooks/github", content=body, headers=headers)
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"

    listed = client.get("/api/remediations").json()
    assert any(r["issue_number"] == 108 for r in listed)


def test_form_body_without_payload_is_rejected(client):
    body, headers = signed_form_body(b"nope=1", "d-109")
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 400


def test_other_labels_are_ignored(client):
    body, headers = signed_headers(issue_payload(label="bug", number=103), "d-103")
    response = client.post("/webhooks/github", content=body, headers=headers)
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


def test_disallowed_repo_is_ignored(client):
    body, headers = signed_headers(issue_payload(repo="someone/other", number=104), "d-104")
    assert client.post("/webhooks/github", content=body, headers=headers).json()["status"] == "ignored"


def test_non_labeled_action_ignored(client):
    body, headers = signed_headers(issue_payload(action="opened", number=105), "d-105")
    assert client.post("/webhooks/github", content=body, headers=headers).json()["status"] == "ignored"


def test_non_issue_event_ignored(client):
    body, headers = signed_headers(issue_payload(number=106), "d-106")
    headers["X-GitHub-Event"] = "push"
    assert client.post("/webhooks/github", content=body, headers=headers).json()["status"] == "ignored"


def test_dashboard_renders(client):
    body, headers = signed_headers(issue_payload(number=107), "d-107")
    client.post("/webhooks/github", content=body, headers=headers)
    page = client.get("/")
    assert page.status_code == 200
    assert "Devin Issue Remediator" in page.text
    assert "cjada/superset#107" in page.text
    # Scheme-relative so the page is not broken by mixed content behind a TLS proxy.
    assert '<link rel="stylesheet" href="/static/styles.css?v=' in page.text


def test_relative_time_filter(client):
    from app.main import relative_time

    now = datetime.now(UTC)
    assert relative_time(now) == "just now"
    assert relative_time(now - timedelta(minutes=5)) == "5m ago"
    assert relative_time(now - timedelta(hours=3)) == "3h ago"
    assert relative_time((now - timedelta(days=2)).replace(tzinfo=None)) == "2d ago"


def test_humanize_duration(client):
    from app.main import humanize_duration

    assert humanize_duration(9) == "9s"
    assert humanize_duration(65) == "1m 05s"
    assert humanize_duration(4335) == "1h 12m"


def test_running_row_carries_a_ticking_duration(client):
    """In-flight rows expose their start time so the browser can tick the duration."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Remediation, RemediationStatus

    body, headers = signed_headers(issue_payload(number=110), "d-110")
    client.post("/webhooks/github", content=body, headers=headers)

    with Session(engine) as db:
        row = db.exec(select(Remediation).where(Remediation.issue_number == 110)).one()
        row.status = RemediationStatus.RUNNING
        row.finished_at = None
        db.add(row)
        db.commit()

    page = client.get("/").text
    assert 'class="ticking" data-started="' in page
    assert "/static/dashboard.js" in page


def test_dashboard_script_is_served(client):
    response = client.get("/static/dashboard.js")
    assert response.status_code == 200
    assert "setInterval" in response.text


def test_stylesheet_is_served(client):
    response = client.get("/static/styles.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "dry_run": True}
