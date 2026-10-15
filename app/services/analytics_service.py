from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import wraps
import logging
from typing import Any, ParamSpec, TypeVar

from sqlalchemy import case, distinct, func, literal, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.exceptions import APIError
from app.core.logging_utils import log_database_exception
from app.database.connection import rollback_failed_transaction
from app.models import Call, Company, Customer, FollowUp, Meeting, SalesEnquiry
from app.schemas.analytics import (
    ActivityMetrics,
    ActivityTimePoint,
    CountTimePoint,
    CustomerMetrics,
    DistributionMetric,
    EnquiryMetrics,
    FollowupMetrics,
    OverviewMetrics,
    PipelineMetrics,
    TimeGrain,
)

logger = logging.getLogger(__name__)
P = ParamSpec("P")
R = TypeVar("R")
OPEN_ENQUIRY_STATUSES = ("open", "in_progress")
OPEN_FOLLOWUP_STATUSES = ("pending", "in_progress")
SALES_STAGE_ORDER = ("new", "qualified", "proposal", "negotiation", "won", "lost")
ZERO = Decimal("0")


def _database_errors(function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except SQLAlchemyError as exc:
            db = args[0] if args and isinstance(args[0], Session) else kwargs.get("db")
            if isinstance(db, Session):
                rollback_failed_transaction(db)
            log_database_exception(logger, function.__name__, exc)
            raise APIError(
                "The analytics query could not be completed.",
                status_code=500,
                code="database_error",
            ) from exc

    return wrapped



def _range_conditions(
    column: ColumnElement[Any],
    start_date: date | None,
    end_date: date | None,
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if start_date is not None:
        conditions.append(
            column >= datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        )
    if end_date is not None:
        conditions.append(
            column
            < datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )
    return conditions


def _period_expression(
    column: ColumnElement[Any],
    grain: TimeGrain,
    db: Session,
) -> ColumnElement[Any]:
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        pg_format = {
            # ISO year with ISO week, so the first days of January land in last year's week.
            "daily": "YYYY-MM-DD",
            "weekly": 'IYYY-"W"IW',
            "monthly": "YYYY-MM",
        }[grain]
        # timestamptz -> UTC wall time, so labels do not depend on the session TimeZone.
        return func.to_char(func.timezone("UTC", column), pg_format)
    format_string = {
        "daily": "%Y-%m-%d",
        "weekly": "%Y-W%u" if dialect == "mysql" else "%Y-W%W",
        "monthly": "%Y-%m",
    }[grain]
    if dialect == "mysql":
        return func.date_format(column, format_string)
    if dialect == "sqlite":
        return func.strftime(format_string, column)
    return func.date(column)


def _distribution(
    rows: list[tuple[object, object]],
) -> list[DistributionMetric]:
    return [
        DistributionMetric(label=str(label), count=int(count or 0))
        for label, count in rows
    ]


def _count_by(
    db: Session,
    column: ColumnElement[Any],
    *,
    conditions: list[ColumnElement[bool]] | None = None,
    value_column: ColumnElement[Any] | None = None,
) -> list[DistributionMetric]:
    label = func.coalesce(column, literal("Unspecified"))
    count_expr = func.count()
    if value_column is not None:
        statement = (
            select(label, count_expr, func.coalesce(func.sum(value_column), 0))
            .select_from(SalesEnquiry)
            .join(Customer, SalesEnquiry.customer_id == Customer.customer_id)
        )
        if conditions:
            statement = statement.where(*conditions)
        statement = statement.group_by(label).order_by(func.count().desc(), label)
        return [
            DistributionMetric(label=str(name), count=int(count or 0), value=value or ZERO)
            for name, count, value in db.execute(statement).all()
        ]

    statement = select(label, count_expr)
    if conditions:
        statement = statement.where(*conditions)
    statement = statement.group_by(label).order_by(func.count().desc(), label)
    return _distribution(list(db.execute(statement).all()))


def _time_series(
    db: Session,
    model: type[Customer] | type[SalesEnquiry] | type[Meeting] | type[Call] | type[FollowUp],
    date_column: ColumnElement[Any],
    grain: TimeGrain,
    start_date: date | None,
    end_date: date | None,
    *,
    organization_id: int,
) -> list[CountTimePoint]:
    period = _period_expression(date_column, grain, db)
    statement = select(period, func.count()).select_from(model)
    conditions = [
        model.organization_id == organization_id,
        *_range_conditions(date_column, start_date, end_date),
    ]
    statement = statement.where(*conditions)
    statement = statement.group_by(period).order_by(period)
    return [
        CountTimePoint(period=str(label), count=int(count or 0))
        for label, count in db.execute(statement).all()
        if label is not None
    ]


def _decimal_percent(numerator: int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return (Decimal(numerator) * Decimal(100) / Decimal(denominator)).quantize(
        Decimal("0.01")
    )


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


@_database_errors
def get_overview_metrics(
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    organization_id: int,
) -> OverviewMetrics:
    customer_counts = db.execute(
        select(
            func.count(Customer.customer_id),
            func.coalesce(
                func.sum(case((Customer.status == "active", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((Customer.status == "inactive", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((Customer.status == "prospect", 1), else_=0)), 0
            ),
        ).where(Customer.organization_id == organization_id)
    ).one()
    new_customers = db.scalar(
        select(func.count())
        .select_from(Customer)
        .where(
            Customer.organization_id == organization_id,
            *_range_conditions(Customer.created_at, start_date, end_date),
        )
    ) or 0
    enquiry_counts = db.execute(
        select(
            func.coalesce(
                func.sum(case((SalesEnquiry.status.in_(OPEN_ENQUIRY_STATUSES), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((SalesEnquiry.status.in_(("closed", "lost")), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((SalesEnquiry.status == "converted", 1), else_=0)), 0
            ),
        ).where(
            SalesEnquiry.organization_id == organization_id,
            *_range_conditions(SalesEnquiry.created_at, start_date, end_date),
        )
    ).one()

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (
        month_start.replace(year=month_start.year + 1, month=1)
        if month_start.month == 12
        else month_start.replace(month=month_start.month + 1)
    )

    def current_month_count(
        model: type[Meeting] | type[Call],
        column: ColumnElement[Any],
    ) -> int:
        conditions: list[ColumnElement[bool]] = [
            model.organization_id == organization_id,
            column >= month_start,
            column < next_month,
        ]
        conditions.extend(_range_conditions(column, start_date, end_date))
        return int(
            db.scalar(
                select(func.count()).select_from(model).where(*conditions)
            )
            or 0
        )

    today = _utc_today()
    overdue_conditions: list[ColumnElement[bool]] = [
        FollowUp.organization_id == organization_id,
        (FollowUp.status == "overdue")
        | (
            FollowUp.status.in_(OPEN_FOLLOWUP_STATUSES)
            & (FollowUp.due_date < datetime.combine(today, time.min, tzinfo=timezone.utc))
        )
    ]
    open_followup_conditions: list[ColumnElement[bool]] = [
        FollowUp.organization_id == organization_id,
        FollowUp.status.in_(OPEN_FOLLOWUP_STATUSES),
        FollowUp.due_date >= datetime.combine(today, time.min, tzinfo=timezone.utc),
        *_range_conditions(FollowUp.due_date, start_date, end_date),
    ]
    completed_followup_conditions: list[ColumnElement[bool]] = [
        FollowUp.organization_id == organization_id,
        FollowUp.status == "completed",
        *_range_conditions(FollowUp.due_date, start_date, end_date),
    ]
    overdue_conditions.extend(_range_conditions(FollowUp.due_date, start_date, end_date))

    def followup_count(conditions: list[ColumnElement[bool]]) -> int:
        return int(
            db.scalar(
                select(func.count()).select_from(FollowUp).where(*conditions)
            )
            or 0
        )

    return OverviewMetrics(
        total_customers=int(customer_counts[0] or 0),
        active_customers=int(customer_counts[1] or 0),
        inactive_customers=int(customer_counts[2] or 0),
        prospects=int(customer_counts[3] or 0),
        new_customers=int(new_customers),
        open_enquiries=int(enquiry_counts[0] or 0),
        closed_enquiries=int(enquiry_counts[1] or 0),
        converted_enquiries=int(enquiry_counts[2] or 0),
        meetings_this_month=current_month_count(Meeting, Meeting.scheduled_at),
        calls_this_month=current_month_count(
            Call, func.coalesce(Call.actual_time, Call.scheduled_at, Call.created_at)
        ),
        pending_followups=followup_count(open_followup_conditions),
        overdue_followups=followup_count(overdue_conditions),
        completed_followups=followup_count(completed_followup_conditions),
    )


@_database_errors
def get_customer_metrics(
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    grain: TimeGrain = "monthly",
    *,
    organization_id: int,
) -> CustomerMetrics:
    conditions = [
        Customer.organization_id == organization_id,
        *_range_conditions(Customer.created_at, start_date, end_date),
    ]
    by_status = _count_by(db, Customer.status, conditions=conditions)
    by_sales_stage = _count_by(db, Customer.sales_stage, conditions=conditions)
    industry_label = func.coalesce(Company.industry, literal("Unspecified"))
    industry_query = (
        select(industry_label, func.count(Customer.customer_id))
        .select_from(Customer)
        .join(Company, Customer.company_id == Company.company_id)
        .where(*conditions)
        .group_by(industry_label)
        .order_by(func.count(Customer.customer_id).desc(), industry_label)
    )
    by_industry = _distribution(list(db.execute(industry_query).all()))
    return CustomerMetrics(
        by_status=by_status,
        by_sales_stage=by_sales_stage,
        by_industry=by_industry,
        created_over_time=_time_series(
            db, Customer, Customer.created_at, grain, start_date, end_date,
            organization_id=organization_id,
        ),
        time_grain=grain,
    )


@_database_errors
def get_enquiry_metrics(
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    grain: TimeGrain = "monthly",
    *,
    organization_id: int,
) -> EnquiryMetrics:
    conditions = [
        SalesEnquiry.organization_id == organization_id,
        *_range_conditions(SalesEnquiry.created_at, start_date, end_date),
    ]
    by_status = _count_by(db, SalesEnquiry.status, conditions=conditions)
    by_priority = _count_by(db, SalesEnquiry.priority, conditions=conditions)
    by_product = _count_by(db, SalesEnquiry.product, conditions=conditions)
    value_by_status = _count_by(
        db,
        SalesEnquiry.status,
        conditions=conditions,
        value_column=SalesEnquiry.estimated_value,
    )
    total_value = db.scalar(
        select(func.coalesce(func.sum(SalesEnquiry.estimated_value), 0))
        .select_from(SalesEnquiry)
        .where(*conditions)
    )
    # Qualification describes open leads; Won/Lost are the outcomes of closed ones.
    by_qualification = _count_by(
        db,
        SalesEnquiry.qualification_status,
        conditions=[*conditions, SalesEnquiry.outcome.is_(None)],
        value_column=SalesEnquiry.estimated_value,
    )
    by_outcome = _count_by(
        db,
        SalesEnquiry.outcome,
        conditions=[*conditions, SalesEnquiry.outcome.is_not(None)],
        value_column=SalesEnquiry.estimated_value,
    )
    outcome_counts = {item.label: item.count for item in by_outcome}
    won = outcome_counts.get("won", 0)
    win_rate = _decimal_percent(won, won + outcome_counts.get("lost", 0))
    return EnquiryMetrics(
        by_qualification=by_qualification,
        by_outcome=by_outcome,
        win_rate_percent=win_rate,
        win_rate_unavailable_reason=(
            None if win_rate is not None
            else "No opportunities created in the selected period have been closed as won or lost."
        ),
        by_status=by_status,
        by_priority=by_priority,
        by_product=by_product,
        by_status_value=value_by_status,
        over_time=_time_series(
            db,
            SalesEnquiry,
            SalesEnquiry.created_at,
            grain,
            start_date,
            end_date,
            organization_id=organization_id,
        ),
        total_estimated_value=total_value or ZERO,
        time_grain=grain,
    )


@_database_errors
def get_activity_metrics(
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    grain: TimeGrain = "monthly",
    *,
    organization_id: int,
) -> ActivityMetrics:
    call_date = func.coalesce(Call.actual_time, Call.scheduled_at, Call.created_at)
    series: dict[str, dict[str, int]] = {}
    models = (
        ("meetings", Meeting, Meeting.scheduled_at),
        ("calls", Call, call_date),
        ("followups", FollowUp, FollowUp.due_date),
    )
    for name, model, date_column in models:
        for item in _time_series(
            db, model, date_column, grain, start_date, end_date,
            organization_id=organization_id,
        ):
            series.setdefault(item.period, {"meetings": 0, "calls": 0, "followups": 0})
            series[item.period][name] = item.count
    over_time = [
        ActivityTimePoint(period=period, **series[period])
        for period in sorted(series)
    ]
    return ActivityMetrics(
        over_time=over_time,
        meetings_by_status=_count_by(
            db,
            Meeting.status,
            conditions=[
                Meeting.organization_id == organization_id,
                *_range_conditions(Meeting.scheduled_at, start_date, end_date),
            ],
        ),
        calls_by_status=_count_by(
            db,
            Call.status,
            conditions=[
                Call.organization_id == organization_id,
                *_range_conditions(call_date, start_date, end_date),
            ],
        ),
        followups_by_status=_count_by(
            db,
            FollowUp.status,
            conditions=[
                FollowUp.organization_id == organization_id,
                *_range_conditions(FollowUp.due_date, start_date, end_date),
            ],
        ),
        time_grain=grain,
    )


@_database_errors
def get_pipeline_metrics(
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    organization_id: int,
) -> PipelineMetrics:
    enquiry_conditions = [
        SalesEnquiry.organization_id == organization_id,
        *_range_conditions(SalesEnquiry.created_at, start_date, end_date),
    ]
    grouped = db.execute(
        select(
            Customer.sales_stage,
            func.count(SalesEnquiry.enquiry_id),
            func.coalesce(func.sum(SalesEnquiry.estimated_value), 0),
        )
        .select_from(Customer)
        .join(SalesEnquiry, SalesEnquiry.customer_id == Customer.customer_id)
        .where(*enquiry_conditions)
        .group_by(Customer.sales_stage)
    ).all()
    grouped_values = {
        str(stage): (int(count or 0), value or ZERO)
        for stage, count, value in grouped
    }
    actual_stages = set(
        db.scalars(
            select(Customer.sales_stage)
            .where(Customer.organization_id == organization_id)
            .distinct()
        ).all()
    )
    stage_order = [stage for stage in SALES_STAGE_ORDER if stage in actual_stages]
    stage_order.extend(sorted(str(stage) for stage in actual_stages if stage not in SALES_STAGE_ORDER))
    by_sales_stage = [
        DistributionMetric(
            label=stage,
            count=grouped_values.get(stage, (0, ZERO))[0],
            value=grouped_values.get(stage, (0, ZERO))[1],
        )
        for stage in stage_order
    ]

    enquiry_count = int(
        db.scalar(
            select(func.count())
            .select_from(SalesEnquiry)
            .where(*enquiry_conditions)
        )
        or 0
    )
    meeting_conditions = [
        Meeting.organization_id == organization_id,
        *_range_conditions(Meeting.scheduled_at, start_date, end_date),
    ]
    meetings_for_enquiry = int(
        db.scalar(
            select(func.count(distinct(Meeting.enquiry_id)))
            .select_from(Meeting)
            .join(SalesEnquiry, Meeting.enquiry_id == SalesEnquiry.enquiry_id)
            .where(Meeting.enquiry_id.is_not(None), *meeting_conditions, *enquiry_conditions)
        )
        or 0
    )
    conversion = _decimal_percent(meetings_for_enquiry, enquiry_count)
    conversion_reason = (
        None
        if conversion is not None
        else "No sales leads exist in the selected period."
    )

    won_lost = db.execute(
        select(
            func.coalesce(func.sum(case((Customer.sales_stage == "won", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Customer.sales_stage == "lost", 1), else_=0)), 0),
        ).where(
            Customer.organization_id == organization_id,
            *_range_conditions(Customer.created_at, start_date, end_date),
        )
    ).one()
    closed_customer_count = int(won_lost[0] or 0) + int(won_lost[1] or 0)
    won_lost_ratio = _decimal_percent(int(won_lost[0] or 0), closed_customer_count)

    return PipelineMetrics(
        by_sales_stage=by_sales_stage,
        enquiry_to_meeting_conversion_percent=conversion,
        enquiry_to_meeting_conversion_unavailable_reason=conversion_reason,
        meeting_to_proposal_conversion_percent=None,
        meeting_to_proposal_conversion_unavailable_reason=(
            "The CRM does not store a proposal outcome linked to an individual meeting."
        ),
        won_lost_ratio_percent=won_lost_ratio,
        won_lost_ratio_unavailable_reason=(
            None
            if won_lost_ratio is not None
            else "No customers in the selected period are in the won or lost sales stage."
        ),
    )


@_database_errors
def get_followup_metrics(
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    organization_id: int,
) -> FollowupMetrics:
    today = _utc_today()
    today_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    tomorrow = today + timedelta(days=1)
    in_seven_days = today + timedelta(days=8)
    base_conditions = [
        FollowUp.organization_id == organization_id,
        *_range_conditions(FollowUp.due_date, start_date, end_date),
    ]

    def count(conditions: list[ColumnElement[bool]]) -> int:
        return int(
            db.scalar(
                select(func.count())
                .select_from(FollowUp)
                .where(*conditions)
            )
            or 0
        )

    overdue = count(
        [
            (FollowUp.status == "overdue")
            | (
                FollowUp.status.in_(OPEN_FOLLOWUP_STATUSES)
                & (FollowUp.due_date < today_start)
            ),
            *base_conditions,
        ]
    )
    pending = count(
        [
            FollowUp.status.in_(OPEN_FOLLOWUP_STATUSES),
            FollowUp.due_date >= today_start,
            *base_conditions,
        ]
    )
    completed = count([FollowUp.status == "completed", *base_conditions])
    cancelled = count([FollowUp.status == "cancelled", *base_conditions])
    due_today = count(
        [
            FollowUp.status.in_(OPEN_FOLLOWUP_STATUSES),
            FollowUp.due_date >= today_start,
            FollowUp.due_date < datetime.combine(tomorrow, time.min, tzinfo=timezone.utc),
            *base_conditions,
        ]
    )
    due_next_7_days = count(
        [
            FollowUp.status.in_(OPEN_FOLLOWUP_STATUSES),
            FollowUp.due_date >= datetime.combine(tomorrow, time.min, tzinfo=timezone.utc),
            FollowUp.due_date < datetime.combine(in_seven_days, time.min, tzinfo=timezone.utc),
            *base_conditions,
        ]
    )
    return FollowupMetrics(
        pending=pending,
        completed=completed,
        overdue=overdue,
        cancelled=cancelled,
        due_today=due_today,
        due_next_7_days=due_next_7_days,
        by_status=[
            DistributionMetric(label="pending", count=pending),
            DistributionMetric(label="completed", count=completed),
            DistributionMetric(label="overdue", count=overdue),
            DistributionMetric(label="cancelled", count=cancelled),
        ],
    )
