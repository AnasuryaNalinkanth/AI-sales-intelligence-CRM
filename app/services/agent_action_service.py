import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, TypeGuard
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Interrupt
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.actions import action_checkpointer
from app.agent.agent import build_sales_agent
from app.agent.model import create_chat_model, map_ai_provider_exception
from app.agent.tools import AgentToolContext
from app.agent.tools.action_schemas import (
    ApplyEnrichmentProposal,
    CloseOpportunityProposal,
    CompleteFollowupProposal,
    CreateFollowupProposal,
    CreateMeetingProposal,
    RecordCallResultProposal,
    ScheduleCallProposal,
    UpdateLeadQualificationProposal,
)
from app.core.config import settings
from app.core.exceptions import APIError
from app.database.connection import atomic_transaction
from app.core.logging_utils import (
    contains_sensitive_material,
    redact_sensitive_text,
)
from app.schemas.agent import (
    AgentActionName,
    AgentActionResult,
    AgentChatRequest,
    AgentChatResponse,
    AgentTaskInputRequest,
    AgentTaskResponse,
    AgentToolCall,
    PendingAgentAction,
)
from app.schemas.call import CallCreate, CallResponse
from app.schemas.customer import CustomerResponse
from app.schemas.follow_up import FollowUpCreate, FollowUpResponse
from app.schemas.meeting import MeetingCreate, MeetingResponse
from app.schemas.sales_enquiry import (
    LeadQualificationUpdate,
    OpportunityClose,
    SalesEnquiryResponse,
)
from app.models import (
    Contact,
    HitlActionProposal,
    HitlExecutionAudit,
    HitlTask,
    SalesEnquiry,
    User,
)
from app.services import (
    call_service,
    enquiry_service,
    enrichment_service,
    followup_service,
    meeting_service,
)
from app.services import calendar_sync_service
from app.services.activity_validation import require_customer, validate_customer_references
from app.schemas.validators import timezone_from_name
from app.services.scheduling_service import current_time, lock_workspace_schedule
from app.services.agent_action_state import (
    PendingActionRecord,
    action_fingerprint,
    authorize_decision,
    pending_actions,
)
from app.services.hitl_policy import enforce_action_policy, get_action_policy
from app.services.hitl_action_registry import validate_operation
from app.services.meeting_draft import (
    extract_draft_answers,
    load_meeting_draft,
    load_pending_meeting,
    meeting_followup_intent,
    unmatched_reference_questions,
)

logger = logging.getLogger(__name__)

WRITE_TOOL_NAMES: frozenset[AgentActionName] = frozenset(
    {
        "create_meeting",
        "create_followup",
        "schedule_call",
        "record_call_result",
        "complete_followup",
        "apply_enrichment",
        "update_lead_qualification",
        "close_opportunity",
    }
)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _is_agent_action(value: object) -> TypeGuard[AgentActionName]:
    return isinstance(value, str) and value in WRITE_TOOL_NAMES


def _validate_proposal(
    action: AgentActionName,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if action == "create_meeting":
        return CreateMeetingProposal.model_validate(payload).model_dump(mode="json")

    if action == "create_followup":
        return CreateFollowupProposal.model_validate(payload).model_dump(mode="json")

    if action == "schedule_call":
        return ScheduleCallProposal.model_validate(payload).model_dump(mode="json")

    if action == "record_call_result":
        return RecordCallResultProposal.model_validate(payload).model_dump(mode="json")

    if action == "apply_enrichment":
        return ApplyEnrichmentProposal.model_validate(payload).model_dump(mode="json")

    if action == "update_lead_qualification":
        return UpdateLeadQualificationProposal.model_validate(payload).model_dump(mode="json")

    if action == "close_opportunity":
        return CloseOpportunityProposal.model_validate(payload).model_dump(mode="json")

    return CompleteFollowupProposal.model_validate(payload).model_dump(mode="json")


def _proposal_payload(value: Any) -> tuple[AgentActionName, dict[str, Any]]:
    if not isinstance(value, dict):
        raise APIError(
            "The proposed action is invalid.",
            502,
            "invalid_action_proposal",
        )

    action = value.get("action")
    payload = value.get("payload")

    if not _is_agent_action(action) or not isinstance(payload, dict):
        raise APIError(
            "The proposed action is invalid.",
            502,
            "invalid_action_proposal",
        )

    try:
        validated_payload = _validate_proposal(action, payload)
    except ValidationError as exc:
        raise APIError(
            "The proposed action could not be validated.",
            422,
            "invalid_action_proposal",
        ) from exc

    return action, validated_payload


_CALENDAR_ACTIVITY_TYPES: dict[str, Literal["meeting", "call", "followup"]] = {
    "create_meeting": "meeting",
    "schedule_call": "call",
    "create_followup": "followup",
}


def _schedule_label(payload: dict[str, Any]) -> str:
    for at_key, zone_key in (
        ("scheduled_at", "scheduled_timezone"),
        ("due_date", "due_timezone"),
    ):
        starts_at, zone_name = payload.get(at_key), payload.get(zone_key)
        if not isinstance(starts_at, str) or not isinstance(zone_name, str):
            continue
        try:
            moment = datetime.fromisoformat(starts_at)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            local = moment.astimezone(timezone_from_name(zone_name))
        except (TypeError, ValueError):
            return ""
        offset = local.strftime("%z")
        return f" for {local:%Y-%m-%d %H:%M} {zone_name} (UTC{offset[:3]}:{offset[3:]})"
    return ""


def _meeting_clarification_question(missing_fields: list[str]) -> str:
    missing = set(missing_fields)
    if "valid_scheduled_time" in missing:
        return (
            "That local time does not exist because of a daylight-saving change. "
            "What valid time should I use?"
        )
    if "scheduled_time_occurrence" in missing:
        return (
            "That local time occurs twice because of a daylight-saving change. "
            "Should I use the earlier or later occurrence?"
        )
    date_missing = "scheduled_date" in missing
    time_missing = "scheduled_time" in missing
    timezone_missing = "scheduled_timezone" in missing
    if date_missing and time_missing:
        question = "Sure — what date and time would you like to schedule the meeting?"
    elif date_missing:
        question = "What date would you like to schedule the meeting?"
    elif time_missing:
        question = "What time would you like to schedule the meeting?"
    else:
        question = "What timezone should I use for the meeting?"
    if timezone_missing:
        question = question[:-1] + ", and what timezone should I use?"
    return question


def _draft_question(
    db: Session,
    values: dict[str, Any],
    missing: list[str],
    *,
    organization_id: int,
    extra: list[str] | None = None,
) -> str:
    """One message: missing schedule details first, then every unresolved contact or enquiry."""
    schedule = [field for field in missing if field not in {"contact_id", "enquiry_id"}]
    parts = [_meeting_clarification_question(schedule)] if schedule else []
    for question in [
        *(extra or []),
        *unmatched_reference_questions(db, values, organization_id=organization_id),
    ]:
        if question not in parts:
            parts.append(question)
    return " ".join(parts)


def _validate_proposal_customer_scope(
    db: Session,
    *,
    request_customer_id: int | None,
    tool_context: AgentToolContext,
    payload: dict[str, Any],
) -> None:
    target_customer_id = payload.get("customer_id")

    if not isinstance(target_customer_id, int):
        raise APIError(
            "The proposed action is invalid.",
            422,
            "invalid_action_proposal",
        )

    if (
        request_customer_id is not None
        and target_customer_id != request_customer_id
    ):
        raise APIError(
            "Write proposal cannot target a customer outside the active conversation scope.",
            403,
            "customer_scope_violation",
        )

    if not tool_context.has_customer(target_customer_id):
        raise APIError(
            "Use CRM read tools to identify this customer before proposing an action.",
            400,
            "record_not_retrieved",
        )

    contact_id = payload.get("contact_id")
    enquiry_id = payload.get("enquiry_id")
    meeting_id = payload.get("meeting_id")
    call_id = payload.get("call_id")
    followup_id = payload.get("followup_id")

    for record_id, has_fn, label in (
        (contact_id, tool_context.has_contact, "contact"),
        (enquiry_id, tool_context.has_enquiry, "enquiry"),
        (meeting_id, tool_context.has_meeting, "meeting"),
        (call_id, tool_context.has_call, "call"),
        (followup_id, tool_context.has_followup, "followup"),
    ):
        if record_id is not None and not has_fn(
            target_customer_id,
            int(record_id),
        ):
            raise APIError(
                f"Use CRM read tools to identify this {label} before proposing an action.",
                400,
                "record_not_retrieved",
            )

    validate_customer_references(
        db,
        target_customer_id,
        organization_id=tool_context.organization_id,
        contact_id=contact_id,
        enquiry_id=enquiry_id,
        meeting_id=meeting_id,
        call_id=call_id,
        followup_id=followup_id,
    )


def _extract_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        # Replies before the latest user message are history, never this run's answer.
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and not message.tool_calls:
            content = message.content

            if isinstance(content, str) and content.strip():
                return content.strip()

            if isinstance(content, list):
                text_parts: list[str] = []

                for item in content:
                    if isinstance(item, str):
                        text_parts.append(item)
                    elif isinstance(item, dict) and isinstance(
                        item.get("text"),
                        str,
                    ):
                        text_parts.append(item["text"])

                text = "\n".join(text_parts).strip()

                if text:
                    return text

    return "I could not complete that request."


# First-person or "successfully" claims that a CRM change happened. Only reached
# when this request created no proposal and executed no action. "We have scheduled"
# and "has been booked" are left out: they also describe existing records.
_UNCONFIRMED_CHANGE_CLAIM = re.compile(
    r"\bI(?:['’]ve|\s+have)?\s+(?:successfully\s+|now\s+|just\s+)?"
    r"(?:scheduled|created|booked|saved|added|updated|logged|recorded|completed|"
    r"approved|synced|set up)\b"
    r"|\bsuccessfully\s+(?:scheduled|created|booked|saved|added|updated|logged|"
    r"recorded|completed|approved|synced)\b"
    r"|\b(?:has|have)\s+been\s+(?:successfully\s+)?(?:saved|approved|synced)\b",
    re.IGNORECASE,
)
_NO_CHANGE_MESSAGE = (
    "No CRM change was made in this request. If you want me to schedule or update "
    "something, ask again and I will prepare it for your approval."
)


def _without_unconfirmed_change_claim(text: str) -> str:
    return _NO_CHANGE_MESSAGE if _UNCONFIRMED_CHANGE_CLAIM.search(text) else text


def _read_tool_calls(messages: list[Any]) -> list[AgentToolCall]:
    results_by_id = {
        message.tool_call_id: message
        for message in messages
        if isinstance(message, ToolMessage)
    }

    calls: list[AgentToolCall] = []

    for message in messages:
        for call in getattr(message, "tool_calls", []):
            name = call.get("name", "unknown")

            if name in WRITE_TOOL_NAMES:
                continue

            result = results_by_id.get(call.get("id"))
            error_code = None

            if result is not None and isinstance(result.content, str):
                try:
                    data = json.loads(result.content)
                except json.JSONDecodeError:
                    data = {}

                error = data.get("error") if isinstance(data, dict) else None

                if isinstance(error, dict) and isinstance(
                    error.get("code"),
                    str,
                ):
                    error_code = error["code"]

            args = call.get("args")
            safe_args = args if isinstance(args, dict) else {}

            summary = {
                key: value
                for key, value in safe_args.items()
                if isinstance(value, (str, int))
            }

            calls.append(
                AgentToolCall(
                    name=name,
                    input_summary=summary,
                    success=error_code is None,
                    error_code=error_code,
                )
            )

    return calls


def _submit_meeting_draft(
    db: Session,
    task_id: str,
    values: dict[str, Any],
    *,
    request: AgentChatRequest,
    user_id: int,
    organization_id: int,
    tool_calls: list[AgentToolCall],
    questions: list[str] | None = None,
) -> AgentChatResponse:
    """Merge values into the user's draft task; report its proposal or what is still needed."""
    from app.services.hitl_task_service import (
        _response as build_task_response,
        _validate_partial_targets,
        submit_task_input,
    )

    if questions:
        # Something is ambiguous: keep what is clear, but never finalize yet.
        task = pending_actions.get_task(db, task_id, user_id=user_id)
        merged = {**task.collected_data["operations"][0]["values"], **values}
        _, missing = validate_operation("create_meeting", merged)
        _validate_partial_targets(db, merged, organization_id=organization_id)
        task = pending_actions.update_task_inputs(
            db,
            task_id,
            user_id=user_id,
            collected_data={
                "operations": [
                    {
                        "action": "create_meeting",
                        "values": merged,
                        "status": "collecting_information",
                    }
                ]
            },
        )
        return AgentChatResponse(
            response=_draft_question(
                db, merged, missing, organization_id=organization_id, extra=questions
            ),
            customer_id=request.customer_id,
            tool_calls=tool_calls,
            clarification_task=build_task_response(db, task),
        )
    try:
        task_response = submit_task_input(
            db,
            task_id,
            AgentTaskInputRequest(operation_index=0, values=values),
            user_id=user_id,
            organization_id=organization_id,
        )
    except APIError as exc:
        if exc.code not in {"schedule_conflict", "schedule_in_past"}:
            raise
        # submit_task_input kept the answers on the still-open draft.
        suggestions = (exc.details or {}).get("suggestions")
        return AgentChatResponse(
            response=_action_failure_message(exc),
            customer_id=request.customer_id,
            tool_calls=tool_calls,
            clarification_task=build_task_response(
                db, pending_actions.get_task(db, task_id, user_id=user_id)
            ),
            schedule_alternatives=suggestions if isinstance(suggestions, list) else [],
            schedule_reason="past" if exc.code == "schedule_in_past" else "conflict",
        )
    proposal = db.scalar(
        select(HitlActionProposal).where(
            HitlActionProposal.task_id == task_id,
            HitlActionProposal.status == "pending",
        )
    )
    if proposal is not None:
        return AgentChatResponse(
            response=(
                f"I am ready to create meeting{_schedule_label(proposal.parameters)}. "
                "Please confirm."
            ),
            customer_id=request.customer_id,
            tool_calls=tool_calls,
            pending_action=PendingAgentAction(
                action_id=proposal.action_id,
                action="create_meeting",
                payload=proposal.parameters,
            ),
        )
    operation = task_response.operations[0]
    return AgentChatResponse(
        response=_draft_question(
            db, operation.values, operation.missing_fields, organization_id=organization_id
        )
        or task_response.message,
        customer_id=request.customer_id,
        tool_calls=tool_calls,
        clarification_task=task_response,
    )


def _pending_action(proposal: HitlActionProposal) -> PendingAgentAction:
    return PendingAgentAction(
        action_id=proposal.action_id,
        action="create_meeting",
        payload=proposal.parameters,
    )


def _meeting_summary(db: Session, payload: dict[str, Any], *, organization_id: int) -> str:
    """Every detail of a meeting proposal, named from this organization's records."""
    parts = [f"Meeting{_schedule_label(payload)}"]
    for model, field, label, name in (
        (Contact, "contact_id", "contact", "name"),
        (SalesEnquiry, "enquiry_id", "sales enquiry", "product"),
    ):
        record_id = payload.get(field)
        record = (
            db.scalar(
                select(model).where(
                    model.organization_id == organization_id,
                    getattr(model, field) == record_id,
                )
            )
            if record_id is not None
            else None
        )
        parts.append(
            f"{label} #{record_id} {getattr(record, name)}" if record is not None else f"no {label}"
        )
    parts.append(f"{payload['duration']} minutes" if payload.get("duration") else "default duration")
    if payload.get("agenda"):
        parts.append(f"agenda: {payload['agenda']}")
    if payload.get("notes"):
        parts.append(f"notes: {payload['notes']}")
    return "; ".join(parts) + "."


_UNRESOLVED_NAMES = {"contact_name": "contact_id", "enquiry_reference": "enquiry_id"}
_CHOOSE_INTENT = (
    'Say "change it to ..." to update the pending meeting, or "schedule another meeting ..." '
    "to keep it and add a separate one."
)


def _amend_pending_meeting(
    db: Session,
    proposal: HitlActionProposal,
    task_values: dict[str, Any],
    *,
    request: AgentChatRequest,
    user_id: int,
    organization_id: int,
    tool_calls: list[AgentToolCall],
    questions: list[str] | None = None,
    intent: str | None = None,
) -> AgentChatResponse:
    """Replace the pending meeting proposal with its revision, after the same checks.

    Details that resolved are applied even while a contact or enquiry name still needs
    a choice; that name is kept on the task and asked about, and until it is settled the
    proposal keeps its current contact or enquiry.
    """
    from app.services.hitl_task_service import _require_scheduling_availability

    unresolved = {
        name: task_values[name] for name in _UNRESOLVED_NAMES if task_values.get(name)
    }
    values = {key: value for key, value in task_values.items() if key not in _UNRESOLVED_NAMES}
    for name in unresolved:
        values[_UNRESOLVED_NAMES[name]] = proposal.parameters.get(_UNRESOLVED_NAMES[name])
    payload, missing = validate_operation("create_meeting", values)
    if payload is None:
        return AgentChatResponse(
            response=_draft_question(db, task_values, missing, organization_id=organization_id)
            + " Your pending meeting proposal is unchanged.",
            customer_id=request.customer_id,
            tool_calls=tool_calls,
            pending_action=_pending_action(proposal),
        )
    if payload.get("customer_id") != request.customer_id:
        raise APIError(
            "Write proposal cannot target a customer outside the active conversation scope.",
            403,
            "customer_scope_violation",
        )
    validate_customer_references(
        db,
        payload["customer_id"],
        organization_id=organization_id,
        contact_id=payload.get("contact_id"),
        enquiry_id=payload.get("enquiry_id"),
    )
    enforce_action_policy(db, "create_meeting", organization_id=organization_id)
    current = CreateMeetingProposal.model_validate(proposal.parameters)
    revised = CreateMeetingProposal.model_validate(payload)
    if intent != "amend" and (
        _as_utc(revised.scheduled_at) != _as_utc(current.scheduled_at)
        or revised.scheduled_timezone != current.scheduled_timezone
    ):
        # Moving a pending meeting needs a clear request; it may be a second meeting.
        return AgentChatResponse(
            response=(
                f"Your pending meeting is{_schedule_label(proposal.parameters)}. Do you want to "
                f"move it to {_schedule_label(payload).removeprefix(' for ')}, or schedule a "
                f"separate meeting? {_CHOOSE_INTENT} Your pending meeting proposal is unchanged."
            ),
            customer_id=request.customer_id,
            tool_calls=tool_calls,
            pending_action=_pending_action(proposal),
        )
    try:
        _require_scheduling_availability(
            db, "create_meeting", payload, organization_id=organization_id
        )
    except APIError as exc:
        if exc.code not in {"schedule_conflict", "schedule_in_past"}:
            raise
        return AgentChatResponse(
            response=f"{_action_failure_message(exc)} Your pending meeting proposal is unchanged.",
            customer_id=request.customer_id,
            tool_calls=tool_calls,
            pending_action=_pending_action(proposal),
        )
    record = pending_actions.supersede(
        db, proposal.action_id, payload, user_id=user_id, unresolved=unresolved
    )
    summary = _meeting_summary(db, record.payload, organization_id=organization_id)
    changed = record.action_id != proposal.action_id
    question = (
        _draft_question(db, task_values, [], organization_id=organization_id, extra=questions)
        if unresolved or questions
        else ""
    )
    if question:
        head = "I updated the meeting proposal." if changed else "Your pending meeting proposal is unchanged."
        response = (
            f"{head} {summary} {question} You can approve this version now, or answer "
            "and I will revise it."
        )
    elif changed:
        response = f"I updated the meeting proposal. {summary} Please confirm this revised version."
    else:
        response = f"The pending meeting proposal already has these details. {summary} Please confirm."
    return AgentChatResponse(
        response=response,
        customer_id=request.customer_id,
        tool_calls=tool_calls,
        pending_action=PendingAgentAction(
            action_id=record.action_id,
            action="create_meeting",
            payload=record.payload,
        ),
    )


def propose_or_answer(
    db: Session,
    request: AgentChatRequest,
    *,
    model: BaseChatModel | None = None,
    user_id: int = 0,
    organization_id: int,
    history: list[dict[str, str]] | None = None,
) -> AgentChatResponse:
    tool_context = AgentToolContext(
        organization_id=organization_id,
        locked_customer_id=request.customer_id,
        timezone_name=request.timezone_name,
        request_message=request.message,
    )

    if request.customer_id is not None:
        # The client-selected customer counts as retrieved only once it is verified here.
        require_customer(db, request.customer_id, organization_id=organization_id)
        tool_context.remember_customer(request.customer_id)

    draft_task: HitlTask | None = None
    draft_values: dict[str, Any] = {}
    if request.task_id is not None:
        draft_task, draft_values = load_meeting_draft(
            db,
            str(request.task_id),
            user_id=user_id,
            organization_id=organization_id,
            customer_id=request.customer_id,
        )
        tool_context.meeting_draft = draft_values
        # load_meeting_draft rechecked these against this customer and organization.
        if draft_values.get("contact_id") is not None:
            tool_context.remember_contact(request.customer_id, draft_values["contact_id"])
        if draft_values.get("enquiry_id") is not None:
            tool_context.remember_enquiry(request.customer_id, draft_values["enquiry_id"])

    amend_target: HitlActionProposal | None = None
    # The pending proposal a separate new meeting leaves untouched.
    kept_pending: HitlActionProposal | None = None
    followup_intent = (
        meeting_followup_intent(request.message) if request.pending_action_id is not None else None
    )
    if request.pending_action_id is not None:
        # Always authorized first, whatever the message turns out to ask for.
        amend_target, draft_values = load_pending_meeting(
            db,
            request.pending_action_id,
            user_id=user_id,
            organization_id=organization_id,
            customer_id=request.customer_id,
        )
        if followup_intent == "unclear":
            return AgentChatResponse(
                response=(
                    "Do you want to change your pending meeting, or schedule a separate one? "
                    f"{_CHOOSE_INTENT} Your pending meeting proposal is unchanged."
                ),
                customer_id=request.customer_id,
                tool_calls=[],
                pending_action=_pending_action(amend_target),
            )
        if followup_intent == "new":
            kept_pending, amend_target, draft_values = amend_target, None, {}
    if amend_target is not None:
        tool_context.meeting_draft = draft_values
        # load_pending_meeting rechecked these against this customer and organization.
        if draft_values.get("contact_id") is not None:
            tool_context.remember_contact(request.customer_id, draft_values["contact_id"])
        if draft_values.get("enquiry_id") is not None:
            tool_context.remember_enquiry(request.customer_id, draft_values["enquiry_id"])

    prompt = request.message

    if request.customer_id is not None:
        prompt = (
            f"Selected CRM customer ID (use read tools to retrieve its facts): "
            f"{request.customer_id}\nUser request: {request.message}"
        )
    if draft_task is not None:
        _, draft_missing = validate_operation("create_meeting", draft_values)
        prompt = (
            "Open meeting draft with details the user already gave (validated): "
            f"{json.dumps(draft_values)}. Still missing: {', '.join(draft_missing) or 'nothing'}. "
            "Call create_meeting with only the new details; the draft supplies the rest.\n"
            f"{prompt}"
        )
    if kept_pending is not None:
        prompt = (
            "The user has another meeting proposal awaiting approval; it stays as it is. "
            "This message asks for a separate meeting, so propose it on its own.\n"
            f"{prompt}"
        )
    if amend_target is not None:
        prompt = (
            "Pending meeting proposal awaiting the user's approval (validated): "
            f"{json.dumps(draft_values)}. If the user adds or changes meeting details, call "
            "create_meeting with only those details; the proposal keeps the rest and the "
            "revised version needs a new approval.\n"
            f"{prompt}"
        )
    if request.timezone_name is not None:
        prompt = f"User's timezone (IANA): {request.timezone_name}\n{prompt}"
    # Stated explicitly so the model cannot claim to remember messages it was never given.
    prompt = (
        f"Earlier messages in this conversation: {len(history)} (shown above).\n{prompt}"
        if history
        else f"Earlier messages in this conversation: none. This is its first message.\n{prompt}"
    )
    # Lets the model tell past records from upcoming ones without guessing today's date.
    prompt = f"Current time (UTC): {current_time().isoformat(timespec='minutes')}\n{prompt}"

    thread_id = str(uuid4())

    try:
        agent = build_sales_agent(
            db,
            model=model or create_chat_model(),
            allow_actions=True,
            tool_context=tool_context,
        )

        try:
            logger.info(
                "Starting agent request [thread_id=%s customer_id=%s message_chars=%s]",
                thread_id,
                request.customer_id,
                len(request.message),
            )

            state = agent.invoke(
                {
                    # Earlier turns of this user's conversation for this customer only.
                    "messages": [
                        *(history or []),
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ]
                },
                config={
                    "configurable": {"thread_id": thread_id},
                    "recursion_limit": settings.agent_recursion_limit,
                },
            )

            logger.info(
                "Agent completed [thread_id=%s messages=%s interrupts=%s]",
                thread_id,
                len(state.get("messages", [])),
                len(state.get("__interrupt__", [])),
            )
        finally:
            # Immediately clean up the LangGraph checkpoint thread after
            # extracting state; confirmation executes validated CRM service
            # calls directly rather than resuming the graph.
            action_checkpointer.delete_thread(thread_id)

        interrupts = state.get("__interrupt__", [])

        if interrupts:
            if (
                len(interrupts) != 1
                or not isinstance(interrupts[0], Interrupt)
            ):
                raise APIError(
                    "Please propose one CRM action at a time.",
                    409,
                    "multiple_pending_actions",
                )

            interrupt_value = interrupts[0].value
            if (
                isinstance(interrupt_value, dict)
                and interrupt_value.get("clarification") is True
            ):
                action = interrupt_value.get("action")
                payload = interrupt_value.get("payload")
                if action != "create_meeting" or not isinstance(payload, dict):
                    raise APIError(
                        "The meeting clarification request is invalid.",
                        502,
                        "invalid_action_proposal",
                    )
                _validate_proposal_customer_scope(
                    db,
                    request_customer_id=request.customer_id,
                    tool_context=tool_context,
                    payload=payload,
                )
                enforce_action_policy(db, action, organization_id=organization_id)
                _, missing_fields = validate_operation(action, payload)
                raw_questions = interrupt_value.get("questions")
                questions = [
                    item for item in raw_questions or [] if isinstance(item, str)
                ] if isinstance(raw_questions, list) else []
                if not missing_fields and not questions:
                    raise APIError(
                        "A complete meeting must use the approval proposal flow.",
                        502,
                        "invalid_action_proposal",
                    )
                if amend_target is not None:
                    lead = interrupt_value.get("question")
                    return _amend_pending_meeting(
                        db,
                        amend_target,
                        # The proposal supplies any schedule detail this reply left unclear.
                        {**draft_values, **payload},
                        request=request,
                        user_id=user_id,
                        organization_id=organization_id,
                        tool_calls=_read_tool_calls(state.get("messages", [])),
                        questions=[*([lead] if isinstance(lead, str) else []), *questions],
                        intent=followup_intent,
                    )
                collected_data: dict[str, object] = {
                    "operations": [
                        {
                            "action": action,
                            "values": payload,
                            "status": "collecting_information",
                        }
                    ]
                }
                # A resumed draft is updated in place so no second open task is left behind.
                task = (
                    pending_actions.update_task_inputs(
                        db,
                        draft_task.task_id,
                        user_id=user_id,
                        collected_data=collected_data,
                    )
                    if draft_task is not None
                    else pending_actions.create_task(
                        db,
                        user_id=user_id,
                        action_type=action,
                        collected_data=collected_data,
                    )
                )
                from app.services.hitl_task_service import _response as build_task_response

                clarification_task: AgentTaskResponse = build_task_response(db, task)
                # An AM/PM question replaces the generic schedule question.
                lead = (
                    interrupt_value["question"]
                    if isinstance(interrupt_value.get("question"), str)
                    else None
                )
                follow_up = _draft_question(
                    db,
                    payload,
                    [] if lead else missing_fields,
                    organization_id=organization_id,
                    extra=questions,
                )
                return AgentChatResponse(
                    response=" ".join(part for part in (lead, follow_up) if part),
                    customer_id=request.customer_id,
                    tool_calls=_read_tool_calls(state.get("messages", [])),
                    clarification_task=clarification_task,
                )

            action, payload = _proposal_payload(interrupt_value)

            _validate_proposal_customer_scope(
                db,
                request_customer_id=request.customer_id,
                tool_context=tool_context,
                payload=payload,
            )

            enforce_action_policy(db, action, organization_id=organization_id)
            task_values = interrupt_value.get("task_values")
            if amend_target is not None and action == "create_meeting" and isinstance(task_values, dict):
                return _amend_pending_meeting(
                    db,
                    amend_target,
                    task_values,
                    request=request,
                    user_id=user_id,
                    organization_id=organization_id,
                    tool_calls=_read_tool_calls(state.get("messages", [])),
                    intent=followup_intent,
                )
            if draft_task is not None and action == "create_meeting" and isinstance(task_values, dict):
                # The draft's own task becomes the proposal, through the task flow's checks.
                return _submit_meeting_draft(
                    db,
                    draft_task.task_id,
                    task_values,
                    request=request,
                    user_id=user_id,
                    organization_id=organization_id,
                    tool_calls=_read_tool_calls(state.get("messages", [])),
                )
            if (
                action != "create_meeting"
                and get_action_policy(db, action, organization_id=organization_id) == "automatic"
            ):
                pending = pending_actions.add(
                    db,
                    action,
                    payload,
                    thread_id,
                    user_id=user_id,
                    start_execution=True,
                )
                result = _run_claimed_action(
                    db,
                    pending,
                    require_automatic=True,
                )
                return AgentChatResponse(
                    response=result.message,
                    customer_id=request.customer_id,
                    tool_calls=_read_tool_calls(state.get("messages", [])),
                )

            # Repeating the same request (e.g. "5:50 PM." again) reuses the identical
            # pending proposal instead of queueing a duplicate for approval.
            duplicate = db.scalar(
                select(HitlActionProposal).where(
                    HitlActionProposal.owner_user_id == user_id,
                    HitlActionProposal.status == "pending",
                    HitlActionProposal.payload_fingerprint == action_fingerprint(action, payload),
                )
            )
            pending = (
                duplicate
                if duplicate is not None and _as_utc(duplicate.expires_at) > datetime.now(timezone.utc)
                else pending_actions.add(
                    db,
                    action,
                    payload,
                    thread_id,
                    user_id=user_id,
                )
            )

            still_pending = (
                " Your earlier meeting proposal is still awaiting approval under Agent approvals."
                if kept_pending is not None and kept_pending.action_id != pending.action_id
                else ""
            )
            return AgentChatResponse(
                response=(
                    f"I am ready to {action.replace('_', ' ')}"
                    f"{_schedule_label(payload)}. Please confirm.{still_pending}"
                ),
                customer_id=request.customer_id,
                tool_calls=_read_tool_calls(
                    state.get("messages", [])
                ),
                pending_action=PendingAgentAction(
                    action_id=pending.action_id,
                    action=action,
                    payload=payload,
                ),
            )

        messages = state.get("messages", [])
        conflict = tool_context.schedule_conflict
        if amend_target is not None and conflict is not None:
            options = [
                f"{item['starts_at']} ({item['timezone']})"
                for item in conflict.get("suggestions") or []
                if isinstance(item, dict) and isinstance(item.get("starts_at"), str)
            ]
            reason = (
                "That time has already passed."
                if conflict.get("reason") == "past"
                else "That time overlaps an existing scheduled activity."
            )
            return AgentChatResponse(
                response=(
                    f"{reason} Your pending meeting proposal is unchanged."
                    + (f" Available alternatives: {'; '.join(options)}." if options else "")
                ),
                customer_id=request.customer_id,
                tool_calls=_read_tool_calls(messages),
                pending_action=_pending_action(amend_target),
            )
        if (
            conflict is not None
            and get_action_policy(db, conflict["action"], organization_id=organization_id)
            != "disabled"
        ):
            # Keep the conflicting request as an editable task; choosing an
            # alternative resubmits it through the task flow, which rechecks
            # availability before any proposal is created.
            _validate_proposal_customer_scope(
                db,
                request_customer_id=request.customer_id,
                tool_context=tool_context,
                payload=conflict["values"],
            )
            conflict_data: dict[str, object] = {
                "operations": [
                    {
                        "action": conflict["action"],
                        "values": conflict["values"],
                        "status": "collecting_information",
                    }
                ]
            }
            task = (
                pending_actions.update_task_inputs(
                    db, draft_task.task_id, user_id=user_id, collected_data=conflict_data
                )
                if draft_task is not None and conflict["action"] == "create_meeting"
                else pending_actions.create_task(
                    db,
                    user_id=user_id,
                    action_type=conflict["action"],
                    collected_data=conflict_data,
                )
            )
            from app.services.hitl_task_service import _response as build_task_response

            return AgentChatResponse(
                # Deterministic text from the availability result, so no invented slots.
                response=conflict.get("message") or _extract_text(messages),
                customer_id=request.customer_id,
                tool_calls=_read_tool_calls(messages),
                clarification_task=build_task_response(db, task),
                schedule_alternatives=conflict["suggestions"],
                schedule_reason=conflict.get("reason", "conflict"),
            )

        if amend_target is not None and request.customer_id is not None:
            answers, questions = extract_draft_answers(
                db,
                request.message,
                draft_values,
                organization_id=organization_id,
                customer_id=request.customer_id,
                timezone_name=request.timezone_name,
            )
            if answers or questions:
                return _amend_pending_meeting(
                    db,
                    amend_target,
                    {**draft_values, **answers},
                    request=request,
                    user_id=user_id,
                    organization_id=organization_id,
                    tool_calls=_read_tool_calls(messages),
                    questions=questions,
                    intent=followup_intent,
                )
            return AgentChatResponse(
                response=_without_unconfirmed_change_claim(_extract_text(messages)),
                customer_id=request.customer_id,
                tool_calls=_read_tool_calls(messages),
                pending_action=_pending_action(amend_target),
            )

        if draft_task is not None and request.customer_id is not None:
            # The model made no meeting tool call; answers this message clearly states
            # still reach the draft, through the same task validation and approval.
            answers, questions = extract_draft_answers(
                db,
                request.message,
                draft_values,
                organization_id=organization_id,
                customer_id=request.customer_id,
                timezone_name=request.timezone_name,
            )
            if answers or questions:
                return _submit_meeting_draft(
                    db,
                    draft_task.task_id,
                    answers,
                    request=request,
                    user_id=user_id,
                    organization_id=organization_id,
                    tool_calls=_read_tool_calls(messages),
                    questions=questions,
                )
            from app.services.hitl_task_service import _response as build_task_response

            return AgentChatResponse(
                response=_without_unconfirmed_change_claim(_extract_text(messages)),
                customer_id=request.customer_id,
                tool_calls=_read_tool_calls(messages),
                # Still open, so the next message can keep completing it.
                clarification_task=build_task_response(db, draft_task),
            )

        return AgentChatResponse(
            response=_without_unconfirmed_change_claim(_extract_text(messages)),
            customer_id=request.customer_id,
            tool_calls=_read_tool_calls(messages),
            # A separate request that produced nothing leaves the earlier proposal shown.
            pending_action=_pending_action(kept_pending) if kept_pending is not None else None,
        )

    except APIError:
        raise

    except GraphRecursionError as exc:
        # The graph stopped before any proposal or task was persisted.
        logger.warning(
            "Agent step limit reached [operation=agent_chat thread_id=%s "
            "recursion_limit=%s customer_id=%s]",
            thread_id,
            settings.agent_recursion_limit,
            request.customer_id,
        )
        raise APIError(
            "The AI assistant could not finish this request. No CRM changes were "
            "made. Please try again with the customer, date, and time in one message.",
            502,
            "agent_step_limit",
        ) from exc

    except Exception as exc:
        raise map_ai_provider_exception(
            exc,
            operation="agent_chat",
            default_code="agent_error",
            default_message="The AI assistant could not complete this request.",
        ) from exc


def _execute_action(
    db: Session,
    record: PendingActionRecord,
    *,
    require_automatic: bool = False,
) -> AgentActionResult:
    proposal_row = db.scalar(
        select(HitlActionProposal)
        .where(HitlActionProposal.action_id == record.action_id)
        .with_for_update()
    )
    actor = db.get(User, record.user_id)
    if proposal_row is None or actor is None or not actor.is_active:
        raise APIError(
            "The action owner is no longer authorized to execute this action.",
            403,
            "forbidden",
        )
    owner = db.get(User, proposal_row.owner_user_id)
    if owner is None or not owner.is_active:
        raise APIError(
            "The action owner is no longer authorized to execute this action.",
            403,
            "forbidden",
        )
    # Rechecked at execution: the approver must still share the owner's organization.
    if actor.organization_id != owner.organization_id:
        raise APIError("Pending action not found.", 404, "action_not_found")
    if proposal_row.owner_user_id != record.user_id and actor.role != "admin":
        raise APIError(
            "You are not authorized to execute this pending action.",
            403,
            "forbidden",
        )
    # Every target is checked against the owner's organization, never the payload's.
    organization_id = owner.organization_id
    expires_at = proposal_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise APIError("Pending action has expired.", 410, "action_expired")
    if (
        proposal_row.status != "executing"
        or proposal_row.action_type != record.action
        or proposal_row.payload_fingerprint != record.payload_fingerprint
        or action_fingerprint(
            proposal_row.action_type,
            proposal_row.parameters,
        )
        != record.payload_fingerprint
    ):
        raise APIError(
            "The action parameters or state changed and can no longer be executed.",
            409,
            "action_parameters_changed",
        )

    policy = get_action_policy(db, record.action, organization_id=organization_id, for_update=True)
    if policy == "disabled":
        raise APIError(
            "This action is disabled by the global autonomy policy and cannot execute.",
            403,
            "action_disabled",
        )
    if require_automatic and policy != "automatic":
        raise APIError(
            "The action policy changed before automatic execution could begin.",
            409,
            "action_policy_changed",
        )
    if action_fingerprint(record.action, record.payload) != record.payload_fingerprint:
        raise APIError(
            "The approved action parameters have changed and can no longer be executed.",
            409,
            "action_parameters_changed",
        )
    try:
        validated_payload = _validate_proposal(record.action, record.payload)
    except ValidationError as exc:
        raise APIError(
            "The action parameters are no longer valid.",
            422,
            "invalid_action_proposal",
        ) from exc
    if action_fingerprint(record.action, validated_payload) != record.payload_fingerprint:
        raise APIError(
            "The approved action parameters have changed and can no longer be executed.",
            409,
            "action_parameters_changed",
        )
    record = PendingActionRecord(
        action_id=record.action_id,
        action=record.action,
        payload=validated_payload,
        task_id=record.task_id,
        expires_at=record.expires_at,
        user_id=record.user_id,
        payload_fingerprint=record.payload_fingerprint,
    )

    customer_id = record.payload.get("customer_id")
    if not isinstance(customer_id, int):
        raise APIError("The action target is invalid.", 422, "invalid_action_proposal")
    if record.action in {"create_meeting", "create_followup", "schedule_call"}:
        lock_workspace_schedule(db, organization_id)
    validate_customer_references(
        db,
        customer_id,
        organization_id=organization_id,
        contact_id=record.payload.get("contact_id"),
        enquiry_id=record.payload.get("enquiry_id"),
        meeting_id=record.payload.get("meeting_id"),
        call_id=record.payload.get("call_id"),
        followup_id=record.payload.get("followup_id"),
    )
    if record.action == "schedule_call":
        proposal = ScheduleCallProposal.model_validate(record.payload)
        call_data = CallCreate.model_validate(proposal.model_dump())
        call = call_service.create_call(
            db,
            call_data,
            organization_id=organization_id,
            defer_commit=True,
        )
        serialized = CallResponse.model_validate(call).model_dump(mode="json")
        record_id = call.call_id
        message = "Scheduled call created successfully."

    elif record.action == "create_meeting":
        proposal = CreateMeetingProposal.model_validate(record.payload)

        meeting_data = MeetingCreate.model_validate(
            proposal.model_dump()
        )

        meeting = meeting_service.create_meeting(
            db,
            meeting_data,
            organization_id=organization_id,
            defer_commit=True,
        )

        serialized = MeetingResponse.model_validate(
            meeting
        ).model_dump(mode="json")

        record_id = meeting.meeting_id
        message = "Meeting created successfully."

    elif record.action == "create_followup":
        proposal = CreateFollowupProposal.model_validate(record.payload)

        followup_data = FollowUpCreate.model_validate(
            proposal.model_dump()
        )

        followup = followup_service.create_followup(
            db,
            followup_data,
            organization_id=organization_id,
            defer_commit=True,
        )

        serialized = FollowUpResponse.model_validate(
            followup
        ).model_dump(mode="json")

        record_id = followup.followup_id
        message = "Follow-up created successfully."

    elif record.action == "record_call_result":
        proposal = RecordCallResultProposal.model_validate(record.payload)

        call_data = CallCreate.model_validate(
            proposal.model_dump()
        )

        call = call_service.create_call(
            db,
            call_data,
            organization_id=organization_id,
            defer_commit=True,
        )

        serialized = CallResponse.model_validate(
            call
        ).model_dump(mode="json")

        record_id = call.call_id
        message = "Call result recorded successfully. No call was placed."

    elif record.action == "apply_enrichment":
        proposal = ApplyEnrichmentProposal.model_validate(
            record.payload
        )

        customer_updates: dict[str, object] = {}
        enquiry_updates: dict[str, object] = {}

        if proposal.sales_stage is not None:
            customer_updates["sales_stage"] = proposal.sales_stage

        if proposal.customer_status is not None:
            customer_updates["status"] = proposal.customer_status

        if proposal.enquiry_priority is not None:
            enquiry_updates["priority"] = proposal.enquiry_priority

        if proposal.enquiry_status is not None:
            enquiry_updates["status"] = proposal.enquiry_status

        if proposal.estimated_value is not None:
            enquiry_updates["estimated_value"] = proposal.estimated_value

        updated_customer, updated_enquiry = (
            enrichment_service.apply_enrichment(
                db,
                customer_id=proposal.customer_id,
                organization_id=organization_id,
                enquiry_id=proposal.enquiry_id,
                customer_updates=customer_updates,
                enquiry_updates=enquiry_updates,
                defer_commit=True,
            )
        )

        serialized: dict[str, Any] = {}

        if updated_customer is not None:
            serialized["customer"] = CustomerResponse.model_validate(
                updated_customer
            ).model_dump(mode="json")

        if updated_enquiry is not None:
            serialized["enquiry"] = SalesEnquiryResponse.model_validate(
                updated_enquiry
            ).model_dump(mode="json")

        record_id = proposal.customer_id
        message = "CRM enrichment applied successfully."

    elif record.action == "update_lead_qualification":
        proposal = UpdateLeadQualificationProposal.model_validate(record.payload)
        enquiry = enquiry_service.update_qualification(
            db,
            proposal.enquiry_id,
            LeadQualificationUpdate(
                qualification_status=proposal.qualification_status,
                qualification_reason=proposal.qualification_reason,
            ),
            organization_id=organization_id,
            customer_id=proposal.customer_id,
            defer_commit=True,
        )
        serialized = SalesEnquiryResponse.model_validate(enquiry).model_dump(mode="json")
        record_id = enquiry.enquiry_id
        label = "Ready" if enquiry.qualification_status == "ready" else "Not Ready"
        message = f"Sales lead #{record_id} qualification set to {label}."

    elif record.action == "close_opportunity":
        proposal = CloseOpportunityProposal.model_validate(record.payload)
        enquiry = enquiry_service.close_opportunity(
            db,
            proposal.enquiry_id,
            OpportunityClose(outcome=proposal.outcome, loss_reason=proposal.loss_reason),
            organization_id=organization_id,
            customer_id=proposal.customer_id,
            defer_commit=True,
        )
        serialized = SalesEnquiryResponse.model_validate(enquiry).model_dump(mode="json")
        record_id = enquiry.enquiry_id
        message = f"Opportunity #{record_id} closed as {proposal.outcome.title()}."

    else:
        proposal = CompleteFollowupProposal.model_validate(
            record.payload
        )

        completed = followup_service.complete_customer_followup(
            db,
            customer_id=proposal.customer_id,
            followup_id=proposal.followup_id,
            organization_id=organization_id,
            defer_commit=True,
        )

        serialized = FollowUpResponse.model_validate(
            completed
        ).model_dump(mode="json")

        record_id = completed.followup_id
        message = "Follow-up marked as completed."

    return AgentActionResult(
        status="completed",
        action=record.action,
        record_id=record_id,
        record=serialized,
        message=message,
    )


def confirm_action(
    db: Session,
    action_id: str,
    *,
    user_id: int = 0,
    is_admin: bool = False,
) -> AgentActionResult:
    proposal = db.get(HitlActionProposal, action_id)
    owner = db.get(User, proposal.owner_user_id) if proposal is not None else None
    if proposal is not None and owner is not None:
        # Before reading any policy: another organization's action looks missing.
        authorize_decision(
            db,
            proposal,
            user_id=user_id,
            is_admin=is_admin,
            message="You are not authorized to confirm or cancel this pending action.",
        )
    if (
        proposal is not None
        and owner is not None
        and proposal.status == "pending"
        and _is_agent_action(proposal.action_type)
        and get_action_policy(
            db, proposal.action_type, organization_id=owner.organization_id
        )
        == "disabled"
    ):
        pending_actions.block_disabled(
            db,
            action_id,
            user_id=user_id,
            is_admin=is_admin,
        )
        raise APIError(
            "This action is disabled by the global autonomy policy and cannot execute.",
            403,
            "action_disabled",
        )

    record = pending_actions.claim(
        db,
        action_id,
        user_id=user_id,
        is_admin=is_admin,
    )

    return _run_claimed_action(db, record)


def _action_failure_message(error: APIError) -> str:
    if error.code != "schedule_conflict" or error.details is None:
        return error.message
    suggestions = error.details.get("suggestions")
    if not isinstance(suggestions, list):
        return error.message
    alternatives = [
        f"{item['starts_at']} ({item['timezone']})"
        for item in suggestions
        if isinstance(item, dict)
        and isinstance(item.get("starts_at"), str)
        and isinstance(item.get("timezone"), str)
    ]
    if not alternatives:
        return error.message
    return f"{error.message} Available alternatives: {'; '.join(alternatives)}."


def _run_claimed_action(
    db: Session,
    record: PendingActionRecord,
    *,
    require_automatic: bool = False,
) -> AgentActionResult:
    result: AgentActionResult | None = None
    try:
        with atomic_transaction(db):
            result = _execute_action(
                db,
                record,
                require_automatic=require_automatic,
            )
            pending_actions.finish(
                db,
                record,
                status=result.status,
                result_record_id=result.record_id,
                error_code=result.error_code,
                result_message=result.message,
                commit=False,
            )
        calendar_type = _CALENDAR_ACTIVITY_TYPES.get(record.action)
        if result.status == "completed" and calendar_type and result.record_id:
            # Only after the approved CRM write has committed; the event goes to the
            # calendar of the user who requested the proposal.
            proposal = db.get(HitlActionProposal, record.action_id)
            calendar_sync_service.sync_activity(
                db,
                calendar_type,
                result.record_id,
                created_by_user_id=proposal.owner_user_id if proposal else record.user_id,
            )
        return result
    except APIError as exc:
        db.rollback()
        result = AgentActionResult(
            status="failed",
            action=record.action,
            error_code=exc.code,
            message=_action_failure_message(exc),
            details=exc.details,
        )
        pending_actions.finish(
            db,
            record,
            status="failed",
            result_record_id=None,
            error_code=result.error_code,
            result_message=result.message,
        )
        return result
    except Exception as exc:
        db.rollback()
        proposal = db.get(HitlActionProposal, record.action_id)
        execution = db.scalar(
            select(HitlExecutionAudit).where(
                HitlExecutionAudit.action_id == record.action_id
            )
        )
        if (
            proposal is not None
            and proposal.status == "completed"
            and execution is not None
            and execution.status == "completed"
        ):
            return AgentActionResult(
                status="completed",
                action=record.action,
                record_id=execution.result_record_id,
                record=result.record if result is not None else None,
                message=execution.result_message or "The CRM action completed.",
            )

        raw_message = str(exc)

        if contains_sensitive_material(raw_message):
            logger.error(
                "Unexpected error executing confirmed action "
                "[action=%s error_type=%s detail=%s]",
                record.action,
                type(exc).__name__,
                redact_sensitive_text(raw_message),
            )
        else:
            logger.exception(
                "Unexpected error executing confirmed action "
                "[action=%s error_type=%s]",
                record.action,
                type(exc).__name__,
            )

        result = AgentActionResult(
            status="failed",
            action=record.action,
            error_code="action_execution_error",
            message="The CRM action could not be completed.",
        )
        pending_actions.finish(
            db,
            record,
            status="failed",
            result_record_id=None,
            error_code=result.error_code,
            result_message=result.message,
        )
        return result


def cancel_action(
    db: Session,
    action_id: str,
    *,
    user_id: int = 0,
) -> AgentActionResult:
    record = pending_actions.cancel(db, action_id, user_id=user_id)

    return AgentActionResult(
        status="cancelled",
        action=record.action,
        message="Action cancelled. No CRM changes were made.",
    )


def reject_action(
    db: Session,
    action_id: str,
    *,
    user_id: int = 0,
    is_admin: bool = False,
) -> AgentActionResult:
    record = pending_actions.reject(
        db,
        action_id,
        user_id=user_id,
        is_admin=is_admin,
    )
    return AgentActionResult(
        status="rejected",
        action=record.action,
        message="Action rejected. No CRM changes were made.",
    )