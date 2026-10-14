"""Meeting drafts that span chat turns, and CRM reference matching for them.

A draft is the user's own collecting_information create_meeting HITL task, or the
values of their pending create_meeting proposal being amended. Its values were
grounded and validated when saved, so they are trusted; chat history never is.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.tools.time_grounding import stated_meeting_time
from app.core.exceptions import APIError
from app.agent.tools.action_schemas import CreateMeetingProposal
from app.models import Contact, HitlActionProposal, HitlTask, SalesEnquiry
from app.schemas.validators import (
    AmbiguousLocalTimeError,
    resolve_local_datetime,
    timezone_from_name,
    validate_timezone_name,
)
from app.services.activity_validation import validate_customer_references
from app.services.agent_action_state import action_fingerprint, pending_actions
from app.services.scheduling_service import current_time

ReferenceKind = Literal["contact", "enquiry"]


def load_meeting_draft(
    db: Session,
    task_id: str,
    *,
    user_id: int,
    organization_id: int,
    customer_id: int | None,
) -> tuple[HitlTask, dict[str, Any]]:
    # Owner-scoped: another user's or organization's task is reported as missing.
    task = pending_actions.get_task(db, task_id, user_id=user_id)
    if task.status == "expired":
        raise APIError("This meeting draft has expired.", 410, "task_expired")
    if task.status != "collecting_information":
        raise APIError(
            "This meeting draft is no longer collecting details.",
            409,
            "invalid_task_transition",
        )
    data = task.collected_data if isinstance(task.collected_data, dict) else {}
    operations = data.get("operations")
    operation = operations[0] if isinstance(operations, list) and len(operations) == 1 else None
    values = operation.get("values") if isinstance(operation, dict) else None
    if (
        customer_id is None
        or task.action_type != "create_meeting"
        or not isinstance(operation, dict)
        or operation.get("action") != "create_meeting"
        or not isinstance(values, dict)
        or values.get("customer_id") != customer_id
    ):
        raise APIError(
            "This meeting draft does not belong to the current customer conversation.",
            422,
            "invalid_meeting_draft",
        )
    # Saved references are rechecked in case a record moved or was deleted since.
    validate_customer_references(
        db,
        customer_id,
        organization_id=organization_id,
        contact_id=values.get("contact_id"),
        enquiry_id=values.get("enquiry_id"),
    )
    return task, dict(values)


def load_pending_meeting(
    db: Session,
    action_id: str,
    *,
    user_id: int,
    organization_id: int,
    customer_id: int | None,
) -> tuple[HitlActionProposal, dict[str, Any]]:
    """The user's own pending meeting proposal for this customer, as editable draft values."""
    proposal = db.get(HitlActionProposal, action_id)
    # Another user's (or organization's) proposal is reported as missing.
    if proposal is None or proposal.owner_user_id != user_id:
        raise APIError("Pending action not found.", 404, "action_not_found")
    expires_at = proposal.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if proposal.status == "pending" and expires_at <= datetime.now(timezone.utc):
        pending_actions.expire_overdue(db)
        raise APIError("Pending action has expired.", 410, "action_expired")
    if proposal.status == "expired":
        raise APIError("Pending action has expired.", 410, "action_expired")
    if proposal.status != "pending":
        raise APIError(
            "Pending action is already being processed or is no longer available.",
            409,
            "action_already_processed",
        )
    parameters = proposal.parameters if isinstance(proposal.parameters, dict) else {}
    if (
        customer_id is None
        or proposal.action_type != "create_meeting"
        or parameters.get("customer_id") != customer_id
    ):
        raise APIError(
            "This pending action is not a meeting for the current customer conversation.",
            422,
            "invalid_pending_action",
        )
    if action_fingerprint("create_meeting", parameters) != proposal.payload_fingerprint:
        raise APIError(
            "The pending action parameters have changed and can no longer be amended.",
            409,
            "action_parameters_changed",
        )
    validate_customer_references(
        db,
        customer_id,
        organization_id=organization_id,
        contact_id=parameters.get("contact_id"),
        enquiry_id=parameters.get("enquiry_id"),
    )
    values = proposal_draft_values(parameters)
    # A contact or enquiry name still awaiting the user's choice from an earlier message.
    task = db.get(HitlTask, proposal.task_id)
    data = task.collected_data if task is not None and isinstance(task.collected_data, dict) else {}
    unresolved = data.get("pending_amendment")
    if isinstance(unresolved, dict):
        for name_field, id_field in (("contact_name", "contact_id"), ("enquiry_reference", "enquiry_id")):
            phrase = unresolved.get(name_field)
            if isinstance(phrase, str) and phrase:
                values[name_field] = phrase
                values.pop(id_field, None)
    return proposal, values


_NEW_MEETING = re.compile(
    r"\b(?:another|second|separate|additional|extra|new|one\s+more)\s+"
    r"(?:meeting|appointment|session|one)\b|\balso\s+(?:schedule|book|set\s+up|arrange)\b",
    re.IGNORECASE,
)
_CHANGE = r"change|move|reschedule|update|edit|amend|modify|postpone"
_STRONG_AMEND = re.compile(rf"\b(?:{_CHANGE})\b", re.IGNORECASE)
_AMEND = re.compile(
    rf"\b(?:{_CHANGE}|adjust|shift|push|instead|actually|make\s+(?:it|that)|set\s+it|switch)\b",
    re.IGNORECASE,
)


def meeting_followup_intent(message: str) -> Literal["new", "amend", "unclear"] | None:
    """What a message sent while a meeting proposal is pending clearly asks for, if anything."""
    new_meeting = _NEW_MEETING.search(message)
    if new_meeting and _STRONG_AMEND.search(message):
        return "unclear"
    if new_meeting:
        return "new"
    return "amend" if _AMEND.search(message) else None


def proposal_draft_values(parameters: dict[str, Any]) -> dict[str, Any]:
    """A meeting proposal payload as task values (local date and time in its timezone)."""
    proposal = CreateMeetingProposal.model_validate(parameters)
    zone_name = proposal.scheduled_timezone or "UTC"
    local = proposal.scheduled_at.astimezone(timezone_from_name(zone_name))
    wall_time = local.time().replace(fold=0)
    values: dict[str, Any] = {
        key: value
        for key, value in proposal.model_dump(
            mode="json", exclude={"scheduled_at", "scheduled_timezone"}
        ).items()
        if value is not None
    }
    values.update(
        scheduled_date=local.date().isoformat(),
        scheduled_time=wall_time.isoformat(),
        scheduled_timezone=zone_name,
    )
    try:
        resolve_local_datetime(local.date(), wall_time, zone_name)
    except AmbiguousLocalTimeError:
        # Keep the exact instant when the wall-clock time repeats at a DST change.
        values["scheduled_time_occurrence"] = "later" if local.fold else "earlier"
    return values


def _words(text: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _has_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


def _candidates(
    db: Session, kind: ReferenceKind, *, organization_id: int, customer_id: int
) -> list[Any]:
    model = Contact if kind == "contact" else SalesEnquiry
    return list(
        db.scalars(
            select(model)
            .where(model.organization_id == organization_id, model.customer_id == customer_id)
            .order_by(getattr(model, f"{kind}_id"))
        )
    )


def _label(kind: ReferenceKind, record: Any) -> str:
    if kind == "contact":
        return f"#{record.contact_id} {record.name}" + (
            f" ({record.job_title})" if record.job_title else ""
        )
    return f"#{record.enquiry_id} {record.product}"


def _matches(kind: ReferenceKind, records: list[Any], phrase: str) -> list[Any]:
    wanted = _words(phrase)
    attribute = "name" if kind == "contact" else "product"
    exact = [record for record in records if _words(getattr(record, attribute)) == wanted]
    if exact:
        return exact
    # The phrase names the record plus extra words ("Rohan Kapoor IT Director",
    # "Network Upgrade - pen test"); an enquiry phrase may also be part of its text.
    return [
        record
        for record in records
        if _has_phrase(wanted, _words(getattr(record, attribute)))
        or (
            kind == "enquiry"
            and (
                _has_phrase(_words(record.product), wanted)
                or _has_phrase(_words(record.enquiry_text), wanted)
            )
        )
    ]


def _pick(kind: ReferenceKind, candidates: list[Any], message: str | None) -> Any | None:
    """The one candidate a reply singles out by "#ID", or by job title (contact) or product."""
    if not message:
        return None
    ids = {int(value) for value in re.findall(r"#\s*(\d+)", message)}
    chosen = [record for record in candidates if getattr(record, f"{kind}_id") in ids]
    if len(chosen) == 1:
        return chosen[0]
    text = _words(message)
    attribute = "job_title" if kind == "contact" else "product"
    chosen = [
        record
        for record in candidates
        if _words(getattr(record, attribute)) and _has_phrase(text, _words(getattr(record, attribute)))
    ]
    return chosen[0] if len(chosen) == 1 else None


def _choice_question(kind: ReferenceKind, phrase: str, matches: list[Any]) -> str:
    label = "contact" if kind == "contact" else "sales enquiry"
    options = "; ".join(_label(kind, record) for record in matches)
    return f'More than one {label} matches "{phrase[:80]}": {options}. Which one should I use?'


def resolve_reference(
    db: Session,
    kind: ReferenceKind,
    phrase: str,
    *,
    organization_id: int,
    customer_id: int,
    message: str | None = None,
) -> tuple[int | None, str | None]:
    """A unique match within this customer as (id, None); otherwise (None, question).

    With several matches, message (the user's reply) may single one out by "#ID" or title.
    """
    matches = _matches(
        kind,
        _candidates(db, kind, organization_id=organization_id, customer_id=customer_id),
        phrase,
    )
    if len(matches) > 1 and (picked := _pick(kind, matches, message)) is not None:
        matches = [picked]
    if len(matches) == 1:
        return getattr(matches[0], f"{kind}_id"), None
    if matches:
        return None, _choice_question(kind, phrase, matches)
    label = "contact" if kind == "contact" else "sales enquiry"
    return None, (
        f'I could not find a {label} matching "{phrase[:80]}" for this customer. '
        f"Which {label} should I use, or should I leave it out?"
    )


_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_RELATIVE_DAY = re.compile(r"\b(today|tomorrow)\b", re.IGNORECASE)
_OTHER_DAY = re.compile(
    r"\b(?:not|instead|next|after|before|week|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)
_TIMEZONE = re.compile(r"\b(?:UTC|[A-Za-z]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)\b")
_DURATION = re.compile(r"\b(\d{1,4})\s*(minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE)


def stated_meeting_date(message: str, timezone_name: str) -> str | None:
    """The one date a message states (YYYY-MM-DD, today or tomorrow in the zone), else None."""
    found: set[date] = set()
    for raw in _ISO_DATE.findall(message):
        try:
            found.add(date.fromisoformat(raw))
        except ValueError:
            return None
    # "today"/"tomorrow" count only when no other day is named or negated nearby.
    if not _OTHER_DAY.search(message):
        today = current_time().astimezone(timezone_from_name(timezone_name)).date()
        for word in _RELATIVE_DAY.findall(message):
            found.add(today + timedelta(days=1 if word.lower() == "tomorrow" else 0))
    return found.pop().isoformat() if len(found) == 1 else None


_NUMERIC_DAY = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\b|\b\d{1,2}/\d{1,2}\b", re.IGNORECASE)


def mentions_a_day(message: str) -> bool:
    """Whether a message names any date or day at all, even one this module cannot read."""
    return bool(
        _ISO_DATE.search(message)
        or _RELATIVE_DAY.search(message)
        or _OTHER_DAY.search(message)
        or _NUMERIC_DAY.search(message)
    )


def stated_meeting_timezone(message: str) -> str | None:
    return _stated_timezone(message)


def _stated_timezone(message: str) -> str | None:
    found: set[str] = set()
    for raw in _TIMEZONE.findall(message):
        try:
            found.add(validate_timezone_name(raw))
        except (TypeError, ValueError):
            continue
    return found.pop() if len(found) == 1 else None


def _stated_duration(message: str) -> int | None:
    found = {
        int(amount) * (1 if unit.lower().startswith("m") else 60)
        for amount, unit in _DURATION.findall(message)
    }
    minutes = found.pop() if len(found) == 1 else None
    return minutes if minutes is not None and 1 <= minutes <= 1440 else None


def extract_draft_answers(
    db: Session,
    message: str,
    values: dict[str, Any],
    *,
    organization_id: int,
    customer_id: int,
    timezone_name: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Values the message clearly states, plus questions.

    Used when the model made no tool call. Labelled "agenda:" and "notes:" text replaces
    the stored value; every other field is filled only while the draft lacks it, and
    only from this message. Unlabelled free text never becomes an agenda or notes.
    """
    answers: dict[str, Any] = {}
    questions: list[str] = []
    labelled, message = _labelled_text(message)
    answers.update(labelled)
    stated_zone = _stated_timezone(message)
    if values.get("scheduled_timezone") is None and stated_zone is not None:
        answers["scheduled_timezone"] = stated_zone
    zone = values.get("scheduled_timezone") or stated_zone or timezone_name
    if values.get("scheduled_time") is None:
        stated_time, question = stated_meeting_time(message)
        if stated_time is not None:
            answers["scheduled_time"] = stated_time.strftime("%H:%M")
        elif question is not None:
            questions.append(question)
    if values.get("scheduled_date") is None:
        stated_date = stated_meeting_date(message, zone or "UTC")
        if stated_date is not None:
            answers["scheduled_date"] = stated_date
    if values.get("duration") is None:
        duration = _stated_duration(message)
        if duration is not None:
            answers["duration"] = duration
    for kind, name_field, id_field in (
        ("contact", "contact_name", "contact_id"),
        ("enquiry", "enquiry_reference", "enquiry_id"),
    ):
        if values.get(name_field) and _SKIP_REFERENCE[kind].search(message):
            # An explicit decision to go ahead without the unmatched reference.
            answers[name_field] = answers[id_field] = None
    # A full contact name is specific enough to mean that person. Products are not:
    # mentioning one in passing never links an enquiry; that needs enquiry_reference.
    if values.get("contact_id") is None and "contact_id" not in answers:
        records = _candidates(db, "contact", organization_id=organization_id, customer_id=customer_id)
        named = [record for record in records if _has_phrase(_words(message), _words(record.name))]
        pending = values.get("contact_name")
        pool = named or (_matches("contact", records, pending) if pending else [])
        if len(pool) > 1 and (picked := _pick("contact", pool, message)) is not None:
            pool = [picked]
        if len(pool) == 1:
            answers["contact_id"], answers["contact_name"] = pool[0].contact_id, None
        elif len(pool) > 1 and named:
            # Kept as an open choice so the next message can settle it.
            answers["contact_name"] = named[0].name
            questions.append(_choice_question("contact", named[0].name, named))
    pending_enquiry = values.get("enquiry_reference")
    if values.get("enquiry_id") is None and pending_enquiry and "enquiry_id" not in answers:
        resolved, _ = resolve_reference(
            db,
            "enquiry",
            pending_enquiry,
            organization_id=organization_id,
            customer_id=customer_id,
            message=message,
        )
        if resolved is not None:
            answers["enquiry_id"], answers["enquiry_reference"] = resolved, None
    return answers, questions


# "agenda: ..." / "notes: ..." up to a line break, ";" or the next "label:".
_LABELLED = re.compile(
    r"\b(agenda|notes?)\s*:\s*(.*?)\s*(?=[\n;]|\b[A-Za-z]+\s*:|$)",
    re.IGNORECASE | re.DOTALL,
)


def _labelled_text(message: str) -> tuple[dict[str, str], str]:
    """Explicitly labelled agenda and notes, and the message without them.

    Removing the labelled text keeps a time or name inside notes from being read as
    the meeting's own time or contact.
    """
    found: dict[str, str] = {}

    def take(match: re.Match[str]) -> str:
        value = match[2].strip().rstrip(".,")
        if value:
            found["agenda" if match[1].lower() == "agenda" else "notes"] = value
        return " "

    return found, _LABELLED.sub(take, message)


_SKIP_REFERENCE = {
    "contact": re.compile(
        r"\b(?:no|without(?:\s+(?:a|any))?|skip(?:\s+the)?)\s+contacts?\b", re.IGNORECASE
    ),
    "enquiry": re.compile(
        r"\b(?:no|without(?:\s+(?:a|an|any))?|skip(?:\s+the)?)\s+(?:enquir(?:y|ies)|inquir(?:y|ies)|leads?)\b",
        re.IGNORECASE,
    ),
}


def unmatched_reference_questions(
    db: Session, values: dict[str, Any], *, organization_id: int
) -> list[str]:
    """Re-ask about names saved on a draft that still match no single record."""
    customer_id = values.get("customer_id")
    if not isinstance(customer_id, int):
        return []
    questions: list[str] = []
    for kind, name_field, id_field in (
        ("contact", "contact_name", "contact_id"),
        ("enquiry", "enquiry_reference", "enquiry_id"),
    ):
        phrase = values.get(name_field)
        if phrase and values.get(id_field) is None:
            _, question = resolve_reference(
                db, kind, phrase, organization_id=organization_id, customer_id=customer_id  # type: ignore[arg-type]
            )
            if question:
                questions.append(question)
    return questions
