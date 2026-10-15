"""Import the CRM from the isolated MySQL schema into an empty PostgreSQL database.

Connections come only from process environment variables, never from .env or arguments:
  IMPORT_SOURCE_URL  mysql+pymysql://<reader>:<pw>@127.0.0.1:3306/crm_import_source
  IMPORT_TARGET_URL  postgresql://crm_owner:<pw>@<host>:5432/postgres
                     (a direct or session-pooler connection, not the 6543 transaction pooler)
Importing the models still loads the app settings from .env as usual; they are never used
to connect here.

Usage, from the repository root (see docs/postgres_import_runbook.md):
  python scripts/import_mysql_to_postgres.py (--dry-run | --commit) \
      --confirm-source-database crm_import_source \
      --confirm-target-database postgres --confirm-target-host <target host>

The source must be at the Alembic revision this importer stamps, and is read in one
read-only snapshot. The target must have no tables, views or sequences. Schema creation,
row copy, sequence resets, validation and the Alembic stamp run in one PostgreSQL
transaction: --dry-run rolls it back, and so does any failure. Nothing is ever dropped or
altered to recover.

MySQL DATETIME values carry no zone and are imported as UTC; the runbook's timestamp
inspection must confirm that before a --commit run.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import (  # noqa: E402
    JSON,
    Boolean,
    Date,
    DateTime,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Table,
    Text,
    and_,
    bindparam,
    cast,
    create_engine,
    exists,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import URL, Connection, make_url  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import app.models  # noqa: E402,F401  (registers every table)
from app.database.connection import Base  # noqa: E402
from app.database.migration_guard import POSTGRES_BASELINE_REVISION  # noqa: E402
from app.models.tenancy import _LINKS  # noqa: E402

SOURCE_DATABASE = "crm_import_source"
# Never readable as a source, even if a URL names them.
PROTECTED_DATABASES = {
    "ai_sales_crm", "ai_sales_crm_rehearsal", "ai_sales_crm_test",
    "mysql", "sys", "information_schema", "performance_schema",
}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
SOURCE_PORT = 3306  # Another local port may be a tunnel to a remote server.
# Any other URL option (host=, hostaddr=, dbname=, service=, options=, unix_socket=,
# read_default_file=, init_command=, ...) could connect somewhere other than what was confirmed.
SOURCE_QUERY_KEYS = {"charset"}
TARGET_QUERY_KEYS = {"sslmode", "sslrootcert"}
ENCRYPTED_SSL_MODES = {"require", "verify-ca", "verify-full"}
EXPECTED_REVISION = POSTGRES_BASELINE_REVISION
TARGET_OWNER = "crm_owner"
TARGET_SCHEMA = "public"
METADATA = Base.metadata

Rows = dict[tuple, tuple]


class ImportAbort(SystemExit):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Import stopped: {reason}")


# --- Guards that need no connection -------------------------------------------------


def _url(environ: dict[str, str], name: str) -> URL:
    raw = environ.get(name, "").strip()
    if not raw:
        raise ImportAbort(f"{name} is not set in the process environment.")
    try:
        return make_url(raw)
    except Exception:
        # The URL holds the password, so neither it nor the parser's message is shown.
        raise ImportAbort(f"{name} is not a valid database URL.") from None


def resolve_urls(
    environ: dict[str, str], *, source_database: str, target_database: str, target_host: str
) -> tuple[URL, URL]:
    source = _url(environ, "IMPORT_SOURCE_URL")
    target = _url(environ, "IMPORT_TARGET_URL")
    if source.drivername not in {"mysql", "mysql+pymysql"}:
        raise ImportAbort("IMPORT_SOURCE_URL must be a MySQL URL (mysql+pymysql://).")
    if target.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ImportAbort("IMPORT_TARGET_URL must be a PostgreSQL URL (postgresql://).")
    if set(source.query) - SOURCE_QUERY_KEYS:
        raise ImportAbort(f"IMPORT_SOURCE_URL may only set these options: {sorted(SOURCE_QUERY_KEYS)}.")
    if set(target.query) - TARGET_QUERY_KEYS:
        raise ImportAbort(f"IMPORT_TARGET_URL may only set these options: {sorted(TARGET_QUERY_KEYS)}.")
    name = source.database or ""
    if name.lower() in PROTECTED_DATABASES:
        raise ImportAbort(f"{name} is protected; only {SOURCE_DATABASE} may be the source.")
    if name != SOURCE_DATABASE:
        raise ImportAbort(f"the source database must be exactly {SOURCE_DATABASE}.")
    if (source.host or "").lower() not in LOCAL_HOSTS or (source.port or SOURCE_PORT) != SOURCE_PORT:
        raise ImportAbort(
            f"the source must be the local MySQL server (127.0.0.1, localhost or ::1, port {SOURCE_PORT})."
        )
    if source_database != name:
        raise ImportAbort("--confirm-source-database does not match IMPORT_SOURCE_URL.")
    if not target.database:
        raise ImportAbort("IMPORT_TARGET_URL must name a database.")
    if target_database != target.database:
        raise ImportAbort("--confirm-target-database does not match IMPORT_TARGET_URL.")
    # Every Supabase database is named postgres, so the host is what identifies the project.
    if not target.host or target_host.lower() != target.host.lower():
        raise ImportAbort("--confirm-target-host does not match IMPORT_TARGET_URL.")
    sslmode = target.query.get("sslmode", "require")
    if target.host.lower() not in LOCAL_HOSTS and sslmode not in ENCRYPTED_SSL_MODES:
        raise ImportAbort(f"a remote target needs sslmode {sorted(ENCRYPTED_SSL_MODES)}.")
    return source.set(drivername="mysql+pymysql"), target.set(drivername="postgresql+psycopg")


def _alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def check_script_head(heads: list[str]) -> None:
    # The schema comes from the current models, so they must be exactly the stamped revision.
    if heads != [EXPECTED_REVISION]:
        raise ImportAbort(
            f"the Alembic head is {heads}, but this importer builds and stamps "
            f"{EXPECTED_REVISION}; update the importer for the newer migrations first."
        )


def check_revision(versions: list[str]) -> None:
    if versions != [EXPECTED_REVISION]:
        raise ImportAbort(
            f"the source alembic_version is {versions or 'empty'}, expected "
            f"[{EXPECTED_REVISION}]; finish and verify the runbook's migration step first."
        )


def check_source_layout(found: dict[str, set[str]]) -> None:
    expected = {table.name: {c.name for c in table.columns} for table in METADATA.sorted_tables}
    expected["alembic_version"] = {"version_num"}
    if found != expected:
        missing = sorted(expected.keys() - found.keys())
        extra = sorted(found.keys() - expected.keys())
        changed = sorted(t for t in expected.keys() & found.keys() if expected[t] != found[t])
        raise ImportAbort(
            "the source tables do not match the models: "
            f"missing {missing}, unexpected {extra}, different columns in {changed}."
        )


def refuse_non_empty(tables: list[str], views: list[str], sequences: list[str]) -> None:
    if tables or views or sequences:
        raise ImportAbort(
            f"the target schema {TARGET_SCHEMA} already holds {len(tables)} tables, "
            f"{len(views)} views and {len(sequences)} sequences. This importer only fills an "
            "empty database and never drops or changes existing objects."
        )


def describe_error(exc: BaseException) -> str:
    """Error class and codes only: driver messages can echo hosts, users or row values."""
    orig = getattr(exc, "orig", None) or exc
    parts = [type(orig).__name__]
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate:
        parts.append(f"SQLSTATE {sqlstate}")
    diag = getattr(orig, "diag", None)
    for label in ("table_name", "constraint_name"):
        value = getattr(diag, label, None)
        if value:
            parts.append(f"{label.replace('_', ' ')} {value}")
    if getattr(orig, "args", None) and isinstance(orig.args[0], int):
        parts.append(f"MySQL error {orig.args[0]}")
    return ", ".join(parts)


# --- Values -------------------------------------------------------------------------


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(map(_has_nul, value))
    return False


def convert(column: Any, value: Any, where: str) -> Any:
    """One stored value, in the form it is written to and compared on both databases.

    Anything PostgreSQL would store differently stops the import instead of being altered.
    """
    if value is None:
        return None  # SQL NULL, also for JSON columns: JSON null arrives as the text 'null'.
    kind = column.type
    if isinstance(kind, JSON):
        if not isinstance(value, str):
            raise ImportAbort(f"{where}: JSON was not read as text.")
        try:
            parsed = json.loads(value)
        except ValueError:
            raise ImportAbort(f"{where}: the stored JSON is not valid.") from None
        if _has_nul(parsed):
            raise ImportAbort(f"{where}: JSON contains a NUL character (\\u0000).")
        return value  # Exact source text: PostgreSQL json keeps it verbatim.
    if isinstance(kind, Boolean):
        if value not in (0, 1):
            raise ImportAbort(f"{where}: boolean column holds {value!r}, not 0 or 1.")
        return bool(value)
    if isinstance(kind, DateTime):
        if not isinstance(value, datetime):
            raise ImportAbort(f"{where}: not a valid date and time (zero or invalid date).")
        # Naive MySQL DATETIME is UTC (verified by the runbook before importing).
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(kind, Date):
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ImportAbort(f"{where}: not a valid date (zero or invalid date).")
        return value
    if isinstance(kind, Numeric):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ImportAbort(f"{where}: not a finite decimal.")
        return value
    if isinstance(value, str) and "\x00" in value:
        raise ImportAbort(f"{where}: text contains a NUL character, which PostgreSQL rejects.")
    return value


def _select(table: Table):
    def read(column):
        # JSON as text keeps SQL NULL apart from JSON null; booleans as integers expose 2, -1, ...
        if isinstance(column.type, JSON):
            return cast(column, Text).label(column.name)
        if isinstance(column.type, Boolean):
            return cast(column, Integer).label(column.name)
        return column

    return select(*map(read, table.columns))


def read_rows(conn: Connection, table: Table) -> Rows:
    """Rows keyed by primary key: MySQL and PostgreSQL collations sort strings differently."""
    pk_index = [i for i, column in enumerate(table.columns) if column.primary_key]
    rows: Rows = {}
    # shortcut: whole tables in memory (largest is under 100 rows); stream in batches above ~100k rows.
    for raw in conn.execute(_select(table)):
        pk = tuple(raw[i] for i in pk_index)
        rows[pk] = tuple(
            convert(column, value, f"{table.name}.{column.name} at primary key {pk}")
            for column, value in zip(table.columns, raw)
        )
    return rows


def copy_order() -> list[Table]:
    return list(METADATA.sorted_tables)  # Parents before children.


def insert_statement(table: Table):
    """JSON is written as its exact source text, cast, so SQL NULL stays NULL and 'null' stays JSON null."""
    return insert(table).values(
        {
            column.name: cast(bindparam(f"json_{column.name}", type_=Text), column.type)
            for column in table.columns
            if isinstance(column.type, JSON)
        }
    )


def insert_params(table: Table, rows: list[tuple]) -> list[dict[str, Any]]:
    names = [f"json_{c.name}" if isinstance(c.type, JSON) else c.name for c in table.columns]
    return [dict(zip(names, row)) for row in rows]


# --- Source -------------------------------------------------------------------------


def read_source(conn: Connection) -> dict[str, Rows]:
    # One consistent snapshot; READ ONLY makes MySQL reject any write on this session.
    conn.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
    if conn.scalar(text("SELECT DATABASE()")) != SOURCE_DATABASE:
        raise ImportAbort(f"the connected source database is not {SOURCE_DATABASE}.")
    check_revision([row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))])
    layout: dict[str, set[str]] = {}
    for table, column in conn.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE()"
        )
    ):
        layout.setdefault(table, set()).add(column)
    check_source_layout(layout)
    return {table.name: read_rows(conn, table) for table in copy_order()}


# --- Target -------------------------------------------------------------------------


def check_target_session(conn: Connection, database: str) -> None:
    current_database, current_user, current_schema = conn.execute(
        text("SELECT current_database(), current_user, current_schema()")
    ).one()
    if current_database != database:
        raise ImportAbort("the connected target database does not match --confirm-target-database.")
    if current_user != TARGET_OWNER:
        raise ImportAbort(f"connect to the target as {TARGET_OWNER}, so it owns the imported objects.")
    if current_schema != TARGET_SCHEMA:
        raise ImportAbort(f"the target search_path must start with {TARGET_SCHEMA}.")
    inspector = inspect(conn)
    refuse_non_empty(
        inspector.get_table_names(), inspector.get_view_names(), inspector.get_sequence_names()
    )


def _named_checks() -> set[str]:
    return {
        constraint.name
        for table in METADATA.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }


def check_schema(conn: Connection) -> None:
    """Tables, columns, types, nullability, keys and indexes via Alembic; check constraints by name.

    Server defaults are not compared: every row is inserted with explicit values.
    """
    context = MigrationContext.configure(conn, opts={"compare_type": True})
    differences = compare_metadata(context, METADATA)
    if differences:
        raise ImportAbort(
            f"the created schema differs from the models in {len(differences)} place(s): "
            f"{str(differences)[:500]}"
        )
    checks = set(
        conn.scalars(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE contype = 'c' AND connamespace = CAST(current_schema() AS regnamespace)"
            )
        )
    )
    if checks != _named_checks():
        raise ImportAbort(
            "the created check constraints differ from the models: missing "
            f"{sorted(_named_checks() - checks)}, unexpected {sorted(checks - _named_checks())}."
        )


def serial_tables() -> list[Table]:
    return [table for table in copy_order() if table.autoincrement_column is not None]


def _sequence(conn: Connection, table: Table) -> str:
    name = conn.scalar(
        text("SELECT pg_get_serial_sequence(:table, :column)"),
        {"table": table.name, "column": table.autoincrement_column.name},
    )
    if name is None:
        raise ImportAbort(f"{table.name}.{table.autoincrement_column.name} has no sequence.")
    return name


def reset_sequences(conn: Connection) -> None:
    for table in serial_tables():
        top = conn.scalar(select(func.max(table.autoincrement_column)))
        # Empty table: the next ID is 1. Otherwise the next ID follows the largest imported one.
        conn.execute(
            text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
            {"sequence": _sequence(conn, table), "value": max(top or 0, 1), "called": top is not None},
        )


def sequence_is_ahead(last_value: int, is_called: bool, top: int | None) -> bool:
    next_value = last_value + 1 if is_called else last_value
    return top is None or next_value > top


def compare_rows(table: Table, expected: Rows, found: Rows) -> None:
    if len(found) != len(expected):
        raise ImportAbort(f"{table.name}: {len(expected)} source rows, {len(found)} in the target.")
    for pk, row in expected.items():
        other = found.get(pk)
        if other != row:
            column = (
                "the whole row is missing"
                if other is None
                else next(c.name for c, a, b in zip(table.columns, row, other) if a != b)
            )
            raise ImportAbort(f"{table.name} row {pk} differs after the import ({column}).")


def _orphans(conn: Connection, fk: ForeignKeyConstraint) -> int:
    child = fk.table
    present = and_(*(column.is_not(None) for column in fk.columns))
    match = and_(*(c == element.column for c, element in zip(fk.columns, fk.elements)))
    return conn.scalar(select(func.count()).select_from(child).where(present, ~exists().where(match)))


def validate(conn: Connection, source: dict[str, Rows]) -> dict[str, int]:
    counts = {}
    for table in copy_order():
        compare_rows(table, source[table.name], read_rows(conn, table))
        counts[table.name] = len(source[table.name])
        for column in table.columns:
            if not column.nullable and conn.scalar(
                select(func.count()).select_from(table).where(column.is_(None))
            ):
                raise ImportAbort(f"{table.name}.{column.name} has NULLs in a required column.")
        for fk in table.foreign_key_constraints:
            if _orphans(conn, fk):
                raise ImportAbort(f"{table.name}: rows break foreign key {fk.name}.")
    # The nullable cross-record links the tenant guard checks on every write.
    for model, links in _LINKS.items():
        child = model.__table__
        for key, _relationship, target in links:
            parent = target.__table__
            (parent_pk,) = parent.primary_key.columns
            crossed = conn.scalar(
                select(func.count())
                .select_from(child.join(parent, child.c[key] == parent_pk))
                .where(child.c.organization_id != parent.c.organization_id)
            )
            if crossed:
                raise ImportAbort(f"{child.name}.{key} links {crossed} rows across organizations.")
    organizations = METADATA.tables["organizations"]
    locks = METADATA.tables["scheduling_locks"]
    unlocked = conn.scalars(
        select(organizations.c.organization_id).where(
            ~exists().where(locks.c.lock_id == organizations.c.organization_id)
        )
    ).all()
    if unlocked:
        raise ImportAbort(f"organizations {unlocked} have no scheduling lock row.")
    for table in serial_tables():
        last_value, is_called = conn.execute(
            text(f"SELECT last_value, is_called FROM {_sequence(conn, table)}")
        ).one()
        top = conn.scalar(select(func.max(table.autoincrement_column)))
        if not sequence_is_ahead(last_value, is_called, top):
            raise ImportAbort(f"the {table.name} sequence would reissue existing IDs.")
    heads = MigrationContext.configure(conn).get_current_heads()
    if heads != (EXPECTED_REVISION,):
        raise ImportAbort(f"the target alembic_version is {list(heads)}, not {EXPECTED_REVISION}.")
    return counts


def import_into(conn: Connection, source: dict[str, Rows], script: ScriptDirectory) -> dict[str, int]:
    """Every target change, inside the caller's transaction."""
    METADATA.create_all(conn, checkfirst=False)  # Fails rather than reuses an existing table.
    check_schema(conn)
    for table in copy_order():
        rows = list(source[table.name].values())
        if rows:
            conn.execute(insert_statement(table), insert_params(table, rows))
    reset_sequences(conn)
    MigrationContext.configure(conn).stamp(script, EXPECTED_REVISION)
    return validate(conn, source)


def run(source_url: URL, target_url: URL, *, target_database: str, dry_run: bool) -> dict[str, int]:
    script = ScriptDirectory.from_config(_alembic_config())
    check_script_head(script.get_heads())
    connect_args = {} if "sslmode" in target_url.query else {"sslmode": "require"}
    source_engine = create_engine(source_url, poolclass=NullPool)
    target_engine = create_engine(target_url, poolclass=NullPool, connect_args=connect_args)
    try:
        with source_engine.connect() as source_conn:
            source = read_source(source_conn)
            source_conn.rollback()
        with target_engine.connect() as conn:
            transaction = conn.begin()
            try:
                check_target_session(conn, target_database)
                counts = import_into(conn, source, script)
            except BaseException:
                transaction.rollback()
                print("The target transaction was rolled back; nothing was committed.", file=sys.stderr)
                raise
            if dry_run:
                transaction.rollback()
                return counts
            try:
                transaction.commit()
            except BaseException:
                # A COMMIT that fails or is cut off may or may not have been applied.
                print(
                    "COMMIT did not confirm. The target may or may not hold the import: "
                    "inspect it (runbook section 8) before doing anything else.",
                    file=sys.stderr,
                )
                raise
            return counts
    finally:
        source_engine.dispose()
        target_engine.dispose()


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="import, validate, then roll back")
    mode.add_argument("--commit", action="store_true", help="import, validate, then commit")
    parser.add_argument("--confirm-source-database", required=True)
    parser.add_argument("--confirm-target-database", required=True)
    parser.add_argument("--confirm-target-host", required=True)
    args = parser.parse_args(argv)

    source_url, target_url = resolve_urls(
        dict(os.environ) if environ is None else environ,
        source_database=args.confirm_source_database,
        target_database=args.confirm_target_database,
        target_host=args.confirm_target_host,
    )
    print(
        f"Source: MySQL {SOURCE_DATABASE}. Target: PostgreSQL {target_url.database}. "
        f"Mode: {'dry run (rolled back)' if args.dry_run else 'commit'}."
    )
    try:
        counts = run(source_url, target_url, target_database=args.confirm_target_database, dry_run=args.dry_run)
    except ImportAbort:
        raise
    except Exception as exc:
        raise ImportAbort(f"a database operation failed ({describe_error(exc)}).") from None
    for name, count in counts.items():
        print(f"  {name}: {count} rows")
    print(
        "Validated and rolled back (dry run); nothing was committed."
        if args.dry_run
        else f"Validated and committed; the target is stamped {EXPECTED_REVISION}."
    )


if __name__ == "__main__":
    main()
