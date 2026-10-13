from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

AttentionReasonCode = Literal[
    "overdue_followup",
    "no_recent_activity",
    "no_next_step",
    "high_priority",
    "missing_value",
]


SuggestedActionKind = Literal["schedule_call", "create_followup", "review_followups", "review_value"]


class SuggestedAction(BaseModel):
    """A deterministic next step derived from an item's reason codes.

    Task kinds (schedule_call, create_followup) name an existing structured-task action;
    choosing one only prepares a draft task. Review kinds are links, never writes.
    """

    kind: SuggestedActionKind
    category: Literal["workflow", "data_quality"]
    label: str
    reason_codes: list[AttentionReasonCode]
    opens_task: bool
    # Values taken from this enquiry's own record; never from customer-level context.
    prefill: dict[str, int]
    # Task field the browser fills with the user's own timezone, if any.
    timezone_field: str | None = None
    # Required task fields the user must enter (from the action registry).
    user_must_provide: list[str]


class AttentionReason(BaseModel):
    code: AttentionReasonCode
    detail: str


class AttentionItem(BaseModel):
    enquiry_id: int
    customer_id: int
    customer_name: str
    company_name: str
    product: str | None
    status: str
    priority: str
    estimated_value: Decimal | None
    enquiry_created_at: datetime
    last_activity_at: datetime | None
    days_since_activity: int
    activity_measured_from: Literal["last_activity", "enquiry_created"]
    next_step_at: datetime | None
    overdue_followup_count: int
    oldest_overdue_due_at: datetime | None
    # Records linked to the customer but to no enquiry. Context only: they never add or
    # clear a reason, because they cannot be attributed to this enquiry.
    customer_level_last_activity_at: datetime | None
    customer_level_next_step_at: datetime | None
    customer_level_overdue_followup_count: int
    customer_level_oldest_overdue_due_at: datetime | None
    reasons: list[AttentionReason]
    suggested_actions: list[SuggestedAction]


class AttentionQueue(BaseModel):
    generated_at: datetime
    today_utc: date
    stale_days: int
    new_enquiry_grace_days: int
    open_enquiries: int
    needing_attention: int
    ordering: list[str]
    items: list[AttentionItem]


InsightCategory = Literal["overdue_followup", "stalled_deal", "neglected_lead", "needs_attention"]
InsightLevel = Literal["low", "medium", "high"]


class InsightEvidence(BaseModel):
    # fact: read from a record. estimate: derived from incomplete data (labelled so).
    # missing_data: something the rules needed but the CRM does not record.
    kind: Literal["fact", "estimate", "missing_data"]
    detail: str
    record_type: Literal["customer", "enquiry", "followup", "meeting"] | None = None
    record_id: int | None = None


class InsightLink(BaseModel):
    label: str
    record_type: Literal["customer", "enquiry", "followup", "meeting"]
    record_id: int
    # A path in this app; enquiries, follow-ups and meetings open on their customer's page.
    href: str


class ProactiveInsight(BaseModel):
    recommendation_id: str
    category: InsightCategory
    title: str
    summary: str
    reason: str
    urgency: InsightLevel
    potential_impact: InsightLevel
    priority_score: int
    urgency_score: int
    impact_score: int
    # Normalized 0-1 inputs; weights are in ProactiveInsights.rules.
    score_components: dict[str, float]
    evidence: list[InsightEvidence]
    recommended_next_action: str
    customer_id: int
    customer_name: str
    company_name: str
    enquiry_id: int | None
    followup_id: int | None
    meeting_id: int | None
    links: list[InsightLink]
    suggested_actions: list[SuggestedAction]
    evaluated_at: datetime


class ProactiveInsights(BaseModel):
    generated_at: datetime
    today_utc: date
    rules: dict[str, object]
    # All recommendations for the organization, before filters.
    counts_by_category: dict[InsightCategory, int]
    total: int
    page: int
    page_size: int
    items: list[ProactiveInsight]
