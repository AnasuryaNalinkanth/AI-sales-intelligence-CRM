"""Rehearsal only: stop migration 20261013_0012 after a chosen step, then resume it.

Never imported by the application. Every command refuses to run unless all guards
pass, so it cannot reach the source or a production database:

  1. The target comes only from REHEARSAL_DATABASE_URL (never .env or app settings).
  2. The database is exactly ai_sales_crm_rehearsal, by URL and by SELECT DATABASE().
  3. The host and port are in ALLOWED_TARGETS and ENVIRONMENT is not production,
     in the process environment and in the effective app settings.
  4. The schema holds the _rehearsal_marker table, created after each restore.
  5. Before Alembic runs, the app settings are pointed at the same target and checked.
  6. alembic_version is 20261013_0011 before interrupting or resuming, and
     20261013_0012 before downgrading.

Usage, from the repository root:
  python scripts/rehearsal/migration_interrupt.py interrupt --after {2,4,6}
  python scripts/rehearsal/migration_interrupt.py resume
  python scripts/rehearsal/migration_interrupt.py downgrade
  python scripts/rehearsal/migration_interrupt.py show
"""

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, make_url

REHEARSAL_DATABASE = "ai_sales_crm_rehearsal"
FORBIDDEN_DATABASES = {"ai_sales_crm"}
# Host and port together: a local port may be a tunnel to another server.
# Adding a target is a reviewed code change, never a runtime option.
DEFAULT_PORT = 3306
ALLOWED_TARGETS = {("localhost", 3306), ("127.0.0.1", 3306), ("::1", 3306)}
ALLOWED_QUERY_KEYS = {"charset"}
MARKER_TABLE = "_rehearsal_marker"
STARTING_REVISION = "20261013_0011"
TARGET_REVISION = "20261013_0012"
STOP_POINTS = (2, 4, 6)
ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = ROOT / "alembic" / "versions" / "20261013_0012_add_organizations.py"


class GuardError(SystemExit):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Refusing to run: {reason}")


def target_url(environ: dict[str, str] | None = None) -> URL:
    """Guards 1-3: everything decidable before connecting."""
    environ = os.environ if environ is None else environ
    raw = environ.get("REHEARSAL_DATABASE_URL", "").strip()
    if not raw:
        raise GuardError("REHEARSAL_DATABASE_URL is not set.")
    try:
        url = make_url(raw)
    except Exception as exc:
        raise GuardError("REHEARSAL_DATABASE_URL is not a valid URL.") from exc
    if not url.drivername.startswith("mysql"):
        raise GuardError("the rehearsal target must be MySQL.")
    database = url.database or ""
    if database != REHEARSAL_DATABASE or database.lower() in FORBIDDEN_DATABASES:
        raise GuardError(f"the database must be exactly {REHEARSAL_DATABASE}.")
    if set(url.query) - ALLOWED_QUERY_KEYS:
        raise GuardError("only these URL query options are allowed: charset.")
    target = ((url.host or "").lower(), url.port or DEFAULT_PORT)
    if target not in ALLOWED_TARGETS:
        raise GuardError(f"host and port {target!r} are not in the rehearsal allowlist.")
    if environ.get("ENVIRONMENT", "").strip().lower() == "production":
        raise GuardError("ENVIRONMENT is production.")
    return url


def check_connection(conn: Connection, *, revision: str | None) -> None:
    """Guards 2, 4 and 6, against the live connection."""
    if conn.scalar(text("SELECT DATABASE()")) != REHEARSAL_DATABASE:
        raise GuardError("the connected database is not the rehearsal database.")
    has_marker = conn.scalar(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": MARKER_TABLE},
    )
    if not has_marker:
        raise GuardError(f"the {MARKER_TABLE} table is missing; this is not a rehearsal restore.")
    if revision is not None:
        versions = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
        if versions != [revision]:
            raise GuardError(f"expected alembic_version {revision}, found {versions}.")


def point_app_settings_at(url: URL) -> None:
    """Guard 5: Alembic's env.py connects through app settings, so aim and verify them."""
    if "app.core.config" in sys.modules:
        raise GuardError("app settings were loaded before the rehearsal target was set.")
    os.environ.update(
        {
            "DB_HOST": url.host or "",
            "DB_PORT": str(url.port or DEFAULT_PORT),
            "DB_NAME": url.database or "",
            "DB_USER": url.username or "",
            "DB_PASSWORD": url.password or "",
        }
    )
    sys.path.insert(0, str(ROOT))
    from app.core.config import settings

    # The effective setting includes .env, which the process-environment check cannot see.
    if settings.environment.strip().lower() == "production":
        raise GuardError("the application settings say ENVIRONMENT is production.")
    configured = settings.database_url
    if (
        configured.database != REHEARSAL_DATABASE
        or configured.host != url.host
        or configured.port != (url.port or DEFAULT_PORT)
    ):
        raise GuardError("app settings do not point at the rehearsal database.")


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0012", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def interrupt(url: URL, after: int) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            check_connection(conn, revision=STARTING_REVISION)
            migration = _load_migration()
            with Operations.context(MigrationContext.configure(conn)):
                migration.run_steps(conn, through=after)
            conn.commit()
            check_connection(conn, revision=STARTING_REVISION)
    finally:
        engine.dispose()
    print(f"Stopped after step {after}; alembic_version is still {STARTING_REVISION}.")


def resume(url: URL) -> None:
    point_app_settings_at(url)  # Before connecting, so a production .env refuses first.
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            check_connection(conn, revision=STARTING_REVISION)
    finally:
        engine.dispose()

    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), TARGET_REVISION)

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            check_connection(conn, revision=TARGET_REVISION)
    finally:
        engine.dispose()
    print(f"Resumed; alembic_version is {TARGET_REVISION}.")


def downgrade(url: URL) -> None:
    point_app_settings_at(url)  # Before connecting, so a production .env refuses first.
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            check_connection(conn, revision=TARGET_REVISION)
    finally:
        engine.dispose()

    from alembic import command
    from alembic.config import Config

    command.downgrade(Config(str(ROOT / "alembic.ini")), STARTING_REVISION)

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            check_connection(conn, revision=STARTING_REVISION)
    finally:
        engine.dispose()
    print(f"Downgraded; alembic_version is {STARTING_REVISION}.")


def show(url: URL) -> None:
    """Read-only state for comparing an interrupted-and-resumed run with the baseline."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            check_connection(conn, revision=None)
            print("alembic_version:", conn.scalar(text("SELECT version_num FROM alembic_version")))
            for table in ("organizations", "users"):
                exists = conn.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name = :t"
                    ),
                    {"t": table},
                )
                if exists:
                    # Strip the volatile AUTO_INCREMENT counter so runs compare cleanly.
                    ddl = conn.execute(text(f"SHOW CREATE TABLE `{table}`")).one()[1]
                    print(re.sub(r" AUTO_INCREMENT=\d+", "", ddl))
            print("roles:", conn.execute(text("SELECT role, COUNT(*) FROM users GROUP BY role ORDER BY role")).all())
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    stop = commands.add_parser("interrupt")
    stop.add_argument("--after", type=int, choices=STOP_POINTS, required=True)
    commands.add_parser("resume")
    commands.add_parser("downgrade")
    commands.add_parser("show")
    args = parser.parse_args(argv)

    url = target_url()
    print("Target:", url.render_as_string(hide_password=True))
    if args.command == "interrupt":
        interrupt(url, args.after)
    elif args.command == "resume":
        resume(url)
    elif args.command == "downgrade":
        downgrade(url)
    else:
        show(url)


if __name__ == "__main__":
    main()
