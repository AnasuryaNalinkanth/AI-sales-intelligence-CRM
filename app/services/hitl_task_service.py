from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import APIError
from app.models import (
    HitlActionProposal,
    HitlExecutionAudit,
    HitlTask,
    User,
)
from app.schemas.agent import (
    AgentActionName,
    AgentActionResult,
    AgentTaskInputRequest,
    AgentTaskOperationResponse,
    AgentTaskResponse,
    AgentTaskStartRequest,
)
from app.services.activity_validation import validate_customer_references
from app.services.agent_action_service import _run_claimed_action
from app.services.agent_action_state import (
    PendingActionRecord,
    action_fingerprint,
    pending_actions,
    superseded_action_ids,
)
from app.services.hitl_action_registry import (
    ACTION_REGISTRY,
    form_fields,
    task_is_expired,
    validate_operation,
)
from app.services.hitl_policy import get_action_policy
from app.services.scheduling_service import (
    ScheduleRequest,
    default_duration,
    inspect_availability,
    require_future_start,
)
from app.agent.tools.action_schemas import (
    CreateFollowupProposal,
    CreateMeetingProposal,
    ScheduleCallProposal,
)


def _decode_operations(
    data: dict[str, Any],
    *,
    proposals: list[HitlActionProposal] | None = None,
) -> list[dict[str, Any]]:
    operations = data.get("operations")
    if not isinstance(operations, list) or not operations:
        operations = [
            {
                "action": proposal.action_type,
                "values": proposal.parameters,
                "status": proposal.status,
                "action_id": proposal.action_id,
            }
            for proposal in proposals or []
        ]
    if not operations:
        raise APIError("Persisted task operations are invalid.", 500, "invalid_task_data")
    if any(not isinstance(item, dict) for item in operations):
        raise APIError("Persisted task operations are invalid.", 500, "invalid_task_data")
    return operations


def _validate_targets(
    db: Session,
    payloads: list[tuple[AgentActionName, dict[str, Any]]],
    *,
    organization_id: int,
) -> None:
    for _, payload in payloads:
        validate_customer_references(
            db,
            payload["customer_id"],
            organization_id=organization_id,
            contact_id=payload.get("contact_id"),
            enquiry_id=payload.get("enquiry_id"),
            meeting_id=payload.get("meeting_id"),
            call_id=payload.get("call_id"),
            followup_id=payload.get("followup_id"),
        )


def _require_scheduling_availability(
    db: Session,
    action: AgentActionName,
    payload: dict[str, Any],
    *,
    organization_id: int,
) -> None:
    request: ScheduleRequest | None = None
    if action == "create_meeting":
        proposal = CreateMeetingProposal.model_validate(payload)
        if proposal.status == "scheduled":
            request = ScheduleRequest(
                activity_type="meeting",
                starts_at=proposal.scheduled_at,
                duration_minutes=proposal.duration or default_duration("meeting"),
                timezone_name=proposal.scheduled_timezone or "UTC",
                organization_id=organization_id,
            )
    elif action == "schedule_call":
        proposal = ScheduleCallProposal.model_validate(payload)
        request = ScheduleRequest(
            activity_type="call",
            starts_at=proposal.scheduled_at,
            duration_minutes=proposal.duration or default_duration("call"),
            timezone_name=proposal.scheduled_timezone,
            organization_id=organization_id,
        )
    elif action == "create_followup":
        proposal = CreateFollowupProposal.model_validate(payload)
        if proposal.status in {"pending", "in_progress", "overdue"}:
            request = ScheduleRequest(
                activity_type="followup",
                starts_at=proposal.due_date,
                duration_minutes=proposal.duration or default_duration("followup"),
                timezone_name=proposal.due_timezone or "UTC",
                organization_id=organization_id,
            )
    if request is None:
        return
    if not (action == "create_followup" and payload.get("status") == "overdue"):
        require_future_start(db, request)
    result = inspect_availability(db, request)
    if not result["available"]:
        raise APIError(
            "The requested time overlaps another scheduled CRM activity.",
            409,
            "schedule_conflict",
            details={
                key: value
                for key, value in result.items()
                if key in {
                    "activity_type",
                    "starts_at",
                    "ends_at",
                    "timezone",
                    "duration_minutes",
                    "conflicts",
                    "suggestions",
                }
            },
        )


def _validate_partial_targets(
    db: Session,
    values: dict[str, Any],
    *,
    organization_id: int,
) -> None:
    customer_id = values.get("customer_id")
    reference_fields = (
        "contact_id",
        "enquiry_id",
        "meeting_id",
        "call_id",
        "followup_id",
    )
    if customer_id is None:
        if any(values.get(field) is not None for field in reference_fields):
            raise APIError(
                "Select the customer before selecting related CRM records.",
                422,
                "invalid_task_input",
            )
        return
    if isinstance(customer_id, bool) or not isinstance(customer_id, int):
        raise APIError("Select a valid CRM customer ID.", 422, "invalid_task_input")
    validate_customer_references(
        db,
        customer_id,
        organization_id=organization_id,
        contact_id=values.get("contact_id"),
        enquiry_id=values.get("enquiry_id"),
        meeting_id=values.get("meeting_id"),
        call_id=values.get("call_id"),
        followup_id=values.get("followup_id"),
    )


def _run_automatic_actions(
    db: Session,
    records: list[PendingActionRecord],
    *,
    organization_id: int,
) -> None:
    for record in records:
        if record.action in {"create_meeting", "schedule_call"}:
            continue
        if get_action_policy(db, record.action, organization_id=organization_id) != "automatic":
            continue
        claimed = pending_actions.claim_automatic(
            db,
            record.action_id,
            user_id=record.user_id,
        )
        _run_claimed_action(db, claimed, require_automatic=True)


def _finalize_if_complete(
    db: Session,
    task: HitlTask,
    operations: list[dict[str, Any]],
    *,
    user_id: int,
    organization_id: int,
) -> HitlTask:
    payloads: list[tuple[AgentActionName, dict[str, Any]]] = []
    for item in operations:
        action = item["action"]
        values = item["values"]
        payload, missing = validate_operation(action, values)
        if missing or payload is None:
            return task
        _require_scheduling_availability(db, action, payload, organization_id=organization_id)
        mode = get_action_policy(db, action, organization_id=organization_id)
        if mode == "disabled":
            raise APIError(
                f"{action} is disabled and cannot be proposed.",
                403,
                "action_disabled",
            )
        payloads.append((action, payload))
    _validate_targets(db, payloads, organization_id=organization_id)

    pending_actions.mark_ready_for_review(db, task.task_id, commit=False)
    task_data = {"operations": operations}
    records = pending_actions.add_many(
        db,
        payloads,
        task_id=task.task_id,
        user_id=user_id,
        task_data=task_data,
    )
    _run_automatic_actions(db, records, organization_id=organization_id)
    db.expire_all()
    result = db.get(HitlTask, task.task_id)
    if result is None:
        raise APIError("Task not found.", 404, "task_not_found")
    return result


def _operation_status(
    db: Session,
    item: dict[str, Any],
) -> tuple[str, AgentActionResult | None]:
    action_id = item.get("action_id")
    if not isinstance(action_id, str):
        status = item.get("status")
        if status in {"collecting_information", "ready_for_review"}:
            return status, None
        return "collecting_information", None
    proposal = db.get(HitlActionProposal, action_id)
    if proposal is None:
        return "failed", None
    result = None
    if proposal.status in {"completed", "failed"}:
        audit = db.scalar(
            select(HitlExecutionAudit).where(
                HitlExecutionAudit.action_id == action_id
            )
        )
        if audit is not None:
            result = AgentActionResult(
                status=proposal.status,
                action=proposal.action_type,
                record_id=audit.result_record_id,
                error_code=audit.error_code,
                message=audit.result_message or "",
            )
    return proposal.status, result


def _task_proposals(
    db: Session, task_id: str
) -> tuple[list[HitlActionProposal], list[HitlActionProposal]]:
    """(current, superseded) proposals of a task, oldest first."""
    proposals = list(
        db.scalars(
            select(HitlActionProposal)
            .where(HitlActionProposal.task_id == task_id)
            .order_by(HitlActionProposal.created_at)
        ).all()
    )
    superseded = superseded_action_ids(
        db, [proposal.action_id for proposal in proposals if proposal.status == "cancelled"]
    )
    return (
        [proposal for proposal in proposals if proposal.action_id not in superseded],
        [proposal for proposal in proposals if proposal.action_id in superseded],
    )


def _response(db: Session, task: HitlTask) -> AgentTaskResponse:
    owner = db.get(User, task.owner_user_id)
    if owner is None:
        raise APIError("Task not found.", 404, "task_not_found")
    current, superseded = _task_proposals(db, task.task_id)
    # Earlier versions replaced by a revision are listed first, read-only, as "superseded".
    history = [
        {
            "action": proposal.action_type,
            "values": proposal.parameters,
            "status": "superseded",
            "action_id": proposal.action_id,
        }
        for proposal in superseded
    ]
    proposals = current + superseded
    operations: list[AgentTaskOperationResponse] = []
    for item in [*history, *_decode_operations(task.collected_data, proposals=current)]:
        action = item.get("action")
        values = item.get("values")
        if action not in ACTION_REGISTRY or not isinstance(values, dict):
            raise APIError("Persisted task operations are invalid.", 500, "invalid_task_data")
        action_id = item.get("action_id")
        proposal = next(
            (proposal for proposal in proposals if proposal.action_id == action_id),
            None,
        ) if isinstance(action_id, str) else None
        if proposal is not None:
            if proposal.action_type != action:
                raise APIError("A persisted task action is invalid.", 500, "invalid_task_data")
            try:
                payload = (
                    ACTION_REGISTRY[action]
                    .schema.model_validate(proposal.parameters)
                    .model_dump(mode="json")
                )
            except ValidationError as exc:
                raise APIError(
                    "A persisted task action is invalid.",
                    500,
                    "invalid_task_data",
                ) from exc
            missing: list[str] = []
        else:
            payload, missing = validate_operation(action, values)
        status, result = _operation_status(db, item)
        if item in history:
            status = "superseded"
        operations.append(
            AgentTaskOperationResponse(
                action=action,
                status=status,
                values=payload if payload is not None else values,
                missing_fields=missing,
                fields=(
                    form_fields(db, action, values, organization_id=owner.organization_id)
                    if not action_id
                    and task.status == "collecting_information"
                    else []
                ),
                action_id=action_id,
                result=result,
            )
        )
    if task.status == "collecting_information":
        message = "Provide the missing information to continue. This task expires after 24 hours of inactivity."
    elif task.status == "awaiting_approval":
        message = "The proposal is awaiting an explicit approval decision and expires after seven days."
    elif task.status == "expired":
        message = "This task has expired and cannot be resumed for execution."
    elif task.status == "rejected":
        message = "The task was rejected."
    elif task.status == "cancelled":
        message = "The task was cancelled."
    elif task.status == "completed":
        message = "All task actions completed."
    elif task.status == "failed":
        message = "One or more task actions failed. Review execution history before retrying."
    else:
        message = "Task actions are executing."
    return AgentTaskResponse(
        task_id=task.task_id,
        status=task.status,
        expires_at=task.expires_at,
        operations=operations,
        message=message,
    )


def create_task(
    db: Session,
    request: AgentTaskStartRequest,
    *,
    user_id: int,
    organization_id: int,
) -> AgentTaskResponse:
    operations: list[dict[str, Any]] = []
    for operation in request.operations:
        if get_action_policy(db, operation.action, organization_id=organization_id) == "disabled":
            raise APIError(
                f"{operation.action} is disabled and cannot be proposed.",
                403,
                "action_disabled",
            )
        payload, missing = validate_operation(operation.action, operation.values)
        if payload is not None:
            _require_scheduling_availability(
                db, operation.action, payload, organization_id=organization_id
            )
        _validate_partial_targets(db, operation.values, organization_id=organization_id)
        operations.append(
            {
                "action": operation.action,
                "values": operation.values,
                "status": "collecting_information" if missing else "ready_for_review",
            }
        )
    task = pending_actions.create_task(
        db,
        user_id=user_id,
        action_type=request.operations[0].action if len(request.operations) == 1 else None,
        collected_data={"operations": operations},
    )
    if all(not item["status"] == "collecting_information" for item in operations):
        task = _finalize_if_complete(
            db, task, operations, user_id=user_id, organization_id=organization_id
        )
    return _response(db, task)


def submit_task_input(
    db: Session,
    task_id: str,
    request: AgentTaskInputRequest,
    *,
    user_id: int,
    organization_id: int,
) -> AgentTaskResponse:
    task = pending_actions.get_task(db, task_id, user_id=user_id)
    # get_task marks an overdue task expired, so report expiry before the status check.
    if task.status == "expired" or task_is_expired(task.expires_at):
        raise APIError("Task has expired.", 410, "task_expired")
    if task.status != "collecting_information":
        raise APIError(
            "Task is not collecting information.",
            409,
            "invalid_task_transition",
        )
    operations = _decode_operations(task.collected_data)
    if request.operation_index >= len(operations):
        raise APIError("Operation index is not part of this task.", 422, "invalid_task_input")
    item = operations[request.operation_index]
    if item.get("action_id"):
        raise APIError("This task operation is already finalized.", 409, "invalid_task_transition")
    action = item["action"]
    current_values = item.get("values")
    if not isinstance(current_values, dict):
        raise APIError("Persisted task values are invalid.", 500, "invalid_task_data")
    merged_values = {**current_values, **request.values}
    payload, missing = validate_operation(action, merged_values)
    _validate_partial_targets(db, merged_values, organization_id=organization_id)
    item["values"] = merged_values
    item["status"] = "collecting_information" if missing else "ready_for_review"
    task = pending_actions.update_task_inputs(
        db,
        task_id,
        user_id=user_id,
        collected_data={"operations": operations},
    )
    if all(
        validate_operation(operation["action"], operation["values"])[0] is not None
        for operation in operations
    ):
        try:
            task = _finalize_if_complete(
                db, task, operations, user_id=user_id, organization_id=organization_id
            )
        except APIError as exc:
            if exc.code in {"schedule_conflict", "schedule_in_past"}:
                db.rollback()
                pending_actions.update_task_inputs(
                    db,
                    task_id,
                    user_id=user_id,
                    collected_data={"operations": operations},
                )
            raise
    return _response(db, task)


def resume_task(
    db: Session,
    task_id: str,
    *,
    user_id: int,
    organization_id: int,
) -> AgentTaskResponse:
    task = pending_actions.get_task(db, task_id, user_id=user_id)
    # Superseded versions can never run again, so only current ones are revalidated.
    proposals, _ = _task_proposals(db, task_id)
    if task.status != "expired":
        for item in _decode_operations(task.collected_data, proposals=proposals):
            action = item.get("action")
            values = item.get("values")
            if action not in ACTION_REGISTRY or not isinstance(values, dict):
                raise APIError("Persisted task operations are invalid.", 500, "invalid_task_data")
            mode = get_action_policy(db, action, organization_id=organization_id)
            action_id = item.get("action_id")
            if not isinstance(action_id, str):
                _, missing = validate_operation(action, values)
                _validate_partial_targets(db, values, organization_id=organization_id)
                if not missing:
                    continue
                if mode == "disabled":
                    raise APIError(
                        "An action in this task is currently disabled.",
                        403,
                        "action_disabled",
                    )
                continue
            proposal = db.get(HitlActionProposal, action_id)
            if proposal is None:
                raise APIError("Task action is missing.", 500, "invalid_task_data")
            if proposal.action_type != action:
                raise APIError("A persisted task action is invalid.", 500, "invalid_task_data")
            if action_fingerprint(proposal.action_type, proposal.parameters) != (
                proposal.payload_fingerprint
            ):
                raise APIError(
                    "An action in this task has changed and is no longer valid.",
                    409,
                    "action_parameters_changed",
                )
            try:
                payload = (
                    ACTION_REGISTRY[action]
                    .schema.model_validate(proposal.parameters)
                    .model_dump(mode="json")
                )
            except ValidationError as exc:
                raise APIError(
                    "A persisted task action is invalid.",
                    500,
                    "invalid_task_data",
                ) from exc
            _validate_targets(
                db, [(proposal.action_type, payload)], organization_id=organization_id
            )
            if mode == "disabled" and proposal.status == "pending":
                raise APIError(
                    "An action in this task is currently disabled.",
                    403,
                    "action_disabled",
                )
    return _response(db, task)


def list_tasks(
    db: Session,
    *,
    user_id: int,
    organization_id: int,
    limit: int = 100,
) -> list[AgentTaskResponse]:
    tasks = list(
        db.scalars(
            select(HitlTask)
            .where(HitlTask.owner_user_id == user_id)
            .order_by(HitlTask.updated_at.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    )
    return [
        resume_task(db, task.task_id, user_id=user_id, organization_id=organization_id)
        for task in tasks
    ]


def cancel_task(
    db: Session,
    task_id: str,
    *,
    user_id: int,
) -> AgentTaskResponse:
    pending_actions.get_task(db, task_id, user_id=user_id)
    task = pending_actions.cancel_task(db, task_id, user_id=user_id)
    return _response(db, task)
