"""scripts/import_mysql_to_postgres.py and the Alembic PostgreSQL guard.

Nothing here connects to MySQL or PostgreSQL, except the opt-in tests at the end, which
need PG_TEST_URL. The guard, conversion and validation helpers are tested directly; the
transaction handling with fake engines; the Alembic wiring by running alembic/env.py on
in-memory SQLite, which shows only that env.py consults the guard before any migration:
SQLite does not stand in for PostgreSQL's schema, so the schema, sequence and JSON
behaviour is covered only by the PostgreSQL tests.
"""

import importlib.util
import io
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.dialects.postgresql import psycopg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.database import migration_guard
from app.database.connection import Base

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "import_mysql_to_postgres", ROOT / "scripts" / "import_mysql_to_postgres.py"
)
assert spec is not None and spec.loader is not None
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)

ImportAbort = importer.ImportAbort
TABLES = Base.metadata.tables
SOURCE_SECRET = "src-secret-pw"
TARGET_SECRET = "tgt-secret-pw"
SOURCE = f"mysql+pymysql://reader:{SOURCE_SECRET}@127.0.0.1:3306/crm_import_source"
TARGET = f"postgresql://crm_owner:{TARGET_SECRET}@db.staging.example.com:5432/postgres"
CONFIRM = {
    "source_database": "crm_import_source",
    "target_database": "postgres",
    "target_host": "db.staging.example.com",
}


def _resolve(source: str | None = SOURCE, target: str | None = TARGET, **confirm: str):
    environ = {
        key: value
        for key, value in (("IMPORT_SOURCE_URL", source), ("IMPORT_TARGET_URL", target))
        if value is not None
    }
    return importer.resolve_urls(environ, **{**CONFIRM, **confirm})


# --- Dialect, confirmation and protected-name guards ---------------------------------


def test_accepts_the_import_source_and_a_confirmed_target() -> None:
    source, target = _resolve(target=TARGET.replace("postgresql://", "postgres://"))
    assert source.drivername == "mysql+pymysql"
    assert target.drivername == "postgresql+psycopg"
    assert (source.database, target.database, target.host) == (
        "crm_import_source", "postgres", "db.staging.example.com"
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"source": None}, "IMPORT_SOURCE_URL is not set"),
        ({"target": None}, "IMPORT_TARGET_URL is not set"),
        ({"source": f"mysql+pymysql://reader:{SOURCE_SECRET}@127.0.0.1:notaport/x"}, "not a valid"),
        ({"source": TARGET.replace("/postgres", "/crm_import_source")}, "must be a MySQL URL"),
        ({"source": "sqlite:///crm_import_source"}, "must be a MySQL URL"),
        ({"target": SOURCE}, "must be a PostgreSQL URL"),
        ({"source": SOURCE.replace("crm_import_source", "ai_sales_crm")}, "protected"),
        ({"source": SOURCE.replace("crm_import_source", "AI_SALES_CRM")}, "protected"),
        ({"source": SOURCE.replace("crm_import_source", "ai_sales_crm_rehearsal")}, "protected"),
        ({"source": SOURCE.replace("crm_import_source", "mysql")}, "protected"),
        ({"source": SOURCE.replace("crm_import_source", "crm_import_source2")}, "must be exactly"),
        ({"source": SOURCE.replace("127.0.0.1", "db.prod.example.com")}, "local MySQL"),
        ({"source_database": "ai_sales_crm"}, "--confirm-source-database"),
        ({"target_database": "crm"}, "--confirm-target-database"),
        ({"target_host": "db.production.example.com"}, "--confirm-target-host"),
        ({"target": TARGET.replace("/postgres", "")}, "must name a database"),
        # URL options that could reach a host or database other than the confirmed one.
        ({"target": TARGET + "?host=db.production.example.com"}, "may only set"),
        ({"target": TARGET + "?hostaddr=10.0.0.9"}, "may only set"),
        ({"target": TARGET + "?dbname=production"}, "may only set"),
        ({"target": TARGET + "?service=prod"}, "may only set"),
        ({"target": TARGET + "?options=-csearch_path%3Dprod"}, "may only set"),
        ({"source": SOURCE + "?unix_socket=/tmp/other.sock"}, "may only set"),
        ({"source": SOURCE + "?read_default_file=prod.cnf"}, "may only set"),
        ({"source": SOURCE + "?init_command=USE+ai_sales_crm"}, "may only set"),
        ({"source": SOURCE.replace(":3306", ":3307")}, "port 3306"),
        ({"target": TARGET + "?sslmode=disable"}, "remote target needs sslmode"),
        ({"target": TARGET + "?sslmode=prefer"}, "remote target needs sslmode"),
    ],
)
def test_refuses_unsafe_or_unconfirmed_connections(kwargs: dict, reason: str) -> None:
    with pytest.raises(ImportAbort, match=reason) as refused:
        _resolve(**kwargs)
    assert SOURCE_SECRET not in str(refused.value) and TARGET_SECRET not in str(refused.value)


def test_local_targets_may_disable_ssl_and_remote_ones_may_verify() -> None:
    local = TARGET.replace("db.staging.example.com", "127.0.0.1") + "?sslmode=disable"
    _resolve(target=local, target_host="127.0.0.1")
    _resolve(target=TARGET + "?sslmode=verify-full&sslrootcert=supabase.crt")
    _resolve(source=SOURCE.replace(":3306", ""))  # Default port.


def test_refuses_a_non_empty_target() -> None:
    importer.refuse_non_empty([], [], [])
    for found in ((["users"], [], []), ([], ["report"], []), ([], [], ["users_user_id_seq"])):
        with pytest.raises(ImportAbort, match="never drops or changes existing objects"):
            importer.refuse_non_empty(*found)


# --- Revisions and layout ------------------------------------------------------------


def test_repository_head_is_the_revision_the_importer_stamps() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    importer.check_script_head(ScriptDirectory.from_config(config).get_heads())
    with pytest.raises(ImportAbort, match="update the importer"):
        importer.check_script_head(["20261020_0015"])


@pytest.mark.parametrize("versions", [[], ["20261013_0012"], ["20261013_0013"], ["20261013_0014", "x"]])
def test_refuses_any_source_revision_but_0014(versions: list[str]) -> None:
    importer.check_revision(["20261013_0014"])
    with pytest.raises(ImportAbort, match="finish and verify the runbook"):
        importer.check_revision(versions)


def test_source_layout_must_match_the_models() -> None:
    layout = {table.name: {c.name for c in table.columns} for table in TABLES.values()}
    layout["alembic_version"] = {"version_num"}
    importer.check_source_layout(layout)
    for broken in (
        {**layout, "users": layout["users"] - {"organization_id"}},
        {**layout, "_rehearsal_marker": {"note"}},
        {k: v for k, v in layout.items() if k != "alembic_version"},
    ):
        with pytest.raises(ImportAbort, match="do not match the models"):
            importer.check_source_layout(broken)


def test_schema_check_compares_every_named_check_constraint() -> None:
    names = importer._named_checks()
    assert {"ck_sales_enquiries_outcome", "ck_hitl_action_proposals_action_type",
            "ck_calendar_event_links_sync_status", "ck_organizations_status"} <= names
    assert len(names) == sum(
        1 for t in TABLES.values() for c in t.constraints if type(c).__name__ == "CheckConstraint"
    )


def test_tables_are_copied_parents_first() -> None:
    order = [table.name for table in importer.copy_order()]
    assert sorted(order) == sorted(TABLES)
    for table in TABLES.values():
        for fk in table.foreign_key_constraints:
            assert order.index(fk.referred_table.name) < order.index(table.name), fk.name


# --- Values --------------------------------------------------------------------------


def _convert(table: str, column: str, value):
    return importer.convert(TABLES[table].c[column], value, f"{table}.{column}")


def test_naive_mysql_datetimes_are_read_as_utc() -> None:
    naive = datetime(2026, 10, 12, 11, 6, 46)
    assert _convert("meetings", "scheduled_at", naive) == naive.replace(tzinfo=timezone.utc)
    assert _convert("meetings", "scheduled_at", naive).utcoffset() == timedelta(0)
    india = timezone(timedelta(hours=5, minutes=30))
    converted = _convert("meetings", "scheduled_at", datetime(2026, 10, 12, 16, 36, 46, tzinfo=india))
    assert converted == naive.replace(tzinfo=timezone.utc) and converted.tzinfo == timezone.utc


@pytest.mark.parametrize("value", ["0000-00-00 00:00:00", date(2026, 10, 12)])
def test_invalid_datetimes_stop_the_import(value) -> None:
    with pytest.raises(ImportAbort, match="not a valid date and time"):
        _convert("meetings", "scheduled_at", value)


def test_dates_stay_dates() -> None:
    assert _convert("sales_enquiries", "expected_close_date", date(2026, 12, 31)) == date(2026, 12, 31)
    for value in ("0000-00-00", datetime(2026, 12, 31)):
        with pytest.raises(ImportAbort, match="not a valid date"):
            _convert("sales_enquiries", "expected_close_date", value)


def test_json_sql_null_and_json_null_stay_distinct() -> None:
    assert _convert("hitl_action_audits", "details", None) is None
    assert _convert("hitl_action_audits", "details", "null") == "null"
    text_value = '{"amount": 1.10, "items": [1, "two"]}'
    assert _convert("hitl_action_audits", "details", text_value) == text_value  # Exact text.


def test_json_is_written_as_cast_text_so_sql_null_is_not_json_null() -> None:
    # Through the model type, psycopg would send Python None as a JSON null, not SQL NULL...
    dialect = psycopg.dialect()
    model_type = TABLES["hitl_action_audits"].c.details.type.dialect_impl(dialect)
    assert model_type.bind_processor(dialect)(None) is not None
    # ...so the importer binds the source text untyped and casts it in SQL instead.
    table = TABLES["hitl_action_audits"]
    row = (1, "act-1", None, "proposed", None, datetime(2026, 1, 1, tzinfo=timezone.utc))
    params = importer.insert_params(table, [row])[0]
    assert params["json_details"] is None
    sql = str(
        importer.insert_statement(table).compile(dialect=postgresql.dialect(), column_keys=list(params))
    )
    assert "(audit_id, action_id, actor_user_id, event_type, details, created_at)" in sql
    assert "CAST(%(json_details)s::VARCHAR AS JSON)" in sql


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ('{"note": "a\\u0000b"}', "NUL"),
        ('{"a\\u0000": 1}', "NUL"),
        ("{not json", "not valid"),
        ({"already": "parsed"}, "not read as text"),
    ],
)
def test_unsupported_json_stops_the_import(value, reason: str) -> None:
    with pytest.raises(ImportAbort, match=reason):
        _convert("hitl_action_audits", "details", value)


def test_json_and_booleans_are_read_without_the_model_types() -> None:
    table = TABLES["hitl_action_audits"]
    assert "CAST(hitl_action_audits.details AS CHAR)" in str(
        importer._select(table).compile(dialect=mysql.dialect())
    )
    assert "CAST(hitl_action_audits.details AS TEXT)" in str(
        importer._select(table).compile(dialect=postgresql.dialect())
    )
    assert "CAST(users.is_active AS SIGNED INTEGER)" in str(
        importer._select(TABLES["users"]).compile(dialect=mysql.dialect())
    )


def test_decimals_keep_their_exact_value() -> None:
    value = _convert("sales_enquiries", "estimated_value", Decimal("1234567890.10"))
    assert str(value) == "1234567890.10"
    for bad in (Decimal("NaN"), Decimal("Infinity"), 12.3):
        with pytest.raises(ImportAbort, match="finite decimal"):
            _convert("sales_enquiries", "estimated_value", bad)


def test_booleans_must_be_zero_or_one() -> None:
    assert _convert("users", "is_active", 1) is True
    assert _convert("users", "is_active", 0) is False
    for bad in (2, -1):
        with pytest.raises(ImportAbort, match="not 0 or 1"):
            _convert("users", "is_active", bad)


def test_nul_characters_in_text_stop_the_import() -> None:
    assert _convert("companies", "company_name", "Acme é") == "Acme é"
    with pytest.raises(ImportAbort, match="NUL"):
        _convert("companies", "company_name", "Acme\x00")


# --- Sequences and validation --------------------------------------------------------


def test_sequences_are_reset_for_every_serial_primary_key() -> None:
    names = {table.name for table in importer.serial_tables()}
    assert {"users", "organizations", "companies", "scheduling_locks", "hitl_action_audits"} <= names
    # String, composite and foreign-key primary keys have no sequence.
    assert not names & {
        "hitl_tasks", "hitl_action_proposals", "agent_conversations",
        "hitl_policy_overrides", "google_calendar_connections",
    }


@pytest.mark.parametrize(
    ("last_value", "is_called", "top", "ahead"),
    [(5, True, 5, True), (5, False, 5, False), (1, False, None, True), (3, True, 7, False)],
)
def test_sequence_must_be_past_the_largest_id(last_value, is_called, top, ahead) -> None:
    assert importer.sequence_is_ahead(last_value, is_called, top) is ahead


def test_row_comparison_reports_the_table_key_and_column() -> None:
    table = TABLES["hitl_action_audits"]
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = {(1,): (1, "act-1", None, "proposed", "null", created)}
    importer.compare_rows(table, rows, dict(rows))
    # A timestamp read back in another zone is the same instant.
    other_zone = created.astimezone(timezone(timedelta(hours=-7)))
    importer.compare_rows(table, rows, {(1,): (1, "act-1", None, "proposed", "null", other_zone)})
    with pytest.raises(ImportAbort, match=r"\(1,\) differs .*details"):
        importer.compare_rows(table, rows, {(1,): (1, "act-1", None, "proposed", None, created)})
    with pytest.raises(ImportAbort, match="1 source rows, 0 in the target"):
        importer.compare_rows(table, rows, {})
    with pytest.raises(ImportAbort, match="whole row is missing"):
        importer.compare_rows(table, rows, {(2,): rows[(1,)]})


# --- Redaction and the transaction ---------------------------------------------------


class _PgError(Exception):
    sqlstate = "28P01"

    class diag:
        table_name = None
        constraint_name = None


def test_driver_errors_are_reduced_to_codes() -> None:
    orig = _PgError(f'password authentication failed for user "crm_owner" ({TARGET_SECRET})')
    described = importer.describe_error(OperationalError("connect", {}, orig))
    assert described == "_PgError, SQLSTATE 28P01"
    mysql_error = Exception(1045, f"Access denied for user 'reader'@'localhost' {SOURCE_SECRET}")
    assert importer.describe_error(mysql_error) == "Exception, MySQL error 1045"


def test_main_never_prints_credentials(monkeypatch, capsys) -> None:
    def fail(*args, **kwargs):
        raise OperationalError("connect", {}, _PgError(f"{TARGET} {TARGET_SECRET}"))

    monkeypatch.setattr(importer, "run", fail)
    argv = ["--dry-run", "--confirm-source-database", "crm_import_source",
            "--confirm-target-database", "postgres", "--confirm-target-host", "db.staging.example.com"]
    with pytest.raises(ImportAbort) as stopped:
        importer.main(argv, {"IMPORT_SOURCE_URL": SOURCE, "IMPORT_TARGET_URL": TARGET})
    output = str(stopped.value) + "".join(capsys.readouterr())
    assert "SQLSTATE 28P01" in output
    for secret in (SOURCE_SECRET, TARGET_SECRET, "db.staging.example.com", "reader"):
        assert secret not in output


def test_import_mode_is_required() -> None:
    with pytest.raises(SystemExit):
        importer.main(["--confirm-source-database", "x", "--confirm-target-database", "y",
                       "--confirm-target-host", "z"], {})


class _Fake:
    def __init__(self, log: list[str], name: str, commit_error: Exception | None = None) -> None:
        self.log, self.name, self.commit_error = log, name, commit_error

    def connect(self):
        return self

    def begin(self):
        return self

    def commit(self) -> None:
        self.log.append(f"{self.name} commit")
        if self.commit_error:
            raise self.commit_error

    def rollback(self) -> None:
        self.log.append(f"{self.name} rollback")

    def dispose(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


def _run(
    monkeypatch, log: list[str], *, dry_run: bool, fail_at: str | None = None, commit_error=None
) -> None:
    engines = iter([_Fake(log, "source"), _Fake(log, "target", commit_error)])
    monkeypatch.setattr(importer, "create_engine", lambda *a, **k: next(engines))
    monkeypatch.setattr(importer, "read_source", lambda conn: {})

    def step(name):
        def call(*args):
            log.append(name)
            if name == fail_at:
                raise ImportAbort(f"{name} failed")
            return {"users": 0}
        return call

    monkeypatch.setattr(importer, "check_target_session", step("check target"))
    monkeypatch.setattr(importer, "import_into", step("import"))
    importer.run(*_resolve(), target_database="postgres", dry_run=dry_run)


def test_dry_run_rolls_back_everything(monkeypatch) -> None:
    log: list[str] = []
    _run(monkeypatch, log, dry_run=True)
    assert log == ["source rollback", "check target", "import", "target rollback"]


def test_commit_happens_only_after_validation(monkeypatch) -> None:
    log: list[str] = []
    _run(monkeypatch, log, dry_run=False)
    assert log[-2:] == ["import", "target commit"]


@pytest.mark.parametrize("fail_at", ["check target", "import"])
def test_any_failure_rolls_back_and_never_commits(monkeypatch, fail_at: str) -> None:
    log: list[str] = []
    with pytest.raises(ImportAbort, match="failed"):
        _run(monkeypatch, log, dry_run=False, fail_at=fail_at)
    assert log[-2:] == [fail_at, "target rollback"]
    assert "target commit" not in log


def test_an_unconfirmed_commit_is_reported_as_unknown(monkeypatch, capsys) -> None:
    log: list[str] = []
    lost = OperationalError("COMMIT", {}, _PgError("server closed the connection"))
    with pytest.raises(OperationalError):
        _run(monkeypatch, log, dry_run=False, commit_error=lost)
    assert log[-1] == "target commit"
    assert "may or may not hold the import" in capsys.readouterr().err


# --- Alembic guard -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "revision", "refused"),
    [
        ("postgresql", (), True),
        ("postgresql", None, True),
        ("postgresql", "base", True),
        ("postgresql", ("20261013_0014",), False),
        ("postgresql", "20261013_0013", False),  # --sql with an explicit starting revision
        ("mysql", (), False),
        ("mysql", None, False),
        ("sqlite", (), False),
    ],
)
def test_guard_refuses_only_an_unstamped_postgres(dialect, revision, refused: bool) -> None:
    if refused:
        with pytest.raises(migration_guard.UnbootstrappedPostgresError, match="MySQL-only"):
            migration_guard.refuse_unbootstrapped_postgres(dialect, revision)
    else:
        migration_guard.refuse_unbootstrapped_postgres(dialect, revision)


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(_alembic())


@pytest.mark.parametrize(
    "destination",
    ["base", "20261013_0013", "20261006_0001", "-1", "20261013_0014-1", "head-1",
     "nope", ("20261013_0013",), ["base"]],
)
def test_postgres_may_not_move_below_the_baseline(destination) -> None:
    with pytest.raises(migration_guard.UnbootstrappedPostgresError, match="below it"):
        migration_guard.refuse_postgres_below_baseline("postgresql", destination, _script())


@pytest.mark.parametrize("destination", [None, "head", "heads", "20261013_0014", ("20261013_0014",)])
def test_postgres_may_stay_at_or_above_the_baseline(destination) -> None:
    migration_guard.refuse_postgres_below_baseline("postgresql", destination, _script())


def test_mysql_downgrades_are_not_restricted() -> None:
    for dialect in ("mysql", "sqlite"):
        migration_guard.refuse_postgres_below_baseline(dialect, "base", _script())


def _alembic(connection=None, buffer: io.StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(ROOT / "alembic"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def test_env_checks_the_database_heads_before_migrating(monkeypatch) -> None:
    seen: list[tuple] = []
    real = migration_guard.refuse_unbootstrapped_postgres
    monkeypatch.setattr(
        migration_guard, "refuse_unbootstrapped_postgres", lambda d, r: seen.append((d, r))
    )
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        command.stamp(_alembic(conn), "20261013_0014")
        command.current(_alembic(conn))
    assert seen == [("sqlite", ()), ("sqlite", ("20261013_0014",))]
    # The bootstrap path: a stamped database passes the real guard as PostgreSQL too.
    real("postgresql", seen[-1][1])


def test_env_refuses_before_running_any_migration(monkeypatch) -> None:
    real = migration_guard.refuse_unbootstrapped_postgres
    # Treat the SQLite connection as PostgreSQL, so the real guard decides.
    monkeypatch.setattr(
        migration_guard, "refuse_unbootstrapped_postgres", lambda d, r: real("postgresql", r)
    )
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        with pytest.raises(migration_guard.UnbootstrappedPostgresError):
            command.upgrade(_alembic(conn), "head")
        assert inspect(conn).get_table_names() == []


@pytest.mark.parametrize("destination", ["base", "20261013_0013", "-1"])
def test_env_refuses_a_postgres_downgrade_before_running_it(monkeypatch, destination: str) -> None:
    real = migration_guard.refuse_postgres_below_baseline
    seen: list = []

    def as_postgres(dialect, target, script):
        seen.append(target)
        real("postgresql", target, script)

    monkeypatch.setattr(migration_guard, "refuse_postgres_below_baseline", as_postgres)
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        command.stamp(_alembic(conn), "20261013_0014")
        with pytest.raises(migration_guard.UnbootstrappedPostgresError, match="below it"):
            command.downgrade(_alembic(conn), destination)
        # The raw argument reached the guard ("base" is not turned into None)...
        assert seen[-1] == destination
        # ...and no downgrade ran: 0014's would have queried tables SQLite does not have.
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "20261013_0014"


def test_offline_postgres_downgrade_sql_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "database_url_override", SecretStr("postgresql://u:p@localhost/crm_offline")
    )
    for revisions in ("20261013_0014:base", "20261013_0014:20261013_0013"):
        with pytest.raises(migration_guard.UnbootstrappedPostgresError, match="below it"):
            command.downgrade(_alembic(buffer=io.StringIO()), revisions, sql=True)


def test_offline_postgres_sql_from_base_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "database_url_override", SecretStr("postgresql://u:p@localhost/crm_offline")
    )
    with pytest.raises(migration_guard.UnbootstrappedPostgresError):
        command.upgrade(_alembic(buffer=io.StringIO()), "head", sql=True)
    buffer = io.StringIO()
    command.upgrade(_alembic(buffer=buffer), "20261013_0013:20261013_0014", sql=True)
    assert "ALTER TABLE sales_enquiries ADD COLUMN" in buffer.getvalue()


# --- Opt-in: a real, disposable PostgreSQL database -----------------------------------
#
# PG_TEST_URL=postgresql://<user>:<pw>@127.0.0.1:5432/crm_import_test?sslmode=disable
# The database must be exactly crm_import_test, reached through a local host name with no
# URL options but sslmode, carry PG_TEST_MARKER as its comment (set once, by hand, when
# the disposable database is created), have an empty public schema, and must not be a
# Supabase database. Every change is made inside a transaction that is rolled back.

PG_TEST_DATABASE = "crm_import_test"
PG_TEST_MARKER = "disposable: crm_import_test"


def _pg_test_url():
    raw = os.environ.get("PG_TEST_URL", "").strip()
    if not raw:
        pytest.skip("PG_TEST_URL is not set; the PostgreSQL import tests are opt-in.")
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        pytest.fail("PG_TEST_URL must be a PostgreSQL URL.")
    if url.database != PG_TEST_DATABASE:
        pytest.fail(f"PG_TEST_URL must name the disposable database {PG_TEST_DATABASE}.")
    if (url.host or "").lower() not in importer.LOCAL_HOSTS:
        pytest.fail("PG_TEST_URL must point at a local PostgreSQL server, never a hosted one.")
    if set(url.query) - {"sslmode"}:
        # host=, hostaddr=, dbname=, service=, options= could connect somewhere else.
        pytest.fail("PG_TEST_URL may not set URL options other than sslmode.")
    if os.environ.get("IMPORT_TARGET_URL", "").strip() == raw:
        pytest.fail("PG_TEST_URL must not be the import target.")
    if settings.environment.strip().lower() == "production":
        pytest.fail("ENVIRONMENT is production.")
    return url.set(drivername="postgresql+psycopg")


@pytest.fixture
def pg_conn():
    engine = create_engine(_pg_test_url())
    try:
        with engine.connect() as conn:
            # The live session, not just the URL: a local port can be a tunnel elsewhere.
            database, marker, hosted_roles = conn.execute(
                text(
                    "SELECT current_database(), shobj_description("
                    "(SELECT oid FROM pg_database WHERE datname = current_database()), 'pg_database'), "
                    "(SELECT COUNT(*) FROM pg_roles WHERE rolname IN "
                    "('anon', 'authenticated', 'service_role', 'supabase_admin'))"
                )
            ).one()
            if database != PG_TEST_DATABASE or marker != PG_TEST_MARKER:
                pytest.fail(f"the connected database is not the marked {PG_TEST_DATABASE}.")
            if hosted_roles:
                pytest.fail("the connected database has Supabase roles; never test against it.")
            if inspect(conn).get_table_names():
                pytest.fail(f"{PG_TEST_DATABASE} must start with an empty public schema.")
            transaction = conn.begin()
            try:
                yield conn
            finally:
                transaction.rollback()
            assert inspect(conn).get_table_names() == [], "the test left tables behind"
    finally:
        engine.dispose()


def _row(table: str, **values) -> tuple:
    return tuple(values.get(column.name) for column in TABLES[table].columns)


def _synthetic_source() -> dict[str, dict[tuple, tuple]]:
    at = datetime(2026, 10, 12, 11, 6, 46, tzinfo=timezone.utc)
    stamps = {"created_at": at, "updated_at": at}
    task_id = str(uuid.uuid4())
    rows = {
        "organizations": [_row("organizations", organization_id=4, name="Org", slug="org", status="active", **stamps)],
        "users": [_row("users", user_id=9, email="a@example.invalid", full_name="A", password_hash="!",
                       role="sales", is_active=True, organization_id=4, **stamps)],
        "scheduling_locks": [_row("scheduling_locks", lock_id=4)],
        "companies": [_row("companies", company_id=2, organization_id=4, company_name="Co", **stamps)],
        "customers": [_row("customers", customer_id=7, organization_id=4, company_id=2,
                           customer_name="Cu", status="active", sales_stage="new", **stamps)],
        "sales_enquiries": [_row("sales_enquiries", enquiry_id=3, organization_id=4, customer_id=7,
                                 enquiry_text="E", priority="normal", status="open",
                                 estimated_value=Decimal("1234567890.10"),
                                 expected_close_date=date(2026, 12, 31),
                                 qualification_status="not_ready", **stamps)],
        "hitl_tasks": [_row("hitl_tasks", task_id=task_id, owner_user_id=9, collected_data='{"n": 1.10}',
                            status="awaiting_approval", expires_at=at, **stamps)],
        "hitl_action_proposals": [_row("hitl_action_proposals", action_id="act-1", task_id=task_id,
                                       owner_user_id=9, action_type="create_meeting", parameters="{}",
                                       payload_fingerprint="f" * 64, status="pending",
                                       expires_at=at, **stamps)],
        "hitl_action_audits": [
            _row("hitl_action_audits", audit_id=1, action_id="act-1", event_type="proposed", details=None, created_at=at),
            _row("hitl_action_audits", audit_id=5, action_id="act-1", event_type="proposed", details="null", created_at=at),
        ],
    }
    source = {name: {} for name in TABLES}
    for name, table_rows in rows.items():
        table = TABLES[name]
        pk_index = [i for i, column in enumerate(table.columns) if column.primary_key]
        source[name] = {tuple(row[i] for i in pk_index): row for row in table_rows}
    return source


def test_postgres_refuses_alembic_on_an_empty_database(pg_conn) -> None:
    with pytest.raises(migration_guard.UnbootstrappedPostgresError):
        command.upgrade(_alembic(pg_conn), "head")
    assert inspect(pg_conn).get_table_names() == []


def test_postgres_import_validates_and_bootstraps_alembic(pg_conn, monkeypatch) -> None:
    monkeypatch.setattr(importer, "TARGET_OWNER", pg_conn.scalar(text("SELECT current_user")))
    importer.check_target_session(pg_conn, PG_TEST_DATABASE)
    script = ScriptDirectory.from_config(_alembic())
    counts = importer.import_into(pg_conn, _synthetic_source(), script)
    assert counts["hitl_action_audits"] == 2 and counts["meetings"] == 0
    details = pg_conn.execute(
        text("SELECT details IS NULL, CAST(details AS TEXT) FROM hitl_action_audits ORDER BY audit_id")
    ).all()
    assert details == [(True, None), (False, "null")]
    assert pg_conn.scalar(text("SELECT nextval(pg_get_serial_sequence('users', 'user_id'))")) == 10
    assert pg_conn.scalar(text("SELECT nextval(pg_get_serial_sequence('meetings', 'meeting_id'))")) == 1
    # The bootstrap path: Alembic now runs on this PostgreSQL database.
    buffer = io.StringIO()
    command.current(_alembic(pg_conn, buffer))
    assert "20261013_0014" in buffer.getvalue()
    with pytest.raises(ImportAbort, match="never drops or changes"):
        importer.check_target_session(pg_conn, PG_TEST_DATABASE)


def test_postgres_validation_catches_a_changed_row(pg_conn, monkeypatch) -> None:
    monkeypatch.setattr(importer, "TARGET_OWNER", pg_conn.scalar(text("SELECT current_user")))
    source = _synthetic_source()
    importer.import_into(pg_conn, source, ScriptDirectory.from_config(_alembic()))
    pg_conn.execute(text("UPDATE hitl_action_audits SET details = NULL WHERE audit_id = 5"))
    with pytest.raises(ImportAbort, match=r"hitl_action_audits row \(5,\) differs .*details"):
        importer.validate(pg_conn, source)
