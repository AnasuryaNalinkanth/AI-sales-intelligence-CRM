from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class HitlTask(Base):
    __tablename__ = "hitl_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('collecting_information', 'ready_for_review', "
            "'awaiting_approval', 'executing', 'completed', 'rejected', "
            "'cancelled', 'failed', 'expired')",
            name="ck_hitl_tasks_status",
        ),
        Index("ix_hitl_tasks_owner_status_expiry", "owner_user_id", "status", "expires_at"),
    )

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action_type: Mapped[str | None] = mapped_column(String(64))
    collected_data: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    cancelled_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.user_id",
            ondelete="SET NULL",
            name="fk_hitl_tasks_cancelled_by_user_id_users",
        )
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="awaiting_approval")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HitlActionProposal(Base):
    __tablename__ = "hitl_action_proposals"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('create_meeting', 'create_followup', 'schedule_call', "
            "'record_call_result', 'complete_followup', 'apply_enrichment', "
            "'update_lead_qualification', 'close_opportunity')",
            name="ck_hitl_action_proposals_action_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'executing', 'completed', 'rejected', "
            "'cancelled', 'failed', 'expired')",
            name="ck_hitl_action_proposals_status",
        ),
        Index(
            "ix_hitl_action_proposals_task_status",
            "task_id",
            "status",
        ),
        Index(
            "ix_hitl_action_proposals_owner_status_expiry",
            "owner_user_id",
            "status",
            "expires_at",
        ),
    )

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("hitl_tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parameters: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HitlApprovalDecision(Base):
    __tablename__ = "hitl_approval_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected', 'cancelled')",
            name="ck_hitl_approval_decisions_decision",
        ),
        UniqueConstraint("action_id", name="uq_hitl_approval_decisions_action_id"),
        Index("ix_hitl_approval_decisions_decided_at", "decided_at"),
    )

    decision_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("hitl_action_proposals.action_id", ondelete="RESTRICT"), nullable=False
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL")
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HitlExecutionAudit(Base):
    __tablename__ = "hitl_execution_audits"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'completed', 'failed')",
            name="ck_hitl_execution_audits_status",
        ),
        UniqueConstraint("action_id", name="uq_hitl_execution_audits_action_id"),
        Index("ix_hitl_execution_audits_status_started", "status", "started_at"),
    )

    execution_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("hitl_action_proposals.action_id", ondelete="RESTRICT"), nullable=False
    )
    initiated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="started")
    result_record_id: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    result_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HitlPolicyOverride(Base):
    __tablename__ = "hitl_policy_overrides"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('automatic', 'approval_required', 'disabled')",
            name="ck_hitl_policy_overrides_mode",
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.organization_id",
            ondelete="RESTRICT",
            name="fk_hitl_policy_overrides_organization_id_organizations",
        ),
        primary_key=True,
    )
    action_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class HitlPolicyAudit(Base):
    __tablename__ = "hitl_policy_audits"
    __table_args__ = (
        CheckConstraint(
            "previous_mode IN ('automatic', 'approval_required', 'disabled')",
            name="ck_hitl_policy_audits_previous_mode",
        ),
        CheckConstraint(
            "new_mode IN ('automatic', 'approval_required', 'disabled')",
            name="ck_hitl_policy_audits_new_mode",
        ),
        Index("ix_hitl_policy_audits_action_changed", "action_type", "changed_at"),
        Index("ix_hitl_policy_audits_organization_changed", "organization_id", "changed_at"),
    )

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.organization_id",
            ondelete="RESTRICT",
            name="fk_hitl_policy_audits_organization_id_organizations",
        ),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    new_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL")
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HitlActionAudit(Base):
    __tablename__ = "hitl_action_audits"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('proposed', 'approved', 'rejected', 'expired', "
            "'cancelled', 'execution_started', 'execution_completed', "
            "'execution_failed')",
            name="ck_hitl_action_audits_event_type",
        ),
        Index("ix_hitl_action_audits_action_created", "action_id", "created_at"),
    )

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("hitl_action_proposals.action_id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
