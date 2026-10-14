from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decimal import Decimal

from app.schemas.follow_up import FollowUpStatus
from app.schemas.meeting import MeetingStatus
from app.schemas.sales_enquiry import (
    EnquiryPriority,
    OpportunityClose,
    OpportunityOutcome,
    QualificationStatus,
)
from app.schemas.validators import (
    MAX_TEXT_LENGTH,
    normalize_utc_datetime,
    resolve_local_datetime,
    timezone_name_from_datetime,
    validate_timezone_name,
)


class ActionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class CreateMeetingProposal(ActionInput):
    customer_id: int = Field(ge=1)
    scheduled_at: datetime
    scheduled_timezone: str | None = None
    contact_id: int | None = Field(default=None, ge=1)
    enquiry_id: int | None = Field(default=None, ge=1)
    duration: int | None = Field(default=None, gt=0, le=1440)
    status: MeetingStatus = "scheduled"
    agenda: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    notes: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)

    @field_validator("scheduled_timezone", mode="after")
    @classmethod
    def validate_scheduled_timezone(cls, value: str | None) -> str | None:
        return validate_timezone_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_aware_schedule(self) -> "CreateMeetingProposal":
        if self.scheduled_at.tzinfo is None or self.scheduled_at.utcoffset() is None:
            raise ValueError("Meeting date and time must include an explicit timezone.")
        if self.scheduled_timezone is None:
            self.scheduled_timezone = timezone_name_from_datetime(self.scheduled_at)
        return self


class CreateMeetingTaskInput(ActionInput):
    customer_id: int = Field(ge=1)
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    scheduled_timezone: str | None = None
    scheduled_time_occurrence: Literal["earlier", "later"] | None = None
    contact_id: int | None = Field(default=None, ge=1)
    enquiry_id: int | None = Field(default=None, ge=1)
    duration: int | None = Field(default=None, gt=0, le=1440)
    status: MeetingStatus = "scheduled"
    agenda: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    notes: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)

    @field_validator("scheduled_timezone", mode="after")
    @classmethod
    def validate_scheduled_timezone(cls, value: str | None) -> str | None:
        return validate_timezone_name(value) if value is not None else None

    def resolve_scheduled_at(self) -> datetime:
        if (
            self.scheduled_date is None
            or self.scheduled_time is None
            or self.scheduled_timezone is None
        ):
            raise ValueError("Meeting date, time, and timezone are required.")
        return resolve_local_datetime(
            self.scheduled_date,
            self.scheduled_time,
            self.scheduled_timezone,
            self.scheduled_time_occurrence,
        )


class CreateFollowupProposal(ActionInput):
    customer_id: int = Field(ge=1)
    type: str = Field(min_length=1, max_length=50)
    due_date: datetime
    due_timezone: str | None = None
    due_time_occurrence: Literal["earlier", "later"] | None = None
    duration: int | None = Field(default=None, gt=0, le=1440)
    enquiry_id: int | None = Field(default=None, ge=1)
    meeting_id: int | None = Field(default=None, ge=1)
    call_id: int | None = Field(default=None, ge=1)
    status: FollowUpStatus = "pending"
    description: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    assigned_to: str | None = Field(default=None, max_length=255)
    completed_at: datetime | None = None

    @field_validator("due_timezone", mode="after")
    @classmethod
    def validate_due_timezone(cls, value: str | None) -> str | None:
        return validate_timezone_name(value) if value is not None else None

    @model_validator(mode="after")
    def normalize_due_date(self) -> "CreateFollowupProposal":
        if self.due_timezone is None:
            self.due_timezone = (
                timezone_name_from_datetime(self.due_date)
                if self.due_date.tzinfo is not None
                and self.due_date.utcoffset() is not None
                else "UTC"
            )
        self.due_date = normalize_utc_datetime(self.due_date)
        assert self.due_date is not None
        return self

    @field_validator("completed_at", mode="after")
    @classmethod
    def ensure_utc_completed_at(cls, value: datetime | None) -> datetime | None:
        return normalize_utc_datetime(value)


class RecordCallResultProposal(ActionInput):
    customer_id: int = Field(ge=1)
    contact_id: int | None = Field(default=None, ge=1)
    enquiry_id: int | None = Field(default=None, ge=1)
    call_type: str | None = Field(default=None, max_length=50)
    actual_time: datetime | None = None
    status: Literal["completed"] = "completed"
    outcome: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    summary: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    next_followup_date: datetime | None = None

    @field_validator("actual_time", "next_followup_date", mode="after")
    @classmethod
    def ensure_utc_datetimes(cls, value: datetime | None) -> datetime | None:
        return normalize_utc_datetime(value)


class ScheduleCallProposal(ActionInput):
    customer_id: int = Field(ge=1)
    contact_id: int | None = Field(default=None, ge=1)
    enquiry_id: int | None = Field(default=None, ge=1)
    call_type: str | None = Field(default=None, max_length=50)
    scheduled_at: datetime
    scheduled_timezone: str
    scheduled_time_occurrence: Literal["earlier", "later"] | None = None
    duration: int | None = Field(default=None, gt=0, le=1440)
    status: Literal["scheduled"] = "scheduled"
    notes: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)

    @field_validator("scheduled_timezone", mode="after")
    @classmethod
    def validate_schedule_timezone(cls, value: str) -> str:
        return validate_timezone_name(value)

    @model_validator(mode="after")
    def require_aware_schedule(self) -> "ScheduleCallProposal":
        if self.scheduled_at.tzinfo is None or self.scheduled_at.utcoffset() is None:
            raise ValueError("A scheduled call requires a timezone-aware start time.")
        return self


class CompleteFollowupProposal(ActionInput):
    customer_id: int = Field(ge=1)
    followup_id: int = Field(ge=1)

class ApplyEnrichmentProposal(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    customer_id: int = Field(ge=1)
    enquiry_id: int | None = Field(default=None, ge=1)

    sales_stage: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
    )
    customer_status: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
    )
    enquiry_priority: EnquiryPriority | None = None
    enquiry_status: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
    )
    estimated_value: Decimal | None = Field(
        default=None,
        ge=0,
    )


class UpdateLeadQualificationProposal(ActionInput):
    """Qualification (Ready / Not Ready) of an open lead. Never an opportunity outcome."""

    customer_id: int = Field(ge=1)
    enquiry_id: int = Field(ge=1)
    qualification_status: QualificationStatus
    qualification_reason: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)


class CloseOpportunityInput(ActionInput):
    """Outcome (Won / Lost) of an active opportunity. Never a qualification status."""

    customer_id: int = Field(ge=1)
    enquiry_id: int = Field(ge=1)
    outcome: OpportunityOutcome
    loss_reason: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)


class CloseOpportunityProposal(CloseOpportunityInput):

    @model_validator(mode="after")
    def loss_reason_matches_outcome(self) -> "CloseOpportunityProposal":
        OpportunityClose(outcome=self.outcome, loss_reason=self.loss_reason)
        return self
