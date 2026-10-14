"""Add lead capture, qualification, and opportunity outcome fields to sales enquiries.

Sales enquiries are presented as sales leads; the table keeps its name so existing
routes, imports, and integrations keep working. Every new column is nullable or has
a server default, so existing rows stay valid: they start as 'not_ready' with no
outcome, and nothing is inferred from their legacy status.

Revision ID: 20261013_0014
Revises: 20261013_0013
Create Date: 2026-10-13
"""

from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261013_0014"
down_revision: Union[str, None] = "20261013_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Literal copies: a migration must not change if application constants do.
OLD_ACTIONS = (
    "action_type IN ('create_meeting', 'create_followup', 'schedule_call', "
    "'record_call_result', 'complete_followup', 'apply_enrichment')"
)
NEW_ACTIONS = (
    "action_type IN ('create_meeting', 'create_followup', 'schedule_call', "
    "'record_call_result', 'complete_followup', 'apply_enrichment', "
    "'update_lead_qualification', 'close_opportunity')"
)
CHECKS = {
    "ck_sales_enquiries_qualification": "qualification_status IN ('ready', 'not_ready')",
    "ck_sales_enquiries_lead_source": (
        "lead_source IN ('website', 'referral', 'campaign', 'manual', 'import', 'other')"
    ),
    "ck_sales_enquiries_outcome": (
        "(outcome IS NULL AND closed_at IS NULL AND loss_reason IS NULL)"
        " OR (outcome = 'won' AND closed_at IS NOT NULL AND loss_reason IS NULL)"
        " OR (outcome = 'lost' AND closed_at IS NOT NULL AND loss_reason IS NOT NULL)"
    ),
}


def upgrade() -> None:
    op.add_column("sales_enquiries", sa.Column("contact_id", sa.Integer(), nullable=True))
    op.add_column("sales_enquiries", sa.Column("assigned_user_id", sa.Integer(), nullable=True))
    op.add_column("sales_enquiries", sa.Column("lead_source", sa.String(length=50), nullable=True))
    op.add_column("sales_enquiries", sa.Column("expected_close_date", sa.Date(), nullable=True))
    op.add_column(
        "sales_enquiries",
        sa.Column(
            "qualification_status",
            sa.String(length=20),
            nullable=False,
            server_default="not_ready",
        ),
    )
    op.add_column("sales_enquiries", sa.Column("qualification_reason", sa.Text(), nullable=True))
    op.add_column("sales_enquiries", sa.Column("outcome", sa.String(length=10), nullable=True))
    op.add_column(
        "sales_enquiries", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("sales_enquiries", sa.Column("loss_reason", sa.Text(), nullable=True))
    for column in ("contact_id", "assigned_user_id", "qualification_status", "outcome"):
        op.create_index(f"ix_sales_enquiries_{column}", "sales_enquiries", [column])
    op.create_foreign_key(
        "fk_sales_enquiries_contact_id_contacts",
        "sales_enquiries", "contacts", ["contact_id"], ["contact_id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_sales_enquiries_assigned_user_id_users",
        "sales_enquiries", "users", ["assigned_user_id"], ["user_id"], ondelete="SET NULL",
    )
    for name, condition in CHECKS.items():
        op.create_check_constraint(name, "sales_enquiries", condition)

    op.drop_constraint(
        "ck_hitl_action_proposals_action_type", "hitl_action_proposals", type_="check"
    )
    op.create_check_constraint(
        "ck_hitl_action_proposals_action_type", "hitl_action_proposals", NEW_ACTIONS
    )


class MigrationStopped(RuntimeError):
    pass


LEAD_ACTIONS = ("update_lead_qualification", "close_opportunity")


def _guard_lead_action_records(bind: Any) -> None:
    """Refuse, before any DDL, while records of the lead actions exist; never delete them.

    The older action-type check would reject those proposals, and the older application
    cannot read proposals or tasks with these action types.
    """
    found = {
        table: bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE action_type IN (:qualification, :closure)"
            ),
            {"qualification": LEAD_ACTIONS[0], "closure": LEAD_ACTIONS[1]},
        ).scalar()
        for table in ("hitl_action_proposals", "hitl_tasks")
    }
    found = {table: count for table, count in found.items() if count}
    if found:
        counts = ", ".join(f"{count} in {table}" for table, count in found.items())
        raise MigrationStopped(
            "Downgrade of 20261013_0014 stopped before any schema change: "
            f"records for the lead actions {', '.join(LEAD_ACTIONS)} still exist ({counts}). "
            "Resolve them first (approve, reject, cancel, or let them expire, then remove "
            "them deliberately); this migration never deletes them."
        )


def downgrade() -> None:
    """Drops lead qualification and outcome data; it cannot be reconstructed afterwards.

    Refused while records of the lead actions exist, rather than deleting them.
    """
    if op.get_context().as_sql:
        raise MigrationStopped(
            "Downgrade of 20261013_0014 is not supported in offline (--sql) mode: the "
            "lead-action guard must check the live database first."
        )
    _guard_lead_action_records(op.get_bind())
    op.drop_constraint(
        "ck_hitl_action_proposals_action_type", "hitl_action_proposals", type_="check"
    )
    op.create_check_constraint(
        "ck_hitl_action_proposals_action_type", "hitl_action_proposals", OLD_ACTIONS
    )
    for name in CHECKS:
        op.drop_constraint(name, "sales_enquiries", type_="check")
    op.drop_constraint(
        "fk_sales_enquiries_assigned_user_id_users", "sales_enquiries", type_="foreignkey"
    )
    op.drop_constraint("fk_sales_enquiries_contact_id_contacts", "sales_enquiries", type_="foreignkey")
    for column in ("outcome", "qualification_status", "assigned_user_id", "contact_id"):
        op.drop_index(f"ix_sales_enquiries_{column}", table_name="sales_enquiries")
    for column in (
        "loss_reason",
        "closed_at",
        "outcome",
        "qualification_reason",
        "qualification_status",
        "expected_close_date",
        "lead_source",
        "assigned_user_id",
        "contact_id",
    ):
        op.drop_column("sales_enquiries", column)
