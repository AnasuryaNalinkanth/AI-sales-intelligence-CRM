import json
from datetime import datetime, timezone
from typing import Any, Sequence, TypeVar, TypedDict

from langchain_core.language_models import BaseChatModel
from sqlalchemy.orm import Session

from app.agent.model import create_chat_model
from app.agent.prompts import CUSTOMER_SUMMARY_SYSTEM_PROMPT, MEETING_BRIEF_SYSTEM_PROMPT
from app.agent.structured import invoke_structured_output
from app.agent.tools import AgentToolContext, build_read_only_tools
from app.core.exceptions import APIError
from app.schemas.agent import (
    CustomerSummaryOutput,
    CustomerSummaryResponse,
    MeetingBriefOutput,
    MeetingBriefResponse,
)
from app.schemas.contact import ContactResponse
from app.schemas.meeting import MeetingResponse
from app.schemas.sales_enquiry import SalesEnquiryResponse
from app.services import meeting_service

MAX_CONTACTS = 8
MAX_PREVIOUS_ENQUIRIES = 5
MAX_MEETINGS = 8
MAX_CALLS = 8
MAX_PENDING_FOLLOWUPS = 8
MAX_COMPLETED_FOLLOWUPS = 5
MAX_ACTIVITY = 12
MAX_PREVIOUS_DISCUSSIONS = 8
RecordType = TypeVar("RecordType")


class DiscussionRecord(TypedDict):
    activity_date: datetime
    date_label: str
    activity_type: str
    fact: str

OPEN_ENQUIRY_STATUSES = {
    "open",
    "new",
    "in_progress",
    "qualified",
    "negotiation",
    "proposal",
}
OPEN_FOLLOWUP_STATUSES = {"pending", "in_progress", "overdue"}
COMPLETED_FOLLOWUP_STATUSES = {"completed"}
TOOL_ERROR_STATUS = {
    "customer_not_found": 404,
    "meeting_not_found": 404,
    "invalid_date_range": 422,
    "customer_scope_violation": 403,
    "database_error": 500,
}


def _tool_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _limit(records: Sequence[RecordType], limit: int) -> list[RecordType]:
    return list(records[:limit])


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_strings(values: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for value in values or []:
        text = _clean_text(value)
        if text:
            cleaned.append(text)
    return cleaned


def _call_crm_tool(tools: dict[str, Any], name: str, payload: dict[str, Any]) -> Any:
    try:
        raw = tools[name].invoke(payload)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except APIError:
        raise
    except Exception as exc:
        raise APIError(
            "Customer data could not be retrieved for AI analysis.",
            status_code=502,
            code="tool_error",
        ) from exc
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
        error = parsed["error"]
        code = error.get("code") if isinstance(error.get("code"), str) else "tool_error"
        message = (
            error.get("message")
            if isinstance(error.get("message"), str)
            else "Customer data could not be retrieved for AI analysis."
        )
        raise APIError(
            message,
            status_code=TOOL_ERROR_STATUS.get(code, 502),
            code=code,
        )
    if not isinstance(parsed, dict) or "data" not in parsed:
        raise APIError(
            "Customer data could not be retrieved for AI analysis.",
            status_code=502,
            code="tool_error",
        )
    return parsed["data"]


def _split_enquiries(
    enquiries: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    ordered = sorted(
        enquiries,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    current = next(
        (
            item
            for item in ordered
            if str(item.get("status") or "").lower() in OPEN_ENQUIRY_STATUSES
        ),
        ordered[0] if ordered else None,
    )
    previous = [item for item in ordered if item is not current]
    return current, _limit(previous, MAX_PREVIOUS_ENQUIRIES)


def _format_followup(followup: dict[str, Any]) -> str:
    description = _clean_text(followup.get("description")) or _clean_text(followup.get("type"))
    status = _clean_text(followup.get("status")) or "unknown"
    due = _clean_text(followup.get("due_date")) or "due date unavailable"
    return f"{description} (status {status}, due {due})"


def _open_followup_labels(followups: list[dict[str, Any]]) -> list[str]:
    return [
        _format_followup(item)
        for item in followups
        if str(item.get("status") or "").lower() in OPEN_FOLLOWUP_STATUSES
    ]


def _discussion_datetime(value: Any) -> datetime:
    text = _clean_text(value)
    if text is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _discussion_lines(
    meetings: list[dict[str, Any]],
    calls: list[dict[str, Any]],
) -> list[str]:
    records: list[DiscussionRecord] = []
    for meeting in meetings:
        fact = (
            _clean_text(meeting.get("summary"))
            or _clean_text(meeting.get("notes"))
            or _clean_text(meeting.get("agenda"))
        )
        if fact:
            date_label = _clean_text(meeting.get("scheduled_at")) or "date unavailable"
            records.append(
                {
                    "activity_date": _discussion_datetime(meeting.get("scheduled_at")),
                    "date_label": date_label,
                    "activity_type": "Meeting",
                    "fact": fact,
                }
            )
    for call in calls:
        fact = (
            _clean_text(call.get("summary"))
            or _clean_text(call.get("outcome"))
            or _clean_text(call.get("notes"))
        )
        if fact:
            activity_date = call.get("actual_time") or call.get("scheduled_at") or call.get("created_at")
            date_label = _clean_text(activity_date) or "date unavailable"
            records.append(
                {
                    "activity_date": _discussion_datetime(activity_date),
                    "date_label": date_label,
                    "activity_type": "Call",
                    "fact": fact,
                }
            )

    records.sort(key=lambda record: record["activity_date"], reverse=True)
    return [
        f'{record["activity_type"]} on {record["date_label"]}: {record["fact"]}'
        for record in _limit(records, MAX_PREVIOUS_DISCUSSIONS)
    ]


def _build_activity_timeline_from_records(
    enquiries: list[dict[str, Any]],
    meetings: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    followups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    timeline: list[tuple[datetime, dict[str, Any]]] = []
    for enquiry in enquiries:
        activity_date = enquiry.get("created_at")
        timeline.append(
            (
                _discussion_datetime(activity_date),
                {
                    "activity_id": f"enquiry-{enquiry.get('enquiry_id')}",
                    "activity_type": "enquiry",
                    "activity_date": activity_date,
                    "status": enquiry.get("status"),
                    "title": enquiry.get("product") or "Sales lead",
                    "description": enquiry.get("enquiry_text"),
                },
            )
        )
    for meeting in meetings:
        activity_date = meeting.get("scheduled_at")
        description = (
            meeting.get("summary") or meeting.get("notes") or meeting.get("agenda")
        )
        timeline.append(
            (
                _discussion_datetime(activity_date),
                {
                    "activity_id": f"meeting-{meeting.get('meeting_id')}",
                    "activity_type": "meeting",
                    "activity_date": activity_date,
                    "status": meeting.get("status"),
                    "title": meeting.get("agenda") or "Meeting",
                    "description": description,
                },
            )
        )
    for call in calls:
        activity_date = (
            call.get("actual_time")
            or call.get("scheduled_at")
            or call.get("created_at")
        )
        timeline.append(
            (
                _discussion_datetime(activity_date),
                {
                    "activity_id": f"call-{call.get('call_id')}",
                    "activity_type": "call",
                    "activity_date": activity_date,
                    "status": call.get("status"),
                    "title": call.get("call_type") or "Sales call",
                    "description": call.get("summary")
                    or call.get("outcome")
                    or call.get("notes"),
                },
            )
        )
    for followup in followups:
        activity_date = followup.get("due_date")
        timeline.append(
            (
                _discussion_datetime(activity_date),
                {
                    "activity_id": f"follow_up-{followup.get('followup_id')}",
                    "activity_type": "follow_up",
                    "activity_date": activity_date,
                    "status": followup.get("status"),
                    "title": followup.get("type"),
                    "description": followup.get("description"),
                },
            )
        )

    timeline.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in timeline[:MAX_ACTIVITY]]


def _customer_snapshot(db: Session, customer_id: int, *, organization_id: int) -> dict[str, Any]:
    context = AgentToolContext(organization_id=organization_id)
    tools = {tool.name: tool for tool in build_read_only_tools(db, context)}
    profile = _call_crm_tool(tools, "get_customer_profile", {"customer_id": customer_id})
    enquiries = _tool_items({"items": profile.get("sales_enquiries", [])})
    all_meetings = _tool_items(
        _call_crm_tool(tools, "get_customer_meetings", {"customer_id": customer_id})
    )
    meetings = _limit(all_meetings, MAX_MEETINGS)
    all_calls = _tool_items(
        _call_crm_tool(tools, "get_customer_calls", {"customer_id": customer_id})
    )
    calls = _limit(all_calls, MAX_CALLS)
    followups = _tool_items(
        _call_crm_tool(tools, "get_customer_followups", {"customer_id": customer_id})
    )
    activity = _build_activity_timeline_from_records(
        enquiries,
        all_meetings,
        all_calls,
        followups,
    )
    current_enquiry, previous_enquiries = _split_enquiries(enquiries)
    pending = _limit(
        [
            item
            for item in followups
            if str(item.get("status") or "").lower() in OPEN_FOLLOWUP_STATUSES
        ],
        MAX_PENDING_FOLLOWUPS,
    )
    completed = _limit(
        [
            item
            for item in followups
            if str(item.get("status") or "").lower() in COMPLETED_FOLLOWUP_STATUSES
        ],
        MAX_COMPLETED_FOLLOWUPS,
    )
    contacts = _limit(_tool_items({"items": profile.get("contacts", [])}), MAX_CONTACTS)
    return {
        "customer": profile.get("customer") or "unavailable",
        "company": profile.get("company") or "unavailable",
        "contacts": contacts or "unavailable",
        "current_sales_enquiry": current_enquiry or "unavailable",
        "previous_sales_enquiries": previous_enquiries or "unavailable",
        "recent_meetings": meetings or "unavailable",
        "recent_calls": calls or "unavailable",
        "pending_followups": pending or "unavailable",
        "completed_followups": completed or "unavailable",
        "recent_activity_timeline": activity or "unavailable",
        "_open_followup_labels": _open_followup_labels(pending),
        "_previous_discussions": _discussion_lines(meetings, calls),
        "_has_current_enquiry": current_enquiry is not None,
    }



def _snapshot_for_model(snapshot: dict[str, Any]) -> str:
    public = {key: value for key, value in snapshot.items() if not key.startswith("_")}
    return json.dumps(public, ensure_ascii=False, default=str)


def generate_customer_summary(
    db: Session,
    customer_id: int,
    *,
    organization_id: int,
    model: BaseChatModel | None = None,
) -> CustomerSummaryResponse:
    snapshot = _customer_snapshot(db, customer_id, organization_id=organization_id)
    output = invoke_structured_output(
        model or create_chat_model(),
        CustomerSummaryOutput,
        system_prompt=CUSTOMER_SUMMARY_SYSTEM_PROMPT,
        user_content=(
            "Generate a customer summary from this CRM snapshot. "
            "History has already been limited in the service layer.\n"
            f"{_snapshot_for_model(snapshot)}"
        ),
    )
    if not isinstance(output, CustomerSummaryOutput):
        output = CustomerSummaryOutput.model_validate(output)
    return CustomerSummaryResponse(
        customer_id=customer_id,
        summary=output.summary.strip(),
        key_points=_normalize_strings(output.key_points),
        customer_concerns=_normalize_strings(output.customer_concerns),
        open_followups=snapshot["_open_followup_labels"],
        recommended_next_action=output.recommended_next_action.strip(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def generate_meeting_brief(
    db: Session,
    meeting_id: int,
    *,
    organization_id: int,
    model: BaseChatModel | None = None,
) -> MeetingBriefResponse:
    meeting = meeting_service.get_meeting(db, meeting_id, organization_id=organization_id)
    snapshot = _customer_snapshot(db, meeting.customer_id, organization_id=organization_id)
    contact: Any = "unavailable"
    enquiry: Any = "unavailable"
    if meeting.contact is not None:
        contact = ContactResponse.model_validate(meeting.contact).model_dump(mode="json")
    if meeting.enquiry is not None:
        enquiry = SalesEnquiryResponse.model_validate(meeting.enquiry).model_dump(mode="json")
    meeting_payload = MeetingResponse.model_validate(meeting).model_dump(mode="json")
    brief_context = json.dumps(
        {
            "meeting": meeting_payload,
            "associated_contact": contact,
            "associated_enquiry": enquiry,
            "customer_snapshot": {
                key: value
                for key, value in snapshot.items()
                if not key.startswith("_")
            },
        },
        ensure_ascii=False,
        default=str,
    )
    output = invoke_structured_output(
        model or create_chat_model(),
        MeetingBriefOutput,
        system_prompt=MEETING_BRIEF_SYSTEM_PROMPT,
        user_content=(
            "Prepare a meeting brief from this CRM snapshot. "
            "History has already been limited in the service layer. "
            "Do not invent an agenda or previous discussions.\n"
            f"{brief_context}"
        ),
    )
    if not isinstance(output, MeetingBriefOutput):
        output = MeetingBriefOutput.model_validate(output)
    current_requirement = output.current_requirement.strip()
    if not snapshot["_has_current_enquiry"] and meeting.enquiry is None:
        current_requirement = "Current requirement is unavailable in CRM records."
    return MeetingBriefResponse(
        meeting_id=meeting.meeting_id,
        customer_id=meeting.customer_id,
        brief=output.brief.strip(),
        customer_overview=output.customer_overview.strip(),
        current_requirement=current_requirement,
        previous_discussions=snapshot["_previous_discussions"],
        unresolved_issues=_normalize_strings(output.unresolved_issues),
        recommended_talking_points=_normalize_strings(output.recommended_talking_points),
        recommended_next_action=output.recommended_next_action.strip(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
