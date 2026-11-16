"""SQLite engine and session helpers."""

import uuid
from collections.abc import Iterator

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from .models import AppSettings


def make_engine(db_url: str) -> Engine:
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


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
