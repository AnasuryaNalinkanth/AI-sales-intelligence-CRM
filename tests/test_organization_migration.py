"""Migration 20261013_0013: organization-scoped CRM records.

The unit tests run everywhere. The MySQL tests are opt-in: set MYSQL_TEST_URL to the
dedicated, disposable ai_sales_crm_test database. They drop every table in it, run the
real migration chain, interrupt 0013 at several steps, resume it, and downgrade it.
"""

from collections.abc import Generator
import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings

ROOT = Path(__file__).resolve().parents[1]
MYSQL_TEST_URL = os.getenv("MYSQL_TEST_URL")
APPROVED_TEST_DATABASE = "ai_sales_crm_test"
requires_mysql = pytest.mark.skipif(
    not MYSQL_TEST_URL, reason="MYSQL_TEST_URL is not set; MySQL migration tests skipped"
)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "migration_0013", ROOT / "alembic" / "versions" / "20261013_0013_organization_scoped_crm.py"
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


# --- unit tests -------------------------------------------------------------------


def test_migration_follows_0012_and_refuses_non_mysql() -> None:
    migration = _load_migration()

    assert (migration.revision, migration.down_revision) == ("20261013_0013", "20261013_0012")
    engine = create_engine("sqlite://")
    with engine.connect() as conn, pytest.raises(migration.MigrationStopped, match="MySQL only"):
        migration.run_steps(conn, through=len(migration.STEPS))


def test_constraint_names_fit_mysql_and_match_the_models() -> None:
    from app.database.connection import Base

    migration = _load_migration()
    names = {*migration.INDEXES, *migration.FOREIGN_KEYS}
    assert all(len(name) <= 64 for name in names)

    model_names: set[str] = set()
    for table in Base.metadata.tables.values():
        model_names |= {c.name for c in table.constraints if c.name}
        model_names |= {i.name for i in table.indexes if i.name}
    assert names <= model_names
    assert migration.OLD_COMPANY_NAME_UNIQUE not in model_names
    # Every organization-scoped model column is migrated.
    scoped = {
        name for name, table in Base.metadata.tables.items()
        if "organization_id" in table.c and name not in {"users", "organizations"}
    }
    assert scoped == set(migration.SCOPED_TABLES)


def test_validators_stop_on_unexpected_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    name = "fk_customers_company_organization"
    good_key = (("organization_id", "company_id"), "companies", ("organization_id", "company_id"), "RESTRICT", 1)

    monkeypatch.setattr(migration, "_foreign_keys", lambda _, __: {name: good_key})
    assert migration._has_foreign_key(None, name) is True
    monkeypatch.setattr(migration, "_foreign_keys", lambda _, __: {name: good_key[:3] + ("CASCADE", 1)})
    with pytest.raises(migration.MigrationStopped):
        migration._has_foreign_key(None, name)
    monkeypatch.setattr(migration, "_foreign_keys", lambda _, __: {name: good_key[:4] + (0,)})
    with pytest.raises(migration.MigrationStopped):
        migration._has_foreign_key(None, name)

    index = "uq_companies_organization_company_name"
    expected = (True, ("organization_id", "company_name"), "YES")
    monkeypatch.setattr(migration, "_indexes", lambda _, __: {index: expected})
    assert migration._has_index(None, index) is True
    for bad in (
        (False, expected[1], "YES"),  # not unique
        (True, ("company_name", "organization_id"), "YES"),  # wrong order
        (True, expected[1], "NO"),  # invisible
    ):
        monkeypatch.setattr(migration, "_indexes", lambda _, __, bad=bad: {index: bad})
        with pytest.raises(migration.MigrationStopped):
            migration._has_index(None, index)


# --- MySQL integration --------------------------------------------------------------


@pytest.fixture
def mysql_conn() -> Generator[Connection, None, None]:
    url = make_url(MYSQL_TEST_URL or "")
    # Every table is dropped below, so never point this at a real database.
    database = (url.database or "").lower()
    if (
        not url.drivername.startswith("mysql")
        or database != APPROVED_TEST_DATABASE
        or database == settings.db_name.lower()
        or set(url.query) - {"charset"}
    ):
        pytest.fail(f"MYSQL_TEST_URL must be a mysql URL naming only {APPROVED_TEST_DATABASE}.")
    engine = create_engine(url)
    with engine.connect() as conn:
        if (conn.scalar(text("SELECT DATABASE()")) or "").lower() != database:
            pytest.fail("Connected database does not match the approved test database.")
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for (table,) in conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        ).all():
            conn.execute(text(f"DROP TABLE `{table}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        conn.commit()
        yield conn
    engine.dispose()


def _alembic(conn: Connection, action: str, revision: str) -> None:
    config = Config()  # No ini file, so the test's logging configuration is left alone.
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["connection"] = conn
    getattr(command, action)(config, revision)
    conn.commit()


def _seed_0012(conn: Connection) -> dict[str, Any]:
    statements = [
        "INSERT INTO companies (company_id, company_name, created_at, updated_at) "
        "VALUES (7, 'Acme', '2026-01-02 03:04:05', '2026-01-02 03:04:05')",
        "INSERT INTO customers (customer_id, company_id, customer_name, status, sales_stage, "
        "created_at, updated_at) VALUES (11, 7, 'Acme Buyer', 'active', 'new', "
        "'2026-01-02 03:04:05', '2026-02-03 04:05:06')",
        "INSERT INTO contacts (contact_id, customer_id, name, is_primary) VALUES (13, 11, 'Ann', 1)",
        "INSERT INTO sales_enquiries (enquiry_id, customer_id, enquiry_text, priority, status) "
        "VALUES (17, 11, 'CRM', 'normal', 'open')",
        "INSERT INTO meetings (meeting_id, customer_id, contact_id, enquiry_id, scheduled_at, "
        "status) VALUES (19, 11, 13, 17, '2030-01-07 10:00:00', 'scheduled')",
        "INSERT INTO calls (call_id, customer_id, status) VALUES (23, 11, 'completed')",
        "INSERT INTO follow_ups (followup_id, customer_id, meeting_id, call_id, type, due_date, "
        "status) VALUES (29, 11, 19, 23, 'email', '2030-01-08 10:00:00', 'pending')",
        "INSERT INTO hitl_policy_overrides (action_type, mode) VALUES ('create_followup', 'disabled')",
        "INSERT INTO hitl_policy_audits (action_type, previous_mode, new_mode) "
        "VALUES ('create_followup', 'approval_required', 'disabled')",
    ]
    for statement in statements:
        conn.execute(text(statement))
    conn.commit()
    return _snapshot(conn)


def _snapshot(conn: Connection) -> dict[str, Any]:
    return {
        "customer": conn.execute(
            text("SELECT customer_id, company_id, customer_name, created_at, updated_at FROM customers")
        ).all(),
        "follow_up": conn.execute(
            text("SELECT followup_id, customer_id, meeting_id, call_id, due_date FROM follow_ups")
        ).all(),
        "meeting": conn.execute(
            text("SELECT meeting_id, contact_id, enquiry_id, scheduled_at FROM meetings")
        ).all(),
        "policy": conn.execute(text("SELECT action_type, mode FROM hitl_policy_overrides")).all(),
    }


def _assert_upgraded(conn: Connection, before: dict[str, Any]) -> None:
    assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "20261013_0013"
    assert _snapshot(conn) == before  # IDs, links and timestamps preserved.
    legacy = conn.scalar(text("SELECT organization_id FROM organizations"))
    for table in _load_migration().SCOPED_TABLES:
        assert conn.scalar(
            text(f"SELECT COUNT(*) FROM `{table}` WHERE organization_id <> :id"), {"id": legacy}
        ) == 0
    assert conn.scalar(text("SELECT COUNT(*) FROM scheduling_locks WHERE lock_id = :id"), {"id": legacy})

    # The composite keys reject a customer in one organization under another's company.
    conn.execute(text("INSERT INTO organizations (name, slug) VALUES ('Other', 'other')"))
    other = conn.scalar(text("SELECT organization_id FROM organizations WHERE slug = 'other'"))
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO customers (organization_id, company_id, customer_name, status, "
                "sales_stage) VALUES (:org, 7, 'Intruder', 'active', 'new')"
            ),
            {"org": other},
        )
    conn.rollback()
    # Company names are unique per organization only.
    conn.execute(text("INSERT INTO organizations (name, slug) VALUES ('Other', 'other')"))
    other = conn.scalar(text("SELECT organization_id FROM organizations WHERE slug = 'other'"))
    conn.execute(
        text("INSERT INTO companies (organization_id, company_name) VALUES (:org, 'Acme')"),
        {"org": other},
    )
    conn.rollback()


@requires_mysql
@pytest.mark.parametrize("stop_after", [2, 4, 5, 7, 8])
def test_interrupted_upgrade_resumes_and_preserves_data(mysql_conn: Connection, stop_after: int) -> None:
    _alembic(mysql_conn, "upgrade", "20261013_0012")
    before = _seed_0012(mysql_conn)

    migration = _load_migration()
    with Operations.context(MigrationContext.configure(mysql_conn)):
        migration.run_steps(mysql_conn, through=stop_after)
    mysql_conn.commit()
    assert mysql_conn.scalar(text("SELECT version_num FROM alembic_version")) == "20261013_0012"

    _alembic(mysql_conn, "upgrade", "20261013_0013")
    _assert_upgraded(mysql_conn, before)


@requires_mysql
def test_downgrade_refuses_a_second_tenant_then_restores_0012(mysql_conn: Connection) -> None:
    _alembic(mysql_conn, "upgrade", "20261013_0012")
    before = _seed_0012(mysql_conn)
    _alembic(mysql_conn, "upgrade", "20261013_0013")

    mysql_conn.execute(text("INSERT INTO organizations (name, slug) VALUES ('Other', 'other')"))
    mysql_conn.commit()
    with pytest.raises(Exception, match="legacy workspace"):
        _alembic(mysql_conn, "downgrade", "20261013_0012")
    mysql_conn.rollback()
    assert mysql_conn.scalar(text("SELECT version_num FROM alembic_version")) == "20261013_0013"

    mysql_conn.execute(text("DELETE FROM organizations WHERE slug = 'other'"))
    mysql_conn.commit()
    _alembic(mysql_conn, "downgrade", "20261013_0012")
    assert _snapshot(mysql_conn) == before
    assert mysql_conn.scalar(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
            "AND index_name = 'uq_companies_company_name'"
        )
    )
    assert not mysql_conn.scalar(
        text(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
            "AND table_name = 'customers' AND column_name = 'organization_id'"
        )
    )
    # Upgrading again from the downgraded state works.
    _alembic(mysql_conn, "upgrade", "20261013_0013")
    _assert_upgraded(mysql_conn, before)
