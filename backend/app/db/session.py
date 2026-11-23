import os
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..core import config

logger = logging.getLogger("jrfincloud.db.session")

_ENGINE_CACHE: dict[str, Engine] = {}
_SESSION_CACHE: dict[str, sessionmaker] = {}


def normalize_database_url(url: str) -> str:
    cleaned = (url or "").strip()
    if cleaned.startswith("postgres://"):
        return "postgresql+psycopg://" + cleaned[len("postgres://"):]
    if cleaned.startswith("postgresql://"):
        return "postgresql+psycopg://" + cleaned[len("postgresql://"):]
    return cleaned


def get_database_url() -> str:
    env_url = os.getenv("DATABASE_URL", "").strip()
    if env_url:
        return normalize_database_url(env_url)
    if config.DATABASE_URL:
        return normalize_database_url(config.DATABASE_URL)
    if config.BASE_DIR.parent == Path(config.BASE_DIR.anchor):
        default_sqlite = config.BASE_DIR / "data" / "jr_fincloud_v2.db"
    else:
        default_sqlite = config.BASE_DIR.parent / "database" / "jr_fincloud_v2.db"
    return f"sqlite:///{default_sqlite}"


def get_engine() -> Engine:
    url = get_database_url()
    if url not in _ENGINE_CACHE:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        engine = create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            @event.listens_for(engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
        _ENGINE_CACHE[url] = engine
    return _ENGINE_CACHE[url]


def get_sessionmaker() -> sessionmaker:
    url = get_database_url()
    if url not in _SESSION_CACHE:
        _SESSION_CACHE[url] = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _SESSION_CACHE[url]


def reset_engine_cache() -> None:
    for engine in _ENGINE_CACHE.values():
        engine.dispose()
    _ENGINE_CACHE.clear()
    _SESSION_CACHE.clear()


def set_rls_context(db: Session, tenant_id: str) -> None:
    """Define o tenant atual na sessão PostgreSQL para aplicação das políticas de Row-Level Security."""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text("SELECT set_config('app.current_tenant_id', :tid, true)"), {"tid": str(tenant_id)})


@contextmanager
def db_session_scope() -> Generator[Session, None, None]:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Generator[Session, None, None]:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
