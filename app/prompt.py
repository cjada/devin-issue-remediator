"""Builds the remediation prompt sent to Devin."""

import re

from app.models import Remediation

MAX_BODY_CHARS = 8000

ACCEPTANCE_HEADING = re.compile(r"^\s*#{0,6}\s*\**\s*acceptance criteria\s*\**\s*:?\s*$", re.IGNORECASE)


def extract_acceptance_criteria(body: str) -> str:
    """Return an 'Acceptance Criteria' section from the issue body, if present."""
    lines = body.splitlines()
    collected: list[str] = []
    capturing = False
    for line in lines:
        if ACCEPTANCE_HEADING.match(line):
            capturing = True
            continue
        if capturing:
            stripped = line.strip()
            is_new_heading = line.startswith("#") or (
                stripped.endswith(":") and not stripped.startswith(("-", "*"))
            )
            if is_new_heading:
                break
            collected.append(line)
    return "\n".join(collected).strip()


def build_prompt(remediation: Remediation) -> str:
    body = (remediation.issue_body or "").strip()[:MAX_BODY_CHARS] or "(no description provided)"
    criteria = extract_acceptance_criteria(body) or (
        "No explicit acceptance criteria in the issue. Derive them from the issue "
        "description and state them in the pull request description."
    )
    return f"""You are remediating a GitHub issue in the repository `{remediation.repo_full_name}`.

Issue #{remediation.issue_number}: {remediation.issue_title}
Issue URL: {remediation.issue_url}

## Issue description
{body}

## Acceptance criteria
{criteria}

## Instructions
1. Clone/open `{remediation.repo_full_name}` and reproduce or otherwise verify the reported
   behaviour BEFORE changing any code. If you cannot verify the issue, report that back
   instead of guessing at a fix.
2. Make the smallest focused change that resolves the issue. Do not refactor, reformat,
   upgrade dependencies, or touch files unrelated to this issue.
3. Add or update automated tests that fail before your change and pass after it.
4. Run the relevant validation for the code you touched (targeted tests, linting, type
   checks as applicable to the repository).
5. Open a pull request against the repository's default branch. The PR must reference this
   issue (include "Fixes #{remediation.issue_number}"), explain the root cause, the fix, and
   the validation you ran.
6. Do not modify CI configuration or repository security settings to make checks pass.
"""
