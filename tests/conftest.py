import os
from collections.abc import Iterator

import pytest

TEST_SECRET = "test-webhook-secret"


@pytest.fixture(scope="session", autouse=True)
def _env(tmp_path_factory: pytest.TempPathFactory) -> None:
    db_file = tmp_path_factory.mktemp("db") / "test.db"
    os.environ.update(
        {
            "GITHUB_WEBHOOK_SECRET": TEST_SECRET,
            "TRIGGER_LABEL": "devin-ready",
            "ALLOWED_REPOS": "cjada/superset",
            "DRY_RUN": "true",
            "DEVIN_ORG_ID": "org-test",
            "DEVIN_API_KEY": "test-key",
            "POLL_INTERVAL_SECONDS": "3600",
            "DATABASE_URL": f"sqlite:///{db_file}",
        }
    )


@pytest.fixture
def client(_env: None) -> Iterator:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(_env: None) -> Iterator:
    from sqlmodel import Session

    from app.db import engine, init_db

    init_db()
    with Session(engine) as session:
        yield session
