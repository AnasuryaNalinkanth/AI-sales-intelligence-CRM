from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Annotated
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import APIError
from app.models import Call, Contact, Customer, FollowUp, Meeting, SalesEnquiry
from app.schemas.agent import AgentActionName, AgentTaskFormChoice, AgentTaskFormField
from app.agent.tools.action_schemas import (
    ApplyEnrichmentProposal,
    CloseOpportunityProposal,
    CompleteFollowupProposal,
    CreateFollowupProposal,
    CreateMeetingTaskInput,
    CreateMeetingProposal,
    RecordCallResultProposal,
    ScheduleCallProposal,
    UpdateLeadQualificationProposal,
)
from app.schemas.validators import (
    AmbiguousLocalTimeError,
    NonexistentLocalTimeError,
    validate_timezone_name,
)


@dataclass(frozen=True)
class FormField:
    name: str
    label: str
    input_type: str
    required: bool = False
    help_text: str | None = None
    selector: str | None = None


@dataclass(frozen=True)
class ActionDefinition:
    schema: type[BaseModel]
    fields: tuple[FormField, ...]
    required_fields: frozenset[str]
    required_any: tuple[tuple[str, ...], ...] = ()
    task_schema: type[BaseModel] | None = None


ACTION_REGISTRY: dict[AgentActionName, ActionDefinition] = {
    "create_meeting": ActionDefinition(
        CreateMeetingProposal,
        (
            FormField("customer_id", "Customer", "record", True, selector="customer"),
            FormField("scheduled_date", "Meeting date", "date", True),
            FormField("scheduled_time", "Meeting time", "time", True),
            FormField(
                "scheduled_timezone",
                "Meeting timezone",
                "timezone",
                True,
                "Use an IANA timezone, such as Asia/Dubai, or an explicit UTC offset.",
            ),
            FormField(
                "scheduled_time_occurrence",
                "Which occurrence?",
                "select",
                help_text="Choose this only when the local time occurs twice.",
            ),
            FormField("contact_id", "Contact", "record", selector="contact"),
            FormField(
                "contact_name",
                "Contact name to match",
                "text",
                help_text="Choose the contact above, or clear this to schedule without one.",
            ),
            FormField("enquiry_id", "Sales enquiry", "record", selector="enquiry"),
            FormField(
                "enquiry_reference",
                "Sales enquiry to match",
                "text",
                help_text="Choose the enquiry above, or clear this to schedule without one.",
            ),
            FormField("duration", "Duration in minutes", "number"),
            FormField("agenda", "Agenda", "text"),
            FormField("notes", "Notes", "textarea"),
        ),
        frozenset(
            {"customer_id", "scheduled_date", "scheduled_time", "scheduled_timezone"}
        ),
        task_schema=CreateMeetingTaskInput,
    ),
    "create_followup": ActionDefinition(
        CreateFollowupProposal,
        (
            FormField("customer_id", "Customer", "record", True, selector="customer"),
            FormField("type", "Follow-up type", "text", True),
            FormField("due_date", "Due date and time", "datetime", True),
            FormField(
                "due_timezone",
                "Due-time timezone",
                "timezone",
                True,
                "Use an IANA timezone, such as Asia/Dubai, or an explicit UTC offset.",
            ),
            FormField(
                "due_time_occurrence",
                "Which occurrence?",
                "select",
                help_text="Choose this only when the local time occurs twice.",
            ),
            FormField("enquiry_id", "Sales enquiry", "record", selector="enquiry"),
            FormField("meeting_id", "Meeting", "record", selector="meeting"),
            FormField("call_id", "Call", "record", selector="call"),
            FormField("description", "Description", "textarea"),
            FormField("assigned_to", "Assigned to", "text"),
        ),
        frozenset({"customer_id", "type", "due_date"}),
    ),
    "record_call_result": ActionDefinition(
        RecordCallResultProposal,
        (
            FormField("customer_id", "Customer", "record", True, selector="customer"),
            FormField("contact_id", "Contact", "record", selector="contact"),
            FormField("enquiry_id", "Sales enquiry", "record", selector="enquiry"),
            FormField("call_type", "Call type", "text"),
            FormField("actual_time", "Call time", "datetime", help_text="Enter the time in UTC."),
            FormField("outcome", "Call outcome", "text"),
            FormField("notes", "Call notes", "textarea"),
            FormField("summary", "Call summary", "textarea"),
            FormField("next_followup_date", "Next follow-up date", "datetime", help_text="Enter the time in UTC."),
        ),
        frozenset({"customer_id"}),
        (("outcome", "notes", "summary"),),
    ),
    "schedule_call": ActionDefinition(
        ScheduleCallProposal,
        (
            FormField("customer_id", "Customer", "record", True, selector="customer"),
            FormField("scheduled_at", "Call date and time", "datetime", True),
            FormField(
                "scheduled_timezone",
                "Call timezone",
                "timezone",
                True,
                "Use an IANA timezone, such as Asia/Dubai, or an explicit UTC offset.",
            ),
            FormField(
                "scheduled_time_occurrence",
                "Which occurrence?",
                "select",
                help_text="Choose this only when the local time occurs twice.",
            ),
            FormField("duration", "Duration in minutes", "number"),
            FormField("contact_id", "Contact", "record", selector="contact"),
            FormField("enquiry_id", "Sales enquiry", "record", selector="enquiry"),
            FormField("call_type", "Call type", "text"),
            FormField("notes", "Notes", "textarea"),
        ),
        frozenset({"customer_id", "scheduled_at", "scheduled_timezone"}),
    ),
    "complete_followup": ActionDefinition(
        CompleteFollowupProposal,
        (
            FormField("customer_id", "Customer", "record", True, selector="customer"),
            FormField("followup_id", "Follow-up", "record", True, selector="followup"),
        ),
        frozenset({"customer_id", "followup_id"}),
    ),
    "apply_enrichment": ActionDefinition(
        ApplyEnrichmentProposal,
        (
            FormField("customer_id", "Customer", "record", True, selector="customer"),
            FormField("enquiry_id", "Sales enquiry", "record", selector="enquiry"),
            FormField("sales_stage", "Sales stage", "text"),
            FormField("customer_status", "Customer status", "text"),
            FormField("enquiry_priority", "Enquiry priority", "select"),
            FormField("enquiry_status", "Enquiry status", "text"),
            FormField("estimated_value", "Estimated value", "number"),
        ),
        frozenset({"customer_id"}),
    ),
    "update_lead_qualification": ActionDefinition(
        UpdateLeadQualificationProposal,
        (
            FormField("customer_id", "Customer", "record", True, selector="customer"),
            FormField("enquiry_id", "Sales lead", "record", True, selector="enquiry"),
            FormField("qualification_status", "Qualification", "select", True),
            FormField("qualification_reason", "Reason", "textarea"),
        ),
        frozenset({"customer_id", "enquiry_id", "qualification_status"}),
    ),
    "close_opportunity": ActionDefinition(
        CloseOpportunityProposal,
        (
            FormField("customer_id", "Customer", "record", True, selector="customer"),
            FormField("enquiry_id", "Opportunity", "record", True, selector="enquiry"),
            FormField("outcome", "Outcome", "select", True),
            FormField(
                "loss_reason",
                "Loss reason",
                "textarea",
                help_text="Required when the outcome is Lost.",
            ),
        ),
        frozenset({"customer_id", "enquiry_id", "outcome"}),
    ),
}

_SELECTORS: dict[str, tuple[type[Any], str, str]] = {
    "customer": (Customer, "customer_id", "customer_name"),
    "contact": (Contact, "contact_id", "name"),
    "enquiry": (SalesEnquiry, "enquiry_id", "enquiry_text"),
    "meeting": (Meeting, "meeting_id", "agenda"),
    "call": (Call, "call_id", "call_type"),
    "followup": (FollowUp, "followup_id", "type"),
}


def validate_operation(
    action: AgentActionName, values: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    definition = ACTION_REGISTRY[action]
    input_schema = definition.task_schema or definition.schema
    allowed = set(input_schema.model_fields)
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise APIError(
            f"Unknown fields for {action}: {', '.join(unknown)}.",
            422,
            "invalid_task_input",
        )
    for name, value in values.items():
        field = input_schema.model_fields[name]
        try:
            annotation = (
                Annotated[field.annotation, *field.metadata]
                if field.metadata
                else field.annotation
            )
            adapter = TypeAdapter(annotation)
            try:
                adapter.validate_python(value)
            except ValidationError:
                adapter.validate_json(json.dumps(value))
        except (ValidationError, TypeError) as exc:
            raise APIError(
                f"The submitted value for {name} is invalid.",
                422,
                "invalid_task_input",
            ) from exc
    missing = sorted(
        field
        for field in definition.required_fields
        if values.get(field) is None or values.get(field) == ""
    )
    for group in definition.required_any:
        if not any(values.get(field) not in (None, "") for field in group):
            missing.append(" or ".join(group))
    if action == "close_opportunity" and values.get("outcome") == "lost" and not values.get(
        "loss_reason"
    ):
        missing.append("loss_reason")
    if action == "create_meeting":
        # A named contact or enquiry that matched no single record needs an explicit
        # choice, or the name cleared, before the meeting can be proposed.
        for name_field, id_field in (("contact_name", "contact_id"), ("enquiry_reference", "enquiry_id")):
            if values.get(name_field) and values.get(id_field) is None:
                missing.append(id_field)
    if action == "create_meeting" and values.get("scheduled_timezone"):
        try:
            validate_timezone_name(values["scheduled_timezone"])
        except (TypeError, ValueError) as exc:
            raise APIError(
                "The submitted value for scheduled_timezone is invalid.",
                422,
                "invalid_task_input",
            ) from exc
    if missing:
        return None, sorted(set(missing))
    try:
        if action == "create_meeting":
            task_values = CreateMeetingTaskInput.model_validate(values)
            try:
                scheduled_at = task_values.resolve_scheduled_at()
            except AmbiguousLocalTimeError:
                return None, ["scheduled_time_occurrence"]
            except NonexistentLocalTimeError:
                return None, ["valid_scheduled_time"]
            proposal_values = task_values.model_dump(
                exclude={
                    "scheduled_date",
                    "scheduled_time",
                    "scheduled_time_occurrence",
                    "contact_name",
                    "enquiry_reference",
                }
            )
            proposal_values["scheduled_at"] = scheduled_at
            validated = definition.schema.model_validate(proposal_values)
        else:
            validated = definition.schema.model_validate(values)
    except ValidationError as exc:
        raise APIError(
            "One or more submitted task values are invalid.",
            422,
            "invalid_task_input",
        ) from exc
    payload = validated.model_dump(mode="json")
    if action == "apply_enrichment" and not any(
        payload.get(key) is not None
        for key in (
            "sales_stage",
            "customer_status",
            "enquiry_priority",
            "enquiry_status",
            "estimated_value",
        )
    ):
        return None, ["At least one enrichment value"]
    if action == "record_call_result" and not any(
        payload.get(key) for key in ("outcome", "notes", "summary")
    ):
        return None, ["outcome or notes or summary"]
    return payload, []


def form_fields(
    db: Session,
    action: AgentActionName,
    values: dict[str, Any],
    *,
    organization_id: int,
) -> list[AgentTaskFormField]:
    definition = ACTION_REGISTRY[action]
    customer_id = values.get("customer_id")
    fields: list[AgentTaskFormField] = []
    for item in definition.fields:
        choices: list[AgentTaskFormChoice] = []
        if item.selector:
            model, id_name, label_name = _SELECTORS[item.selector]
            id_column = getattr(model, id_name)
            label_column = getattr(model, label_name)
            query = select(id_column, label_column).where(
                model.organization_id == organization_id
            )
            if item.selector == "customer":
                query = query.order_by(label_column)
            elif customer_id is not None:
                query = query.where(model.customer_id == customer_id).order_by(id_column)
            else:
                query = query.where(id_column == -1)
            for record_id, label in db.execute(query.limit(1000)).all():
                choices.append(
                    AgentTaskFormChoice(
                        id=record_id,
                        label=f"#{record_id} {label or item.selector}",
                    )
                )
        elif item.name == "enquiry_priority":
            choices = [
                AgentTaskFormChoice(id=value, label=value.title())
                for value in ("low", "medium", "normal", "high", "urgent")
            ]
        elif item.name == "qualification_status":
            choices = [
                AgentTaskFormChoice(id="ready", label="Ready"),
                AgentTaskFormChoice(id="not_ready", label="Not Ready"),
            ]
        elif item.name == "outcome":
            choices = [
                AgentTaskFormChoice(id="won", label="Won"),
                AgentTaskFormChoice(id="lost", label="Lost"),
            ]
        elif item.name == "scheduled_time_occurrence":
            choices = [
                AgentTaskFormChoice(id="earlier", label="Earlier occurrence"),
                AgentTaskFormChoice(id="later", label="Later occurrence"),
            ]
        elif item.name == "due_time_occurrence":
            choices = [
                AgentTaskFormChoice(id="earlier", label="Earlier occurrence"),
                AgentTaskFormChoice(id="later", label="Later occurrence"),
            ]
        fields.append(
            AgentTaskFormField(
                name=item.name,
                label=item.label,
                input_type=item.input_type,
                required=item.required,
                value=values.get(item.name),
                help_text=item.help_text
                or ("Select the CRM record by its stable ID." if item.selector else None),
                choices=choices,
                depends_on="customer_id" if item.selector and item.selector != "customer" else None,
            )
        )
    return fields


def task_is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= current
