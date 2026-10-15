"""Model metadata must match the schema built by the Alembic history (offline checks only)."""

import pytest
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateTable

from app.database.connection import Base
import app.models  # noqa: F401  (registers every table)

# name -> (table, column, referenced "table.column", ondelete), as created by migration 0001.
HISTORICAL_FOREIGN_KEYS = {
    "fk_customers_company_id_companies": ("customers", "company_id", "companies.company_id", "RESTRICT"),
    "fk_contacts_customer_id_customers": ("contacts", "customer_id", "customers.customer_id", "RESTRICT"),
    "fk_sales_enquiries_customer_id_customers": (
        "sales_enquiries", "customer_id", "customers.customer_id", "RESTRICT",
    ),
    "fk_meetings_customer_id_customers": ("meetings", "customer_id", "customers.customer_id", "RESTRICT"),
    "fk_meetings_contact_id_contacts": ("meetings", "contact_id", "contacts.contact_id", "SET NULL"),
    "fk_meetings_enquiry_id_sales_enquiries": (
        "meetings", "enquiry_id", "sales_enquiries.enquiry_id", "SET NULL",
    ),
    "fk_calls_customer_id_customers": ("calls", "customer_id", "customers.customer_id", "RESTRICT"),
    "fk_calls_contact_id_contacts": ("calls", "contact_id", "contacts.contact_id", "SET NULL"),
    "fk_calls_enquiry_id_sales_enquiries": (
        "calls", "enquiry_id", "sales_enquiries.enquiry_id", "SET NULL",
    ),
    "fk_follow_ups_customer_id_customers": ("follow_ups", "customer_id", "customers.customer_id", "RESTRICT"),
    "fk_follow_ups_enquiry_id_sales_enquiries": (
        "follow_ups", "enquiry_id", "sales_enquiries.enquiry_id", "SET NULL",
    ),
    "fk_follow_ups_meeting_id_meetings": ("follow_ups", "meeting_id", "meetings.meeting_id", "SET NULL"),
    "fk_follow_ups_call_id_calls": ("follow_ups", "call_id", "calls.call_id", "SET NULL"),
}


def _foreign_keys_by_name():
    return {
        fk.name: fk
        for table in Base.metadata.tables.values()
        for fk in table.foreign_key_constraints
        if fk.name
    }


@pytest.mark.parametrize("name", sorted(HISTORICAL_FOREIGN_KEYS))
def test_historical_foreign_key_names(name: str) -> None:
    table, column, target, ondelete = HISTORICAL_FOREIGN_KEYS[name]
    fk = _foreign_keys_by_name()[name]
    assert fk.parent.name == table
    assert [c.name for c in fk.columns] == [column]
    assert [element.target_fullname for element in fk.elements] == [target]
    assert fk.ondelete == ondelete


def test_constraint_names_fit_postgres_limit() -> None:
    names = [
        item.name
        for table in Base.metadata.tables.values()
        for item in (*table.constraints, *table.indexes)
        if item.name
    ]
    assert max(map(len, names)) <= 63


def test_relationships_still_configure() -> None:
    configure_mappers()


@pytest.mark.parametrize(
    ("table", "column", "default"),
    [
        ("google_calendar_connections", "reminder_minutes", "10"),
        ("calendar_event_links", "attempts", "0"),
    ],
)
def test_calendar_server_defaults_match_migration_0010(
    table: str, column: str, default: str
) -> None:
    col = Base.metadata.tables[table].c[column]
    assert col.server_default.arg == default
    # The Python-side default the app already relies on is unchanged.
    assert col.default.arg == int(default)
    for dialect in (mysql.dialect(), postgresql.dialect()):
        ddl = str(CreateTable(col.table).compile(dialect=dialect))
        line = next(line for line in ddl.splitlines() if line.strip().startswith(f"{column} "))
        assert f"DEFAULT '{default}'" in line and "NOT NULL" in line


@pytest.mark.parametrize("dialect", [mysql.dialect(), postgresql.dialect()], ids=["mysql", "postgresql"])
def test_ddl_uses_historical_foreign_key_names(dialect) -> None:
    ddl = "\n".join(
        str(CreateTable(table).compile(dialect=dialect)) for table in Base.metadata.sorted_tables
    )
    for name in HISTORICAL_FOREIGN_KEYS:
        assert f"CONSTRAINT {name} FOREIGN KEY" in ddl
