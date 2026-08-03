"""Dry-run Devin client used for demos and tests; performs no network calls."""

import itertools
import time

from app.devin_client import CreatedSession, SessionState

_counter = itertools.count(1)


class SimulatedDevinClient:
    """Mimics DevinClient but fabricates a session that finishes immediately."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._repo_by_session: dict[str, str] = {}

    def create_session(self, prompt: str, title: str, repo: str) -> CreatedSession:
        session_id = f"devin-simulated-{int(time.time())}-{next(_counter)}"
        self._sessions[session_id] = SessionState(
            status="exit",
            status_detail="finished",
            # Devin reports usage or it does not; inventing a number makes simulated rows
            # look like real ones.
            acus_consumed=None,
            pr_urls=[f"https://github.com/{repo}/pull/0"],
        )
        self._repo_by_session[session_id] = repo
        return CreatedSession(
            session_id=session_id,
            url=f"https://app.devin.ai/sessions/{session_id}",
        )

    def get_session(self, session_id: str) -> SessionState:
        return self._sessions[session_id]
