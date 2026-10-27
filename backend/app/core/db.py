"""Database engine, session factory and schema bootstrap."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.entities import Base, SchemaVersion

SCHEMA_VERSION = "1.1.0"
_engine = None
_SessionLocal: sessionmaker[Session] | None = None
_db_path: Path | None = None


def _make_engine(db_path: Path | None = None):
    path = db_path or settings.db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return eng


def configure(db_path: Path | None = None) -> None:
    global _engine, _SessionLocal, _db_path
    if _engine is not None:
        _engine.dispose()
    _db_path = db_path or settings.db_file
    _engine = _make_engine(_db_path)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)


def get_engine():
    if _engine is None:
        configure()
    return _engine


def get_session() -> Session:
    if _SessionLocal is None:
        configure()
    assert _SessionLocal is not None
    return _SessionLocal()


def _create_triggers() -> None:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS audit_log_no_update "
                "BEFORE UPDATE ON audit_log BEGIN "
                "SELECT RAISE(ABORT, 'audit_log is append-only'); END;"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete "
                "BEFORE DELETE ON audit_log BEGIN "
                "SELECT RAISE(ABORT, 'audit_log is append-only'); END;"
            )
        )


def init_db(db_path: Path | None = None) -> None:
    configure(db_path)
    Base.metadata.create_all(get_engine())
    _create_triggers()
    with get_session() as db:
        row = db.execute(select(SchemaVersion).limit(1)).scalar_one_or_none()
        if row is None:
            db.add(SchemaVersion(id=1, version=SCHEMA_VERSION))
            db.commit()
        elif row.version != SCHEMA_VERSION:
            row.version = SCHEMA_VERSION
            db.commit()


def reset_db(db_path: Path | None = None) -> None:
    path = db_path or settings.db_file
    if _engine is not None:
        _engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    init_db(path)


def get_db():
    db = get_session()
    try:
        yield db
    finally:
        db.close()
