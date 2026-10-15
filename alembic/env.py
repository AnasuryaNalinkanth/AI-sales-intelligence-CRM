from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import settings
from app.database import migration_guard
from app.database.connection import driver_connect_args
from app.models import Company

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Company.metadata


def run_migrations_offline() -> None:
    migration_guard.refuse_unbootstrapped_postgres(
        settings.database_url.get_backend_name(), context.get_starting_revision_argument()
    )
    context.configure(
        url=settings.database_url.render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    _refuse_below_postgres_baseline(settings.database_url.get_backend_name())

    with context.begin_transaction():
        context.run_migrations()


def _refuse_below_postgres_baseline(dialect_name: str) -> None:
    # The raw argument: get_revision_argument() turns "base" into None.
    migration = context.get_context()
    migration_guard.refuse_postgres_below_baseline(
        dialect_name, migration.opts.get("destination_rev"), migration.script
    )


def _run_on(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    migration_guard.refuse_unbootstrapped_postgres(
        connection.dialect.name, context.get_context().get_current_heads()
    )
    _refuse_below_postgres_baseline(connection.dialect.name)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests pass their own guarded connection; normal runs use the app settings.
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_on(connection)
        return

    connectable = create_engine(
        settings.database_url,
        poolclass=pool.NullPool,
        connect_args=driver_connect_args(settings),
    )
    try:
        with connectable.connect() as connection:
            _run_on(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
