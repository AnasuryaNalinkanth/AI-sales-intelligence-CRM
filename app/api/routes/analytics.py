from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.database.connection import get_db
from app.models import User
from app.schemas.analytics import (
    ActivityMetrics,
    AnalyticsDateRange,
    CustomerMetrics,
    EnquiryMetrics,
    FollowupMetrics,
    OverviewMetrics,
    PipelineMetrics,
    TimeGrain,
)
from app.schemas.intelligence import (
    AttentionQueue,
    InsightCategory,
    InsightLevel,
    ProactiveInsights,
)
from app.services import analytics_service, intelligence_service, proactive_insights_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _date_range(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> AnalyticsDateRange:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="start_date must be on or before end_date",
        )
    return AnalyticsDateRange(start_date=start_date, end_date=end_date)


DateRangeDependency = Annotated[AnalyticsDateRange, Depends(_date_range)]


@router.get("/overview", response_model=OverviewMetrics)
def get_overview(
    date_range: DateRangeDependency,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OverviewMetrics:
    return analytics_service.get_overview_metrics(
        db, date_range.start_date, date_range.end_date, organization_id=current_user.organization_id
    )


@router.get("/customers", response_model=CustomerMetrics)
def get_customers(
    date_range: DateRangeDependency,
    db: Session = Depends(get_db),
    grain: TimeGrain = Query(default="monthly"),
    current_user: User = Depends(get_current_user),
) -> CustomerMetrics:
    return analytics_service.get_customer_metrics(
        db,
        date_range.start_date,
        date_range.end_date,
        grain,
        organization_id=current_user.organization_id,
    )


@router.get("/enquiries", response_model=EnquiryMetrics)
def get_enquiries(
    date_range: DateRangeDependency,
    db: Session = Depends(get_db),
    grain: TimeGrain = Query(default="monthly"),
    current_user: User = Depends(get_current_user),
) -> EnquiryMetrics:
    return analytics_service.get_enquiry_metrics(
        db,
        date_range.start_date,
        date_range.end_date,
        grain,
        organization_id=current_user.organization_id,
    )


@router.get("/activities", response_model=ActivityMetrics)
def get_activities(
    date_range: DateRangeDependency,
    db: Session = Depends(get_db),
    grain: TimeGrain = Query(default="monthly"),
    current_user: User = Depends(get_current_user),
) -> ActivityMetrics:
    return analytics_service.get_activity_metrics(
        db,
        date_range.start_date,
        date_range.end_date,
        grain,
        organization_id=current_user.organization_id,
    )


@router.get("/pipeline", response_model=PipelineMetrics)
def get_pipeline(
    date_range: DateRangeDependency,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PipelineMetrics:
    return analytics_service.get_pipeline_metrics(
        db, date_range.start_date, date_range.end_date, organization_id=current_user.organization_id
    )


@router.get("/followups", response_model=FollowupMetrics)
def get_followups(
    date_range: DateRangeDependency,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FollowupMetrics:
    return analytics_service.get_followup_metrics(
        db, date_range.start_date, date_range.end_date, organization_id=current_user.organization_id
    )


@router.get("/attention", response_model=AttentionQueue)
def get_attention_queue(
    db: Session = Depends(get_db),
    stale_days: int = Query(default=intelligence_service.DEFAULT_STALE_DAYS, ge=1, le=365),
    limit: int = Query(default=intelligence_service.DEFAULT_LIMIT, ge=1, le=200),
    new_enquiry_grace_days: int = Query(
        default=intelligence_service.DEFAULT_NEW_ENQUIRY_GRACE_DAYS, ge=0, le=30
    ),
    current_user: User = Depends(get_current_user),
) -> AttentionQueue:
    return intelligence_service.get_attention_queue(
        db,
        now=datetime.now(timezone.utc),
        organization_id=current_user.organization_id,
        stale_days=stale_days,
        limit=limit,
        new_enquiry_grace_days=new_enquiry_grace_days,
    )


@router.get("/proactive-insights", response_model=ProactiveInsights)
def get_proactive_insights(
    db: Session = Depends(get_db),
    category: InsightCategory | None = Query(default=None),
    urgency: InsightLevel | None = Query(default=None),
    min_score: int = Query(default=0, ge=0, le=100),
    customer_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
) -> ProactiveInsights:
    return proactive_insights_service.get_proactive_insights(
        db,
        organization_id=current_user.organization_id,
        now=datetime.now(timezone.utc),
        category=category,
        urgency=urgency,
        min_score=min_score,
        customer_id=customer_id,
        page=page,
        page_size=page_size,
    )


@router.get("/test")
def test_analytics() -> dict[str, str]:
    return {"status": "ok", "module": "analytics"}
