"""Database engine / session setup.

Uses SQLite in WAL mode so concurrent readers never block a writer and a
write transaction serialises access (``busy_timeout`` makes competing
transactions wait instead of failing immediately).
"""
from __future__ import annotations

import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.environ.get("REPORTBUILDER_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "reportbuilder.db"))
DB_URL = os.environ.get("REPORTBUILDER_DB_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - thin glue
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
