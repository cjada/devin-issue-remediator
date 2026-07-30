"""Database engine and session helpers."""

from collections.abc import Iterator
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_settings = get_settings()

if _settings.database_url.startswith("sqlite"):
    db_path = _settings.database_url.split("///")[-1]
    if db_path not in ("", ":memory:"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
