"""Database engine and session helpers."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import inspect, text
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
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Add columns introduced after a database was created.

    The schema only ever grows by nullable columns, so a full migration tool would
    be more machinery than the tracking data justifies.
    """
    inspector = inspect(engine)
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [column for column in table.columns if column.name not in existing]
        with engine.begin() as connection:
            for column in missing:
                type_ = column.type.compile(engine.dialect)
                connection.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {type_}'))


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
