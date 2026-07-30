"""GitHub webhook signature verification and payload parsing."""

import hashlib
import hmac
from dataclasses import dataclass


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Constant-time check of the X-Hub-Signature-256 header."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@dataclass
class IssueLabelEvent:
    repo_full_name: str
    issue_number: int
    issue_title: str
    issue_url: str
    issue_body: str
    label: str


def parse_issue_label_event(payload: dict) -> IssueLabelEvent | None:
    """Return the event data for an `issues.labeled` payload, else None."""
    if payload.get("action") != "labeled":
        return None
    issue = payload.get("issue")
    repo = payload.get("repository")
    label = (payload.get("label") or {}).get("name")
    if not isinstance(issue, dict) or not isinstance(repo, dict) or not label:
        return None
    number = issue.get("number")
    full_name = repo.get("full_name")
    if not isinstance(number, int) or not isinstance(full_name, str):
        return None
    return IssueLabelEvent(
        repo_full_name=full_name,
        issue_number=number,
        issue_title=issue.get("title") or f"Issue #{number}",
        issue_url=issue.get("html_url") or f"https://github.com/{full_name}/issues/{number}",
        issue_body=issue.get("body") or "",
        label=label,
    )
