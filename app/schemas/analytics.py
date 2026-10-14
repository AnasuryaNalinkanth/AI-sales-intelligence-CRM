from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, model_validator

TimeGrain = Literal["daily", "weekly", "monthly"]


class AnalyticsDateRange(BaseModel):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def date_range_is_valid(self) -> "AnalyticsDateRange":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
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
