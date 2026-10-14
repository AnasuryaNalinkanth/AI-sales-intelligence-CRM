from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from secrets import token_urlsafe
from typing import TypeGuard
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import APIError
from app.core.logging_utils import log_database_exception
from app.database.connection import rollback_failed_transaction
from app.models import (
    HitlActionAudit,
    HitlActionProposal,
    HitlApprovalDecision,
    HitlExecutionAudit,
    HitlTask,
    User,
)
from app.schemas.agent import AgentActionName

logger = logging.getLogger(__name__)

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "collecting_information": frozenset(
        {"collecting_information", "ready_for_review", "cancelled", "failed", "expired"}
    ),
    "ready_for_review": frozenset(
        {"awaiting_approval", "executing", "rejected", "cancelled", "failed", "expired"}
    ),
    "awaiting_approval": frozenset(
        {"awaiting_approval", "executing", "rejected", "cancelled", "failed", "expired"}
    ),
    "executing": frozenset(
        {
            "awaiting_approval",
            "executing",
            "completed",
            "rejected",
            "cancelled",
            "failed",
            "expired",
        }
    ),
    "completed": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}


def transition_task(task: HitlTask, status: str, now: datetime) -> None:
    if status not in TASK_TRANSITIONS.get(task.status, frozenset()):
        raise APIError(
            f"Invalid HITL task transition: {task.status} -> {status}.",
            409,
            "invalid_task_transition",
        )
    task.status = status
    task.updated_at = now


def _sync_task_status(db: Session, task: HitlTask, now: datetime) -> None:
    proposal_statuses = set(
        db.scalars(
            select(HitlActionProposal.status).where(
                HitlActionProposal.task_id == task.task_id
            )
        ).all()
    )
    if "executing" in proposal_statuses:
        target = "executing"
    elif "pending" in proposal_statuses:
        target = "awaiting_approval"
    elif "failed" in proposal_statuses:
        target = "failed"
    elif "expired" in proposal_statuses:
        target = "expired"
    elif "rejected" in proposal_statuses:
        target = "rejected"
    elif "cancelled" in proposal_statuses:
        target = "cancelled"
    elif proposal_statuses and proposal_statuses <= {"completed"}:
        target = "completed"
    else:
        return
    if task.status not in {
        "completed",
        "rejected",
        "failed",
        "cancelled",
        "expired",
    }:
        transition_task(task, target, now)


@dataclass(frozen=True)
class PendingActionRecord:
    action_id: str
    action: AgentActionName
    payload: dict[str, object]
    task_id: str
    expires_at: datetime
    user_id: int
    payload_fingerprint: str


def _pending_count(db: Session, user_id: int) -> int:
    """Pending proposals in the user's organization: one tenant's backlog never blocks another."""
    organization_id = (
        select(User.organization_id).where(User.user_id == user_id).scalar_subquery()
    )
    return int(
        db.scalar(
            select(func.count())
            .select_from(HitlActionProposal)
            .join(User, User.user_id == HitlActionProposal.owner_user_id)
            .where(
                HitlActionProposal.status == "pending",
                User.organization_id == organization_id,
            )
        )
        or 0
    )


def authorize_decision(
    db: Session,
    proposal: HitlActionProposal,
    *,
    user_id: int,
    is_admin: bool,
    message: str,
) -> None:
    """Owners may decide their own proposals; admins only those of their own organization."""
    if proposal.owner_user_id == user_id:
        return
    owner = db.get(User, proposal.owner_user_id)
    actor = db.get(User, user_id)
    if owner is None or actor is None or owner.organization_id != actor.organization_id:
        # Another organization's action looks missing, so its ID cannot be probed.
        raise APIError("Pending action not found.", 404, "action_not_found")
    if not is_admin:
        raise APIError(message, 403, "forbidden")


def _is_agent_action(value: str) -> TypeGuard[AgentActionName]:
    return value in {
        "create_meeting",
        "create_followup",
        "schedule_call",
        "record_call_result",
        "complete_followup",
        "apply_enrichment",
        "update_lead_qualification",
        "close_opportunity",
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def action_fingerprint(action: AgentActionName, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        {"action": action, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record(
    row: HitlActionProposal,
    *,
    actor_user_id: int | None = None,
    verify_fingerprint: bool = True,
) -> PendingActionRecord:
    if not _is_agent_action(row.action_type):
        raise APIError(
            "The persisted proposal has an unsupported action type.",
            500,
            "invalid_persisted_action",
        )
    fingerprint = action_fingerprint(row.action_type, row.parameters)
    if verify_fingerprint and fingerprint != row.payload_fingerprint:
        raise APIError(
            "The approved action parameters have changed and can no longer be executed.",
            409,
            "action_parameters_changed",
        )
    return PendingActionRecord(
        action_id=row.action_id,
        action=row.action_type,
        payload=row.parameters,
        task_id=row.task_id,
        expires_at=_utc(row.expires_at),
        user_id=actor_user_id if actor_user_id is not None else row.owner_user_id,
        payload_fingerprint=row.payload_fingerprint,
    )


def _write_audit(
    db: Session,
    *,
    action_id: str,
    event_type: str,
    actor_user_id: int | None = None,
    details: dict[str, object] | None = None,
) -> None:
    db.add(
        HitlActionAudit(
            action_id=action_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            details=details,
        )
    )


def _expire_pending(db: Session, now: datetime) -> None:
    expired = list(
        db.scalars(
            select(HitlActionProposal).where(
                HitlActionProposal.status == "pending",
                HitlActionProposal.expires_at <= now,
            )
        ).all()
    )
    for proposal in expired:
        proposal.status = "expired"
        proposal.updated_at = now
        task = db.get(HitlTask, proposal.task_id)
        if task is not None:
            _sync_task_status(db, task, now)
        _write_audit(db, action_id=proposal.action_id, event_type="expired")


def _expire_incomplete_tasks(db: Session, now: datetime) -> None:
    cutoff = now - timedelta(hours=24)
    tasks = list(
        db.scalars(
            select(HitlTask).where(
                HitlTask.status == "collecting_information",
                HitlTask.updated_at <= cutoff,
            )
        ).all()
    )
    for task in tasks:
        transition_task(task, "expired", now)


def _recover_stale_executions(db: Session, now: datetime) -> None:
    cutoff = now - timedelta(seconds=max(300, settings.agent_action_ttl_seconds))
    stale = list(
        db.scalars(
            select(HitlActionProposal)
            .where(
                HitlActionProposal.status == "executing",
                HitlActionProposal.updated_at <= cutoff,
            )
            .with_for_update()
        ).all()
    )
    if not stale:
        return

    for proposal in stale:
        proposal.status = "failed"
        proposal.updated_at = now
        task = db.get(HitlTask, proposal.task_id)
        if task is not None:
            transition_task(task, "failed", now)
        execution = db.scalar(
            select(HitlExecutionAudit).where(
                HitlExecutionAudit.action_id == proposal.action_id
            )
        )
        if execution is not None and execution.status == "started":
            execution.status = "failed"
            execution.error_code = "execution_interrupted"
            execution.result_message = (
                "Execution was interrupted; reconcile the CRM outcome before retrying."
            )
            execution.completed_at = now
        _write_audit(
            db,
            action_id=proposal.action_id,
            event_type="execution_failed",
            details={
                "error_code": "execution_interrupted",
                "requires_reconciliation": True,
            },
        )
    db.commit()


class PendingActionStore:
    """Database-backed pending proposal lifecycle."""

    def __init__(self, max_capacity: int | None = None) -> None:
        self._max_capacity = max_capacity

    @property
    def max_capacity(self) -> int:
        if self._max_capacity is not None:
            return self._max_capacity
        return settings.pending_action_max_capacity

    def create_task(
        self,
        db: Session,
        *,
        user_id: int,
        action_type: AgentActionName | None,
        collected_data: dict[str, object],
    ) -> HitlTask:
        now = datetime.now(timezone.utc)
        task = HitlTask(
            task_id=str(uuid4()),
            owner_user_id=user_id,
            action_type=action_type,
            collected_data=collected_data,
            status="collecting_information",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=24),
        )
        try:
            _expire_incomplete_tasks(db, now)
            _expire_pending(db, now)
            db.add(task)
            db.commit()
            return task
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The HITL task could not be saved.",
                500,
                "hitl_persistence_error",
            ) from exc

    def get_task(
        self,
        db: Session,
        task_id: str,
        *,
        user_id: int,
    ) -> HitlTask:
        now = datetime.now(timezone.utc)
        try:
            _expire_incomplete_tasks(db, now)
            _expire_pending(db, now)
            task = db.get(HitlTask, task_id)
            if task is None or task.owner_user_id != user_id:
                raise APIError("Task not found.", 404, "task_not_found")
            if (
                task.status == "collecting_information"
                and _utc(task.expires_at) <= now
            ):
                transition_task(task, "expired", now)
                db.commit()
            elif db.new or db.dirty:
                db.commit()
            return task
        except APIError:
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The HITL task could not be loaded.",
                500,
                "hitl_persistence_error",
            ) from exc

    def update_task_inputs(
        self,
        db: Session,
        task_id: str,
        *,
        user_id: int,
        collected_data: dict[str, object],
    ) -> HitlTask:
        now = datetime.now(timezone.utc)
        try:
            task = db.scalar(
                select(HitlTask)
                .where(
                    HitlTask.task_id == task_id,
                    HitlTask.owner_user_id == user_id,
                )
                .with_for_update()
            )
            if task is None:
                raise APIError("Task not found.", 404, "task_not_found")
            _expire_incomplete_tasks(db, now)
            if task.status == "expired" or _utc(task.expires_at) <= now:
                if task.status == "collecting_information":
                    transition_task(task, "expired", now)
                    db.commit()
                raise APIError("Task has expired.", 410, "task_expired")
            if task.status != "collecting_information":
                raise APIError(
                    "Task is not collecting information.",
                    409,
                    "invalid_task_transition",
                )
            task.collected_data = collected_data
            transition_task(task, "collecting_information", now)
            task.expires_at = now + timedelta(hours=24)
            db.commit()
            return task
        except APIError:
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The task information could not be saved.",
                500,
                "hitl_persistence_error",
            ) from exc

    def cancel_task(
        self,
        db: Session,
        task_id: str,
        *,
        user_id: int,
    ) -> HitlTask:
        now = datetime.now(timezone.utc)
        try:
            task = db.scalar(
                select(HitlTask)
                .where(
                    HitlTask.task_id == task_id,
                    HitlTask.owner_user_id == user_id,
                )
                .with_for_update()
            )
            if task is None:
                raise APIError("Task not found.", 404, "task_not_found")
            if task.status not in {
                "collecting_information",
                "ready_for_review",
                "awaiting_approval",
            }:
                raise APIError(
                    "Task cannot be cancelled in its current state.",
                    409,
                    "invalid_task_transition",
                )
            proposals = list(
                db.scalars(
                    select(HitlActionProposal)
                    .where(
                        HitlActionProposal.task_id == task_id,
                        HitlActionProposal.status == "pending",
                    )
                    .with_for_update()
                ).all()
            )
            for proposal in proposals:
                if _utc(proposal.expires_at) <= now:
                    raise APIError("Task has expired.", 410, "task_expired")
                proposal.status = "cancelled"
                proposal.updated_at = now
                db.add(
                    HitlApprovalDecision(
                        action_id=proposal.action_id,
                        decided_by_user_id=user_id,
                        decision="cancelled",
                        decided_at=now,
                    )
                )
                _write_audit(
                    db,
                    action_id=proposal.action_id,
                    event_type="cancelled",
                    actor_user_id=user_id,
                )
            transition_task(task, "cancelled", now)
            task.cancelled_by_user_id = user_id
            task.cancelled_at = now
            db.commit()
            return task
        except APIError:
            db.rollback()
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The task could not be cancelled.",
                500,
                "hitl_persistence_error",
            ) from exc

    def mark_ready_for_review(
        self,
        db: Session,
        task_id: str,
        *,
        commit: bool = True,
    ) -> HitlTask:
        task = db.get(HitlTask, task_id)
        if task is None:
            raise APIError("Task not found.", 404, "task_not_found")
        transition_task(task, "ready_for_review", datetime.now(timezone.utc))
        if commit:
            db.commit()
        else:
            db.flush()
        return task

    def add(
        self,
        db: Session,
        action: AgentActionName,
        payload: dict[str, object],
        thread_id: str,
        *,
        user_id: int,
        start_execution: bool = False,
        task_id: str | None = None,
    ) -> PendingActionRecord:
        _ = thread_id
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=7)
        task_id = task_id or str(uuid4())
        action_id = token_urlsafe(32)

        try:
            _recover_stale_executions(db, now)
            _expire_incomplete_tasks(db, now)
            _expire_pending(db, now)
            pending_count = _pending_count(db, user_id)
            if pending_count >= max(1, self.max_capacity):
                db.commit()
                raise APIError(
                    "Too many pending actions are currently awaiting a decision. "
                    "Please try again shortly.",
                    429,
                    "pending_actions_full",
                )

            task = db.get(HitlTask, task_id)
            if task is None:
                task = HitlTask(
                    task_id=task_id,
                    owner_user_id=user_id,
                    action_type=action,
                    collected_data={},
                    status="executing" if start_execution else "awaiting_approval",
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                )
                db.add(task)
                db.flush()
            else:
                if task.owner_user_id != user_id:
                    raise APIError("Task not found.", 404, "task_not_found")
                if task.status not in {
                    "ready_for_review",
                    "awaiting_approval",
                    "executing",
                }:
                    raise APIError(
                        "This task cannot accept another action in its current state.",
                        409,
                        "invalid_task_transition",
                    )
                if task.action_type != action:
                    task.action_type = None
                transition_task(
                    task,
                    "executing" if start_execution else "awaiting_approval",
                    now,
                )
                task.expires_at = expires_at
            proposal = HitlActionProposal(
                action_id=action_id,
                task_id=task_id,
                owner_user_id=user_id,
                action_type=action,
                parameters=payload,
                payload_fingerprint=action_fingerprint(action, payload),
                status="executing" if start_execution else "pending",
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
            db.add(proposal)
            db.flush()
            _write_audit(
                db,
                action_id=action_id,
                event_type="proposed",
                actor_user_id=user_id,
            )
            if start_execution:
                db.add(
                    HitlExecutionAudit(
                        action_id=action_id,
                        initiated_by_user_id=user_id,
                        status="started",
                        started_at=now,
                    )
                )
                _write_audit(
                    db,
                    action_id=action_id,
                    event_type="execution_started",
                    actor_user_id=user_id,
                    details={"mode": "automatic"},
                )
            db.commit()
            return _record(proposal, actor_user_id=user_id)
        except APIError:
            raise
        except SQLAlchemyError as exc:
            log_database_exception(
                logger,
                "save_pending_action",
                exc,
                include_exception_details=False,
                include_integrity_diagnostics=True,
            )
            rollback_failed_transaction(db)
            raise APIError(
                "The pending action could not be saved.",
                500,
                "hitl_persistence_error",
            ) from exc

    def add_many(
        self,
        db: Session,
        actions: list[tuple[AgentActionName, dict[str, object]]],
        *,
        task_id: str,
        user_id: int,
        task_data: dict[str, object] | None = None,
    ) -> list[PendingActionRecord]:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=7)
        try:
            task = db.scalar(
                select(HitlTask)
                .where(
                    HitlTask.task_id == task_id,
                    HitlTask.owner_user_id == user_id,
                )
                .with_for_update()
            )
            if task is None:
                raise APIError("Task not found.", 404, "task_not_found")
            if task.status != "ready_for_review":
                raise APIError(
                    "Task is not ready for review.",
                    409,
                    "invalid_task_transition",
                )
            pending_count = _pending_count(db, user_id)
            if pending_count + len(actions) > max(1, self.max_capacity):
                raise APIError(
                    "Too many pending actions are currently awaiting a decision. "
                    "Please try again shortly.",
                    429,
                    "pending_actions_full",
                )

            records: list[PendingActionRecord] = []
            action_ids: list[str] = []
            for action, payload in actions:
                action_id = token_urlsafe(32)
                action_ids.append(action_id)
                proposal = HitlActionProposal(
                    action_id=action_id,
                    task_id=task_id,
                    owner_user_id=user_id,
                    action_type=action,
                    parameters=payload,
                    payload_fingerprint=action_fingerprint(action, payload),
                    status="pending",
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                )
                db.add(proposal)
                # No ORM relationship orders these inserts, so the audit FK needs the parent first.
                db.flush()
                _write_audit(
                    db,
                    action_id=action_id,
                    event_type="proposed",
                    actor_user_id=user_id,
                )
                records.append(_record(proposal, actor_user_id=user_id))
            task.action_type = actions[0][0] if len(actions) == 1 else None
            if task_data is not None:
                persisted_data = dict(task_data)
                task_operations = persisted_data.get("operations")
                if isinstance(task_operations, list):
                    persisted_data["operations"] = [
                        {**operation, "action_id": action_id}
                        if isinstance(operation, dict)
                        else operation
                        for operation, action_id in zip(task_operations, action_ids)
                    ]
                task.collected_data = persisted_data
            task.expires_at = expires_at
            transition_task(task, "awaiting_approval", now)
            db.commit()
            return records
        except APIError:
            db.rollback()
            raise
        except SQLAlchemyError as exc:
            log_database_exception(
                logger,
                "save_pending_actions",
                exc,
                include_exception_details=False,
                include_integrity_diagnostics=True,
            )
            rollback_failed_transaction(db)
            raise APIError(
                "The pending actions could not be saved.",
                500,
                "hitl_persistence_error",
            ) from exc

    def claim_automatic(
        self,
        db: Session,
        action_id: str,
        *,
        user_id: int,
    ) -> PendingActionRecord:
        now = datetime.now(timezone.utc)
        try:
            result = db.execute(
                update(HitlActionProposal)
                .where(
                    HitlActionProposal.action_id == action_id,
                    HitlActionProposal.owner_user_id == user_id,
                    HitlActionProposal.status == "pending",
                    HitlActionProposal.expires_at > now,
                )
                .values(status="executing", updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                db.rollback()
                raise APIError(
                    "The automatic action is no longer available.",
                    409,
                    "action_already_processed",
                )
            proposal = db.get(HitlActionProposal, action_id)
            if proposal is None:
                raise APIError("Pending action not found.", 404, "action_not_found")
            task = db.get(HitlTask, proposal.task_id)
            if task is not None:
                transition_task(task, "executing", now)
            db.add(
                HitlExecutionAudit(
                    action_id=action_id,
                    initiated_by_user_id=user_id,
                    status="started",
                    started_at=now,
                )
            )
            _write_audit(
                db,
                action_id=action_id,
                event_type="execution_started",
                actor_user_id=user_id,
                details={"mode": "automatic"},
            )
            db.commit()
            return _record(proposal, actor_user_id=user_id)
        except APIError:
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The automatic action could not be started.",
                500,
                "hitl_persistence_error",
            ) from exc

    def reject(
        self,
        db: Session,
        action_id: str,
        *,
        user_id: int,
        is_admin: bool = False,
    ) -> PendingActionRecord:
        return self._decide(db, action_id, user_id=user_id, is_admin=is_admin, decision="rejected")

    def cancel(
        self,
        db: Session,
        action_id: str,
        *,
        user_id: int,
    ) -> PendingActionRecord:
        return self._decide(db, action_id, user_id=user_id, is_admin=False, decision="cancelled")

    def _decide(
        self,
        db: Session,
        action_id: str,
        *,
        user_id: int,
        is_admin: bool,
        decision: str,
    ) -> PendingActionRecord:
        now = datetime.now(timezone.utc)
        try:
            proposal = db.get(HitlActionProposal, action_id)
            if proposal is None:
                raise APIError("Pending action not found.", 404, "action_not_found")
            authorize_decision(
                db,
                proposal,
                user_id=user_id,
                is_admin=is_admin,
                message="You are not authorized to decide this pending action.",
            )
            if proposal.status == "pending" and _utc(proposal.expires_at) <= now:
                _expire_pending(db, now)
                db.commit()
                raise APIError("Pending action has expired.", 410, "action_expired")
            if proposal.status != "pending":
                raise APIError(
                    "Pending action is already being processed or is no longer available.",
                    409,
                    "action_already_processed",
                )
            proposal.status = decision
            proposal.updated_at = now
            db.add(
                HitlApprovalDecision(
                    action_id=action_id,
                    decided_by_user_id=user_id,
                    decision=decision,
                    decided_at=now,
                )
            )
            task = db.get(HitlTask, proposal.task_id)
            if task is not None:
                _sync_task_status(db, task, now)
            _write_audit(
                db,
                action_id=action_id,
                event_type=decision,
                actor_user_id=user_id,
            )
            db.commit()
            return _record(proposal, verify_fingerprint=False)
        except APIError:
            db.rollback()
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The pending action decision could not be saved.",
                500,
                "hitl_persistence_error",
            ) from exc

    def claim(
        self,
        db: Session,
        action_id: str,
        *,
        user_id: int,
        is_admin: bool = False,
    ) -> PendingActionRecord:
        now = datetime.now(timezone.utc)
        try:
            _recover_stale_executions(db, now)
            proposal = db.get(HitlActionProposal, action_id)
            if proposal is None:
                raise APIError("Pending action not found.", 404, "action_not_found")
            authorize_decision(
                db,
                proposal,
                user_id=user_id,
                is_admin=is_admin,
                message="You are not authorized to confirm or cancel this pending action.",
            )
            if proposal.status == "pending" and _utc(proposal.expires_at) <= now:
                _expire_pending(db, now)
                db.commit()
                raise APIError("Pending action has expired.", 410, "action_expired")
            if proposal.status != "pending":
                if proposal.status == "expired":
                    raise APIError("Pending action has expired.", 410, "action_expired")
                if proposal.status in {"completed", "cancelled", "rejected", "failed"}:
                    raise APIError("Pending action not found.", 404, "action_not_found")
                raise APIError(
                    "Pending action is already being processed or is no longer available.",
                    409,
                    "action_already_processed",
                )
            if action_fingerprint(proposal.action_type, proposal.parameters) != (
                proposal.payload_fingerprint
            ):
                proposal.status = "failed"
                proposal.updated_at = now
                task = db.get(HitlTask, proposal.task_id)
                if task is not None:
                    _sync_task_status(db, task, now)
                _write_audit(
                    db,
                    action_id=action_id,
                    event_type="rejected",
                    actor_user_id=user_id,
                    details={"reason": "action_parameters_changed"},
                )
                db.commit()
                raise APIError(
                    "The approved action parameters have changed and can no longer be executed.",
                    409,
                    "action_parameters_changed",
                )

            result = db.execute(
                update(HitlActionProposal)
                .where(
                    HitlActionProposal.action_id == action_id,
                    HitlActionProposal.status == "pending",
                    HitlActionProposal.expires_at > now,
                )
                .values(
                    status="executing",
                    approved_by_user_id=user_id,
                    approved_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                db.rollback()
                raise APIError(
                    "Pending action is already being processed or is no longer available.",
                    409,
                    "action_already_processed",
                )

            proposal.status = "executing"
            proposal.approved_by_user_id = user_id
            proposal.approved_at = now
            proposal.updated_at = now
            task = db.get(HitlTask, proposal.task_id)
            if task is not None:
                task.status = "executing"
                task.updated_at = now

            db.add(
                HitlApprovalDecision(
                    action_id=action_id,
                    decided_by_user_id=user_id,
                    decision="approved",
                    decided_at=now,
                )
            )
            db.add(
                HitlExecutionAudit(
                    action_id=action_id,
                    initiated_by_user_id=user_id,
                    status="started",
                    started_at=now,
                )
            )
            _write_audit(
                db,
                action_id=action_id,
                event_type="approved",
                actor_user_id=user_id,
            )
            _write_audit(
                db,
                action_id=action_id,
                event_type="execution_started",
                actor_user_id=user_id,
            )
            db.commit()
            return _record(proposal, actor_user_id=user_id)
        except APIError:
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The pending action could not be claimed.",
                500,
                "hitl_persistence_error",
            ) from exc

    def block_disabled(
        self,
        db: Session,
        action_id: str,
        *,
        user_id: int,
        is_admin: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        try:
            proposal = db.get(HitlActionProposal, action_id)
            if proposal is None:
                raise APIError("Pending action not found.", 404, "action_not_found")
            authorize_decision(
                db,
                proposal,
                user_id=user_id,
                is_admin=is_admin,
                message="You are not authorized to confirm or cancel this pending action.",
            )
            if proposal.status == "pending" and _utc(proposal.expires_at) <= now:
                _expire_pending(db, now)
                db.commit()
                raise APIError("Pending action has expired.", 410, "action_expired")
            result = db.execute(
                update(HitlActionProposal)
                .where(
                    HitlActionProposal.action_id == action_id,
                    HitlActionProposal.status == "pending",
                    HitlActionProposal.expires_at > now,
                )
                .values(status="rejected", updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                db.rollback()
                raise APIError(
                    "Pending action is already being processed or is no longer available.",
                    409,
                    "action_already_processed",
                )
            proposal.status = "rejected"
            proposal.updated_at = now
            task = db.get(HitlTask, proposal.task_id)
            if task is not None:
                _sync_task_status(db, task, now)
            db.add(
                HitlApprovalDecision(
                    action_id=action_id,
                    decided_by_user_id=user_id,
                    decision="rejected",
                    decided_at=now,
                )
            )
            _write_audit(
                db,
                action_id=action_id,
                event_type="rejected",
                actor_user_id=user_id,
                details={"reason": "action_disabled"},
            )
            db.commit()
        except APIError:
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The disabled action could not be rejected.",
                500,
                "hitl_persistence_error",
            ) from exc

    def finish(
        self,
        db: Session,
        record: PendingActionRecord,
        *,
        status: str,
        result_record_id: int | None,
        error_code: str | None,
        result_message: str,
        commit: bool = True,
    ) -> None:
        now = datetime.now(timezone.utc)
        event_type = (
            "execution_completed" if status == "completed" else "execution_failed"
        )
        try:
            proposal = db.get(HitlActionProposal, record.action_id)
            execution = db.scalar(
                select(HitlExecutionAudit).where(
                    HitlExecutionAudit.action_id == record.action_id
                )
            )
            if proposal is None or execution is None:
                raise APIError(
                    "The action execution audit record is missing.",
                    500,
                    "hitl_persistence_error",
                )

            proposal.status = status
            proposal.updated_at = now
            task = db.get(HitlTask, proposal.task_id)
            if task is not None:
                _sync_task_status(db, task, now)
            execution.status = status
            execution.result_record_id = result_record_id
            execution.error_code = error_code
            execution.result_message = result_message
            execution.completed_at = now
            _write_audit(
                db,
                action_id=record.action_id,
                event_type=event_type,
                actor_user_id=record.user_id,
                details={
                    "status": status,
                    "record_id": result_record_id,
                    "error_code": error_code,
                    "message": result_message,
                },
            )
            if commit:
                db.commit()
            else:
                db.flush()
        except APIError:
            rollback_failed_transaction(db)
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The action execution outcome could not be saved.",
                500,
                "hitl_persistence_error",
            ) from exc


pending_actions = PendingActionStore()
