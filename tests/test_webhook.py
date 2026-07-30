from tests.helpers import issue_payload, signed_headers


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


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "dry_run": True}
