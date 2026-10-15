import logging

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import Settings, settings
from app.core.logging_utils import log_database_exception, redact_sensitive_text
from app.database.connection import engine_options
from app.models import FollowUp
from app.services.analytics_service import _period_expression

MYSQL_FIELDS = {
    "db_host": "mysql.local",
    "db_port": 3307,
    "db_name": "crm",
    "db_user": "crm_app",
    "db_password": "mysql-pw",
}


def test_mysql_url_is_unchanged_when_database_url_is_unset_or_blank() -> None:
    for extra in ({}, {"DATABASE_URL": "   "}):
        url = Settings(_env_file=None, **MYSQL_FIELDS, **extra).database_url
        assert url.drivername == "mysql+pymysql"
        assert (url.host, url.port, url.database, url.username, url.password) == (
            "mysql.local", 3307, "crm", "crm_app", "mysql-pw"
        )


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_database_url_takes_precedence_and_uses_psycopg(scheme: str) -> None:
    url = Settings(
        _env_file=None,
        **MYSQL_FIELDS,
        DATABASE_URL=f"{scheme}://pg_user:pg-pw@db.example.com:6543/postgres",
    ).database_url
    assert url.drivername == "postgresql+psycopg"
    assert (url.host, url.port, url.database, url.username) == (
        "db.example.com", 6543, "postgres", "pg_user"
    )
    assert "pg-pw" not in repr(url)


def test_mysql_pool_options_are_unchanged() -> None:
    assert engine_options(Settings(_env_file=None, **MYSQL_FIELDS)) == {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 1800,
    }


def test_serverless_postgres_uses_null_pool_ssl_and_no_prepared_statements() -> None:
    options = engine_options(
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://u:p@db.example.com:6543/postgres",
            database_pool_mode="serverless",
        )
    )
    assert options == {
        "poolclass": NullPool,
        "connect_args": {"sslmode": "require", "prepare_threshold": None},
    }


def test_sslmode_in_url_is_not_overridden() -> None:
    options = engine_options(
        Settings(_env_file=None, DATABASE_URL="postgresql://u:p@localhost/crm?sslmode=disable")
    )
    assert "connect_args" not in options
    assert options["pool_size"] == 5


def _compiled_period(url: str, grain: str) -> str:
    db = Session(bind=create_engine(url))
    statement = select(_period_expression(FollowUp.due_date, grain, db))
    return str(statement.compile(db.get_bind(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize(
    ("grain", "pg_format"),
    [("daily", "YYYY-MM-DD"), ("weekly", 'IYYY-"W"IW'), ("monthly", "YYYY-MM")],
)
def test_postgres_groups_by_utc_period_label(grain: str, pg_format: str) -> None:
    sql = _compiled_period("postgresql+psycopg://", grain)
    assert f"to_char(timezone('UTC', follow_ups.due_date), '{pg_format}')" in sql


def test_mysql_period_grouping_is_unchanged() -> None:
    db = Session(bind=create_engine("mysql+pymysql://"))
    statement = select(_period_expression(FollowUp.due_date, "weekly", db))
    sql = str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "date_format(follow_ups.due_date, '%%Y-W%%u')" in sql


class _Diag:
    constraint_name = "uq_customers_company_customer_name"
    column_name = None
    table_name = "customers"


class _PgUniqueViolation(Exception):
    sqlstate = "23505"
    diag = _Diag()


def _log_integrity(orig: Exception, caplog: pytest.LogCaptureFixture) -> str:
    exc = IntegrityError("INSERT INTO customers VALUES ('Secret Co')", {}, orig)
    with caplog.at_level("ERROR", logger="test.db"):
        log_database_exception(
            logging.getLogger("test.db"),
            "create_customer",
            exc,
            include_exception_details=False,
            include_integrity_diagnostics=True,
        )
    return caplog.text


def test_postgres_integrity_diagnostics_use_sqlstate(caplog: pytest.LogCaptureFixture) -> None:
    text = _log_integrity(_PgUniqueViolation("Key (name)=(Secret Co) already exists"), caplog)
    assert "mysql_error_code=None" in text
    assert "constraint_or_column=uq_customers_company_customer_name" in text
    assert "child_table=customers" in text
    assert "sqlstate=23505]" in text
    assert "Secret Co" not in text


def test_mysql_integrity_diagnostics_are_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    orig = Exception(1062, "Duplicate entry 'Secret Co' for key 'customers.uq_customer_name'")
    text = _log_integrity(orig, caplog)
    assert "mysql_error_code=1062 constraint_or_column=uq_customer_name" in text
    assert "sqlstate=None]" in text
    assert "Secret Co" not in text


def test_database_url_password_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "database_url_override",
        SecretStr("postgresql://u:url-only-pw@db.example.com/postgres"),
    )
    assert "url-only-pw" not in redact_sensitive_text("connect failed using url-only-pw")
