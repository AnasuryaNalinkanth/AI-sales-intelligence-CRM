"""Migration 20261013_0014: sales lead pipeline columns and the lead HITL actions.

Runs without a database server: the guard uses in-memory SQLite, and SQL generation is
offline. Nothing here connects to the configured application database.
"""

import importlib.util
import io
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.models import HitlActionProposal

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"


def _load(filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load("20261013_0014_sales_lead_pipeline.py")


def _config(buffer: io.StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def test_follows_0013_and_is_the_only_head() -> None:
    assert (MIGRATION.revision, MIGRATION.down_revision) == ("20261013_0014", "20261013_0013")
    assert ScriptDirectory.from_config(_config()).get_heads() == ["20261013_0014"]


def test_action_type_constraint_matches_0009_and_the_model() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.checks: dict[str, str] = {}

        def create_check_constraint(self, name: str, table: str, condition: str) -> None:
            self.checks[name] = condition

        def __getattr__(self, name: str):
            return lambda *args, **kwargs: None

    previous = _load("20261009_0009_shared_activity_scheduling.py")
    previous.op = Recorder()
    previous.upgrade()
    assert previous.op.checks["ck_hitl_action_proposals_action_type"] == MIGRATION.OLD_ACTIONS

    model_check = next(
        constraint
        for constraint in HitlActionProposal.__table__.constraints
        if constraint.name == "ck_hitl_action_proposals_action_type"
    )
    assert str(model_check.sqltext) == MIGRATION.NEW_ACTIONS
    # The new constraint only adds the two lead actions to the old one.
    assert MIGRATION.NEW_ACTIONS.replace(
        ", 'update_lead_qualification', 'close_opportunity'", ""
    ) == MIGRATION.OLD_ACTIONS


def _downgrade_database(proposal_action: str | None, task_action: str | None):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE hitl_action_proposals (action_id TEXT, action_type TEXT)"))
        conn.execute(text("CREATE TABLE hitl_tasks (task_id TEXT, action_type TEXT)"))
        conn.execute(text("CREATE TABLE sales_enquiries (enquiry_id INTEGER, outcome TEXT)"))
        conn.execute(text("INSERT INTO hitl_action_proposals VALUES ('kept', 'create_meeting')"))
        if proposal_action:
            conn.execute(text("INSERT INTO hitl_action_proposals VALUES ('p', :a)"), {"a": proposal_action})
        if task_action:
            conn.execute(text("INSERT INTO hitl_tasks VALUES ('t', :a)"), {"a": task_action})
    return engine


@pytest.mark.parametrize(
    ("proposal_action", "task_action", "named"),
    [
        ("close_opportunity", None, "1 in hitl_action_proposals"),
        ("update_lead_qualification", None, "1 in hitl_action_proposals"),
        (None, "close_opportunity", "1 in hitl_tasks"),
    ],
)
def test_downgrade_stops_before_any_ddl_while_lead_action_records_exist(
    proposal_action: str | None, task_action: str | None, named: str
) -> None:
    engine = _downgrade_database(proposal_action, task_action)
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(MIGRATION.MigrationStopped, match="before any schema change") as error:
                MIGRATION.downgrade()
        assert named in str(error.value)
        # No DDL ran and nothing was deleted.
        assert "outcome" in {column["name"] for column in inspect(conn).get_columns("sales_enquiries")}
        assert conn.execute(text("SELECT COUNT(*) FROM hitl_action_proposals")).scalar() == (
            2 if proposal_action else 1
        )
        assert conn.execute(text("SELECT COUNT(*) FROM hitl_tasks")).scalar() == (1 if task_action else 0)


def test_guard_allows_downgrade_when_only_older_actions_exist() -> None:
    engine = _downgrade_database(None, None)
    with engine.connect() as conn:
        assert MIGRATION._guard_lead_action_records(conn) is None


def test_offline_upgrade_sql_and_refused_offline_downgrade() -> None:
    buffer = io.StringIO()
    command.upgrade(_config(buffer), "20261013_0013:20261013_0014", sql=True)
    sql = buffer.getvalue()
    assert "ADD COLUMN qualification_status VARCHAR(20) NOT NULL DEFAULT 'not_ready'" in sql
    assert "DROP CHECK ck_hitl_action_proposals_action_type" in sql
    assert MIGRATION.NEW_ACTIONS in sql
    statements = [line.strip().upper() for line in sql.splitlines()]
    assert not any(
        line.startswith(("DELETE ", "UPDATE SALES_ENQUIRIES", "DROP TABLE", "TRUNCATE"))
        or ("DROP COLUMN" in line)
        for line in statements
    )

    with pytest.raises(Exception, match="offline"):
        command.downgrade(_config(io.StringIO()), "20261013_0014:20261013_0013", sql=True)
