from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings, settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""


def driver_connect_args(app_settings: Settings = settings) -> dict[str, Any]:
    """Driver options for PostgreSQL; MySQL gets none, as before."""
    url = app_settings.database_url
    if url.get_backend_name() != "postgresql":
        return {}
    connect_args: dict[str, Any] = {}
    if "sslmode" not in url.query:
        connect_args["sslmode"] = app_settings.database_ssl_mode
    if app_settings.database_pool_mode == "serverless":
        # Transaction-mode poolers (Supavisor :6543) cannot keep prepared statements.
        connect_args["prepare_threshold"] = None
    return connect_args


def engine_options(app_settings: Settings = settings) -> dict[str, Any]:
    if app_settings.database_pool_mode == "serverless":
        options: dict[str, Any] = {"poolclass": NullPool}
    else:
        options = {
            "pool_pre_ping": True,
            "pool_size": 5,
            "max_overflow": 10,
            "pool_recycle": 1800,
        }
    connect_args = driver_connect_args(app_settings)
    if connect_args:
        options["connect_args"] = connect_args
    return options


engine: Engine = create_engine(settings.database_url, **engine_options())

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


def rollback_failed_transaction(db: Session) -> None:
    """Clear a failed transaction before the request-scoped Session is reused."""
    db.rollback()


@contextmanager
def atomic_transaction(
    db: Session,
    *,
    commit_existing: bool = True,
) -> Generator[None, None, None]:
    """Run atomically, optionally leaving an existing outer transaction open."""
    if not db.in_transaction():
        with db.begin():
            yield
    else:
        try:
            yield
            if commit_existing:
                db.commit()
        except Exception:
            db.rollback()
            raise
