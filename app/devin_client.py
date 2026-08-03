"""Thin client for the Devin organization (v3) API using service-user auth."""

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


class DevinAPIError(RuntimeError):
    pass


@dataclass
class CreatedSession:
    session_id: str
    url: str


@dataclass
class SessionState:
    status: str
    status_detail: str | None
    acus_consumed: float | None
    pr_urls: list[str]

    TERMINAL = ("exit", "error", "suspended")
    AWAITING_INPUT = ("waiting_for_user", "blocked")
    # Devin also idles here after finishing its work, so this detail alone does not mean a
    # human is needed; `blocked` always does.
    IDLE_AFTER_WORK = ("waiting_for_user",)

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL

    @property
    def awaiting_input(self) -> bool:
        """The session is alive but idle until a human replies."""
        return self.status_detail in self.AWAITING_INPUT

    @property
    def blocked(self) -> bool:
        """The session cannot proceed at all without a human."""
        return self.awaiting_input and self.status_detail not in self.IDLE_AFTER_WORK

    @property
    def failed(self) -> bool:
        return self.status == "error"


class DevinClient:
    """Real Devin API client. See https://docs.devin.ai/api-reference/v3/overview."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.devin_api_key or not settings.devin_org_id:
            raise DevinAPIError("DEVIN_API_KEY and DEVIN_ORG_ID are required in real mode")
        self._settings = settings
        self._base = settings.devin_api_base.rstrip("/")
        self._org = settings.devin_org_id
        self._client = client or httpx.Client(timeout=60.0)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.devin_api_key}",
            "Content-Type": "application/json",
        }

    def create_session(self, prompt: str, title: str, repo: str) -> CreatedSession:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "title": title,
            "tags": self._settings.session_tag_list,
            "repos": [f"https://github.com/{repo}"],
        }
        if self._settings.devin_create_as_user_id:
            payload["create_as_user_id"] = self._settings.devin_create_as_user_id
        if self._settings.devin_max_acu_limit:
            payload["max_acu_limit"] = self._settings.devin_max_acu_limit

        response = self._client.post(
            f"{self._base}/v3/organizations/{self._org}/sessions",
            json=payload,
            headers=self._headers,
        )
        data = _json_or_raise(response, "create session")
        session_id = data.get("session_id")
        if not session_id:
            raise DevinAPIError(f"Devin API returned no session_id: {data}")
        return CreatedSession(session_id=session_id, url=data.get("url", ""))

    def get_session(self, session_id: str) -> SessionState:
        response = self._client.get(
            f"{self._base}/v3/organizations/{self._org}/sessions/{session_id}",
            headers=self._headers,
        )
        data = _json_or_raise(response, "get session")
        pull_requests = data.get("pull_requests") or []
        return SessionState(
            status=data.get("status", "unknown"),
            status_detail=data.get("status_detail"),
            acus_consumed=data.get("acus_consumed"),
            pr_urls=[pr["pr_url"] for pr in pull_requests if pr.get("pr_url")],
        )


def _json_or_raise(response: httpx.Response, action: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise DevinAPIError(f"Devin API {action} failed ({response.status_code}): {response.text}")
    return response.json()
