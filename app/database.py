"""SQLite engine/session setup.

Key design decision: every transaction starts with ``BEGIN IMMEDIATE``.
SQLite allows a single writer at a time, so taking the write lock at the
start of a transaction makes "check then write" sequences (capacity check,
duplicate check, job claiming) race-free. Other writers simply wait (up to the
busy timeout) instead of failing half-way.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    is_file_db = url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:")
    if is_file_db:
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
        # Disable the driver's implicit transaction handling; we emit BEGIN ourselves.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn) -> None:  # pragma: no cover - trivial
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    if is_file_db:
        # WAL lets readers and the single writer work side by side. The mode is
        # stored in the database file, so it is enough to set it once.
        raw = engine.raw_connection()
        try:
            raw.cursor().execute("PRAGMA journal_mode=WAL")
        finally:
            raw.close()
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after the short transaction ends.
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine(get_settings().database_url)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(get_engine())
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one short-lived session per request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> None:
    """Create all tables (idempotent)."""
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(engine or get_engine())


if __name__ == "__main__":
    init_db()
    print(f"Database initialised: {get_settings().database_url}")
