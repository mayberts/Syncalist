"""SQLite engine and session helpers."""

import uuid
from collections.abc import Iterator

from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .models import AppSettings


def make_engine(db_url: str) -> Engine:
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Tiny migration: add columns introduced after a table was first created.
    New columns are always nullable, so existing rows simply get NULL."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    coltype = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))


def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def get_app_settings(session: Session, default_interval: int = 60) -> AppSettings:
    settings = session.get(AppSettings, 1)
    if settings is None:
        settings = AppSettings(id=1, client_id=f"syncalist-{uuid.uuid4()}", poll_interval=default_interval)
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings
