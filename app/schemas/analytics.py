from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TimeGrain = Literal["daily", "weekly", "monthly"]
# Every analytics date filter must fall inside these bounds, which together are the widest
# permitted range. They keep date arithmetic (end_date + 1 day) and database datetimes valid.
MIN_ANALYTICS_DATE = date(1900, 1, 1)
MAX_ANALYTICS_DATE = date(2100, 12, 31)


def date_range_error(start_date: date | None, end_date: date | None) -> str | None:
    """The one analytics date rule: None when valid, otherwise a message for the caller."""
    for value in (start_date, end_date):
        if value is not None and not MIN_ANALYTICS_DATE <= value <= MAX_ANALYTICS_DATE:
            return f"Dates must be between {MIN_ANALYTICS_DATE} and {MAX_ANALYTICS_DATE}."
    if start_date and end_date and start_date > end_date:
        return "start_date must be on or before end_date"
    return None


class AnalyticsDateRange(BaseModel):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def date_range_is_valid(self) -> "AnalyticsDateRange":
        if error := date_range_error(self.start_date, self.end_date):
            raise ValueError(error)
        return self


class DistributionMetric(BaseModel):
    label: str
    count: int
    value: Decimal | None = None


class CountTimePoint(BaseModel):
    period: str
    count: int


class ActivityTimePoint(BaseModel):
    period: str
    meetings: int = 0
    calls: int = 0
    followups: int = 0


class OverviewMetrics(BaseModel):
    total_customers: int
    active_customers: int
    inactive_customers: int
    prospects: int
    new_customers: int
    open_enquiries: int
    closed_enquiries: int
    converted_enquiries: int
    meetings_this_month: int
    calls_this_month: int
    pending_followups: int
    overdue_followups: int
    completed_followups: int


class CustomerMetrics(BaseModel):
    by_status: list[DistributionMetric]
    by_sales_stage: list[DistributionMetric]
    by_industry: list[DistributionMetric]
    created_over_time: list[CountTimePoint]
    time_grain: TimeGrain


class EnquiryMetrics(BaseModel):
    by_status: list[DistributionMetric]
    by_priority: list[DistributionMetric]
    by_product: list[DistributionMetric]
    by_status_value: list[DistributionMetric]
    over_time: list[CountTimePoint]
    total_estimated_value: Decimal
    time_grain: TimeGrain
    # Open leads by qualification (Ready / Not Ready) and closed opportunities by outcome.
    by_qualification: list[DistributionMetric] = []
    by_outcome: list[DistributionMetric] = []
    win_rate_percent: Decimal | None = None
    win_rate_unavailable_reason: str | None = None


class ActivityMetrics(BaseModel):
    over_time: list[ActivityTimePoint]
    meetings_by_status: list[DistributionMetric]
    calls_by_status: list[DistributionMetric]
    followups_by_status: list[DistributionMetric]
    time_grain: TimeGrain


class PipelineMetrics(BaseModel):
    by_sales_stage: list[DistributionMetric]
    enquiry_to_meeting_conversion_percent: Decimal | None
    enquiry_to_meeting_conversion_unavailable_reason: str | None
    meeting_to_proposal_conversion_percent: Decimal | None
    meeting_to_proposal_conversion_unavailable_reason: str
    won_lost_ratio_percent: Decimal | None
    won_lost_ratio_unavailable_reason: str | None


class FollowupMetrics(BaseModel):
    pending: int
    completed: int
    overdue: int
    cancelled: int
    due_today: int
    due_next_7_days: int
    by_status: list[DistributionMetric]


# Approved natural-language analytics operations; the registry in
# analytics_query_service must define exactly these names.
AnalyticsOperation = Literal[
    "customer_totals",
    "new_customers",
    "customers_by_status",
    "customers_by_sales_stage",
    "customers_by_industry",
    "customers_created_over_time",
    "leads_by_status",
    "leads_by_priority",
    "leads_by_product",
    "open_leads_by_qualification",
    "closed_leads_by_outcome",
    "lead_value_by_status",
    "total_lead_value",
    "lead_win_rate",
    "leads_created_over_time",
    "pipeline_value_by_stage",
    "lead_to_meeting_conversion",
    "activity_over_time",
    "meetings_by_status",
    "calls_by_status",
    "followup_summary",
]
AnalyticsResultKind = Literal[
    "number", "percent", "amount", "distribution", "time_series", "activity_series", "overview"
]


class AnalyticsAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=3, max_length=500)


class AnalyticsQueryPlan(BaseModel):
    """The only output the model may produce: one approved operation and its filters.

    The range itself is checked with date_range_error after parsing, so an impossible range
    in the question is reported as a client error rather than as malformed model output.
    month, last_week and last_n_days name a relative period; the service resolves them to
    start_date and end_date from one UTC "today", so the model never does that arithmetic.
    The returned interpretation keeps both, showing the period asked for and the dates run.
    """

    model_config = ConfigDict(extra="forbid")

    # sales_overview runs several approved operations for one period (see the query service).
    operation: Literal[AnalyticsOperation, "sales_overview", "unsupported"]
    start_date: date | None = None
    end_date: date | None = None
    grain: TimeGrain | None = None
    month: int | None = Field(default=None, ge=1, le=12)
    last_week: bool = False
    # The upper limit keeps the date arithmetic valid; date_range_error still applies after.
    last_n_days: int | None = Field(default=None, ge=1, le=100_000)


class AnalyticsAskResult(BaseModel):
    kind: AnalyticsResultKind
    value: Decimal | int | None = None
    items: list[DistributionMetric | CountTimePoint | ActivityTimePoint] | None = None
    # Set when a percentage cannot be calculated; value is then null.
    unavailable_reason: str | None = None
    # True when the figure covers all records and ignores the requested dates.
    all_time: bool = False
    note: str | None = None
    # Set only for kind "overview": one result per approved operation, each with its own label.
    sections: list["AnalyticsOverviewSection"] | None = None


class AnalyticsOverviewSection(BaseModel):
    operation: AnalyticsOperation
    result: AnalyticsAskResult
    summary: str


AnalyticsAskResult.model_rebuild()


class AnalyticsAskTimings(BaseModel):
    interpret: int
    execute: int


class AnalyticsAskResponse(BaseModel):
    status: Literal["answered", "unsupported"]
    question: str
    interpretation: AnalyticsQueryPlan
    result: AnalyticsAskResult | None = None
    summary: str
    timings_ms: AnalyticsAskTimings
