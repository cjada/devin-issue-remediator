from app.models import Remediation
from app.prompt import build_prompt, extract_acceptance_criteria


def _remediation(body: str = "") -> Remediation:
    return Remediation(
        delivery_id="d-1",
        repo_full_name="cjada/superset",
        issue_number=7,
        issue_title="Broken export",
        issue_url="https://github.com/cjada/superset/issues/7",
        issue_body=body,
    )


def test_extract_acceptance_criteria_from_heading():
    body = "Intro text\n\n## Acceptance Criteria\n- one\n- two\n\n## Notes\nignore me"
    assert extract_acceptance_criteria(body) == "- one\n- two"


def test_extract_returns_empty_when_absent():
    assert extract_acceptance_criteria("just a description") == ""


def test_prompt_contains_repo_issue_and_instructions():
    prompt = build_prompt(_remediation("## Acceptance Criteria\n- exports keep filters"))
    assert "cjada/superset" in prompt
    assert "Issue #7: Broken export" in prompt
    assert "exports keep filters" in prompt
    for expectation in [
        "BEFORE changing any code",
        "smallest focused change",
        "Add or update automated tests",
        "Run the relevant validation",
        "unrelated to this issue",
        "Fixes #7",
    ]:
        assert expectation in prompt


def test_prompt_handles_missing_body_and_criteria():
    prompt = build_prompt(_remediation(""))
    assert "(no description provided)" in prompt
    assert "No explicit acceptance criteria" in prompt
