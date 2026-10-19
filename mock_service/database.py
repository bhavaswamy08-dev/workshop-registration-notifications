"""Database setup for the mock notification service (independent of the main app)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    is_file_db = url.database not in (None, "", ":memory:")
    if is_file_db:
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    if is_file_db:
        raw = engine.raw_connection()
        try:
            raw.cursor().execute("PRAGMA journal_mode=WAL")
        finally:
            raw.close()
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    from mock_service import models  # noqa: F401

    Base.metadata.create_all(engine)


if __name__ == "__main__":
    import os

    url = os.environ.get("MOCK_DATABASE_URL", "sqlite:///./data/mock_notifications.db")
    init_db(build_engine(url))
    print(f"Mock service database initialised: {url}")
