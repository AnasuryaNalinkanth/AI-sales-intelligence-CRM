"""Keep Alembic from replaying the MySQL-only history on an empty PostgreSQL database.

Revisions up to POSTGRES_BASELINE_REVISION were written for MySQL (0012 and 0013 refuse
any other dialect, but only after 0001-0011 have already run). A PostgreSQL database is
created by scripts/import_mysql_to_postgres.py, which builds the schema from the models
and stamps the baseline; Alembic may only continue from a stamped revision.
"""

POSTGRES_BASELINE_REVISION = "20261013_0014"


class UnbootstrappedPostgresError(RuntimeError):
    pass


def refuse_unbootstrapped_postgres(dialect_name: str, current_revision: object) -> None:
    """current_revision: the database heads online, or the --sql starting revision."""
    if dialect_name == "postgresql" and current_revision in (None, "", "base", ()):
        raise UnbootstrappedPostgresError(
            "Refusing to run Alembic on a PostgreSQL database with no alembic_version: the "
            f"migration history up to {POSTGRES_BASELINE_REVISION} is MySQL-only. Create the "
            "database with scripts/import_mysql_to_postgres.py, which stamps "
            f"{POSTGRES_BASELINE_REVISION}; see docs/postgres_import_runbook.md."
        )


def refuse_postgres_below_baseline(dialect_name: str, destination: object, script) -> None:
    """On PostgreSQL every upgrade, downgrade or stamp must end at the baseline or later.

    Checked before any migration runs. Only absolute revisions (and head) are accepted on
    PostgreSQL: relative targets such as -1 are refused rather than resolved here.
    """
    if dialect_name != "postgresql" or destination is None:
        return
    allowed = {rev.revision for rev in script.walk_revisions(base=POSTGRES_BASELINE_REVISION, head="heads")}
    for target in destination if isinstance(destination, (tuple, list)) else (destination,):
        try:
            revision = script.get_revision(target)
        except Exception:
            revision = None
        if revision is None or revision.revision not in allowed:
            raise UnbootstrappedPostgresError(
                f"Refusing to move a PostgreSQL database to {target!r}: revisions before "
                f"{POSTGRES_BASELINE_REVISION} are MySQL-only, so PostgreSQL cannot be "
                f"downgraded or stamped below it. Use an absolute revision at or after "
                f"{POSTGRES_BASELINE_REVISION}."
            )
