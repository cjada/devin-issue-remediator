#!/usr/bin/env python3
"""Send a correctly signed `issues.labeled` webhook to a running remediator.

Usage:
    GITHUB_WEBHOOK_SECRET=... python scripts/send_test_webhook.py \
        --url http://localhost:8000/webhooks/github --repo cjada/superset --issue 1
"""

import argparse
import hashlib
import hmac
import json
import os
import urllib.request
import uuid


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000/webhooks/github")
    parser.add_argument("--repo", default="cjada/superset")
    parser.add_argument("--issue", type=int, default=1)
    parser.add_argument("--title", default="Simulated issue for the remediator demo")
    parser.add_argument("--label", default=os.environ.get("TRIGGER_LABEL", "devin-ready"))
    parser.add_argument("--delivery", default=str(uuid.uuid4()))
    parser.add_argument(
        "--body",
        default="The chart export drops applied filters.\n\n"
        "Acceptance Criteria\n- exported chart preserves the applied filters\n",
    )
    args = parser.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise SystemExit("GITHUB_WEBHOOK_SECRET must be set")

    payload = {
        "action": "labeled",
        "label": {"name": args.label},
        "issue": {
            "number": args.issue,
            "title": args.title,
            "html_url": f"https://github.com/{args.repo}/issues/{args.issue}",
            "body": args.body,
        },
        "repository": {"full_name": args.repo},
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        args.url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": args.delivery,
            "X-Hub-Signature-256": signature,
        },
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - local dev helper
        print(response.status, response.read().decode())


if __name__ == "__main__":
    main()
