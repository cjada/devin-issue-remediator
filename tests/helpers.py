import hashlib
import hmac
import json
from typing import Any

from tests.conftest import TEST_SECRET


def issue_payload(
    *,
    action: str = "labeled",
    label: str = "devin-ready",
    repo: str = "cjada/superset",
    number: int = 42,
) -> dict[str, Any]:
    return {
        "action": action,
        "label": {"name": label},
        "issue": {
            "number": number,
            "title": "Chart export drops filters",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "body": "Steps to reproduce...\n\nAcceptance Criteria\n- exported chart keeps filters\n",
        },
        "repository": {"full_name": repo},
    }


def signed_headers(
    payload: dict[str, Any], delivery_id: str, secret: str = TEST_SECRET
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery_id,
        "Content-Type": "application/json",
    }
