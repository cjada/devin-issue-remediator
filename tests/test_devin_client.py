import json

import httpx
import pytest

from app.config import Settings
from app.devin_client import DevinAPIError, DevinClient


def _settings(**overrides) -> Settings:
    base = {
        "DEVIN_API_KEY": "sk-test",
        "DEVIN_ORG_ID": "org-test",
        "DEVIN_API_BASE": "https://api.devin.ai",
        "DEVIN_SESSION_TAGS": "issue-remediator",
        "DRY_RUN": "false",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(handler) -> DevinClient:
    transport = httpx.MockTransport(handler)
    return DevinClient(_settings(), client=httpx.Client(transport=transport))


def test_create_session_posts_v3_org_endpoint():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"session_id": "devin-abc", "url": "https://app.devin.ai/sessions/abc"}
        )

    created = _client(handler).create_session(prompt="do work", title="t", repo="cjada/superset")

    assert seen["url"] == "https://api.devin.ai/v3/organizations/org-test/sessions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["prompt"] == "do work"
    assert seen["body"]["repos"] == ["https://github.com/cjada/superset"]
    assert seen["body"]["tags"] == ["issue-remediator"]
    assert created.session_id == "devin-abc"


def test_get_session_maps_status_and_pr():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "exit",
                "status_detail": "finished",
                "acus_consumed": 12.5,
                "pull_requests": [{"pr_url": "https://github.com/cjada/superset/pull/9", "pr_state": "open"}],
            },
        )

    state = _client(handler).get_session("devin-abc")
    assert state.is_terminal and not state.failed
    assert state.acus_consumed == 12.5
    assert state.pr_urls == ["https://github.com/cjada/superset/pull/9"]


def test_error_status_is_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "acus_consumed": 1, "pull_requests": []})

    state = _client(handler).get_session("devin-abc")
    assert state.failed and state.is_terminal


def test_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    with pytest.raises(DevinAPIError):
        _client(handler).get_session("devin-abc")


def test_real_mode_requires_credentials():
    with pytest.raises(DevinAPIError):
        DevinClient(_settings(DEVIN_API_KEY=""))
