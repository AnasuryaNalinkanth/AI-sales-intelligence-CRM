"""Sales lead schemas. Leads are stored as sales enquiries; field names stay compatible."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.validators import (
    MAX_MONETARY_VALUE,
    MAX_TEXT_LENGTH,
    normalize_utc_datetime,
    reject_explicit_nulls,
    validate_monetary_decimal,
)

EnquiryPriority = Literal["low", "medium", "normal", "high", "urgent"]
# Pipeline stage of the lead. converted/closed/lost are legacy terminal values;
# closing an opportunity sets outcome (won/lost) and moves the stage to converted/lost.
EnquiryStatus = Literal["open", "in_progress", "converted", "closed", "lost"]
LeadSource = Literal["website", "referral", "campaign", "manual", "import", "other"]
# Qualification and outcome are separate fields and never share values.
QualificationStatus = Literal["ready", "not_ready"]
OpportunityOutcome = Literal["won", "lost"]


class SalesEnquiryCreate(BaseModel):
    """A new sales lead for an existing customer."""

    model_config = ConfigDict(str_strip_whitespace=True)

    customer_id: int = Field(ge=1)
    product: str | None = Field(default=None, max_length=255, description="Product or service of interest.")
    enquiry_text: str = Field(
        min_length=1, max_length=MAX_TEXT_LENGTH, description="Lead requirements and notes."
    )
    priority: EnquiryPriority = "normal"
    status: EnquiryStatus = Field(default="open", description="Pipeline stage of the lead.")
    estimated_value: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=MAX_MONETARY_VALUE,
    )
    contact_id: int | None = Field(default=None, ge=1, description="Contact of the same customer.")
    assigned_user_id: int | None = Field(default=None, ge=1, description="Assigned sales representative.")
    lead_source: LeadSource | None = None
    expected_close_date: date | None = None

    @field_validator("estimated_value", mode="after")
    @classmethod
    def check_monetary_value(cls, value: Decimal | None) -> Decimal | None:
        return validate_monetary_decimal(value)


class SalesLeadCreate(SalesEnquiryCreate):
    """Create request: a human may set the initial qualification explicitly."""

    qualification_status: QualificationStatus = "not_ready"
    qualification_reason: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)


class SalesEnquiryUpdate(BaseModel):
    """Ordinary edits. Qualification and outcome change only through their own endpoints."""

    model_config = ConfigDict(str_strip_whitespace=True)

    customer_id: int | None = Field(default=None, ge=1)
    product: str | None = Field(default=None, max_length=255)
    enquiry_text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_LENGTH)
    priority: EnquiryPriority | None = None
    status: EnquiryStatus | None = None
    estimated_value: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=MAX_MONETARY_VALUE,
    )
    contact_id: int | None = Field(default=None, ge=1)
    assigned_user_id: int | None = Field(default=None, ge=1)
    lead_source: LeadSource | None = None
    expected_close_date: date | None = None

    @field_validator("estimated_value", mode="after")
    @classmethod
    def check_monetary_value(cls, value: Decimal | None) -> Decimal | None:
        return validate_monetary_decimal(value)

    @model_validator(mode="after")
    def validate_non_nullable_fields(self) -> "SalesEnquiryUpdate":
        return reject_explicit_nulls(
            self,
            ("customer_id", "enquiry_text", "priority", "status"),
        )


class LeadFollowUpInput(BaseModel):
    """Next follow-up scheduled with a Not Ready decision, through the follow-up service."""

    model_config = ConfigDict(str_strip_whitespace=True)

    type: str = Field(min_length=1, max_length=50)
    due_date: datetime
    due_timezone: str | None = None
    description: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)


class LeadQualificationUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    qualification_status: QualificationStatus
    qualification_reason: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    next_followup: LeadFollowUpInput | None = None

    @model_validator(mode="after")
    def followup_only_when_not_ready(self) -> "LeadQualificationUpdate":
        if self.next_followup is not None and self.qualification_status != "not_ready":
            raise ValueError("A next follow-up can only be scheduled for a Not Ready lead.")
        return self


class OpportunityClose(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    outcome: OpportunityOutcome
    loss_reason: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)

    @model_validator(mode="after")
    def loss_reason_matches_outcome(self) -> "OpportunityClose":
        if self.outcome == "lost" and not self.loss_reason:
            raise ValueError("A loss reason is required when an opportunity is lost.")
        if self.outcome == "won" and self.loss_reason:
            raise ValueError("A won opportunity cannot have a loss reason.")
        return self


class SalesEnquiryResponse(SalesEnquiryCreate):
    model_config = ConfigDict(from_attributes=True)

    enquiry_id: int = Field(ge=1)
    qualification_status: QualificationStatus
    qualification_reason: str | None = None
    outcome: OpportunityOutcome | None = None
    closed_at: datetime | None = None
    loss_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def ensure_utc_timestamps(cls, value: datetime) -> datetime:
        normalized = normalize_utc_datetime(value)
        assert normalized is not None
        return normalized

    @field_validator("closed_at", mode="after")
    @classmethod
    def ensure_utc_closed_at(cls, value: datetime | None) -> datetime | None:
        return normalize_utc_datetime(value)


class SalesLeadListItem(SalesEnquiryResponse):
    customer_name: str
    company_name: str
    contact_name: str | None = None
    assigned_user_name: str | None = None


class SalesLeadListResponse(BaseModel):
    items: list[SalesLeadListItem]
    page: int
    page_size: int
    total: int


class LeadAssignee(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    full_name: str
