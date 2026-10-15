"""Natural-language analytics: the model picks one approved operation, existing services compute it.

The model sees only the question, the operation catalog and today's UTC date, never CRM
records. Its output is a validated AnalyticsQueryPlan; the organization always comes from
the authenticated user, and summaries are built from templates, not generated.
"""

import asyncio
import calendar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import inspect
import logging
import time
from typing import get_args, get_type_hints

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.agent.model import create_chat_model
from app.agent.prompts import ANALYTICS_QUERY_SYSTEM_PROMPT
from app.agent.structured import ainvoke_structured_output
from app.core.config import settings
from app.core.exceptions import APIError
from app.schemas.analytics import (
    ActivityTimePoint,
    AnalyticsAskResponse,
    AnalyticsAskResult,
    AnalyticsAskTimings,
    AnalyticsOperation,
    AnalyticsQueryPlan,
    AnalyticsResultKind,
    CountTimePoint,
    DistributionMetric,
    date_range_error,
)
from app.services import analytics_service as metrics

logger = logging.getLogger(__name__)
MAX_SUMMARY_ITEMS = 8
UNSUPPORTED_SUMMARY = (
    "This question cannot be answered from the approved analytics, or its period is too "
    "vague to pin to dates. Ask about customers, sales leads, pipeline value, win rate, "
    "meetings, calls or follow-ups for a specific period, such as March 2026 or the last "
    "30 days."
)
GRAIN_UNITS = {"daily": "day", "weekly": "week", "monthly": "month"}


@dataclass(frozen=True)
class AnalyticsOperationSpec:
    label: str  # Used in summaries.
    description: str  # Shown to the model; states the exact semantics.
    fetch: Callable[..., BaseModel]  # An existing analytics_service function.
    fields: tuple[str, ...]  # Response fields returned; several become one distribution.
    kind: AnalyticsResultKind
    basis: str = ""  # Which records the dates select, e.g. "leads created"; shown in summaries.
    uses_grain: bool = False
    all_time: bool = False
    reason_field: str | None = None
    note: str | None = None
    # Fields measured against today, dropped (with past_note) when the period has ended.
    current_fields: tuple[str, ...] = ()
    past_note: str | None = None


ALL_TIME_NOTE = "All-time figure: these counts ignore any requested dates."

OPERATIONS: dict[str, AnalyticsOperationSpec] = {
    "customer_totals": AnalyticsOperationSpec(
        "Customers by status (all time)",
        "Current number of customers in total and by status (active, inactive, prospect). "
        "Always all time; dates do not apply.",
        metrics.get_overview_metrics,
        ("total_customers", "active_customers", "inactive_customers", "prospects"),
        "distribution",
        all_time=True,
        note=ALL_TIME_NOTE,
    ),
    "new_customers": AnalyticsOperationSpec(
        "New customers",
        "Number of customers created in the period.",
        metrics.get_overview_metrics,
        ("new_customers",),
        "number",
        basis="created",
    ),
    "customers_by_status": AnalyticsOperationSpec(
        "Customers by status",
        "Customers created in the period, grouped by status (active, inactive, prospect).",
        metrics.get_customer_metrics,
        ("by_status",),
        "distribution",
        basis="customers created",
    ),
    "customers_by_sales_stage": AnalyticsOperationSpec(
        "Customers by sales stage",
        "Customers created in the period, grouped by sales stage "
        "(new, qualified, proposal, negotiation, won, lost).",
        metrics.get_customer_metrics,
        ("by_sales_stage",),
        "distribution",
        basis="customers created",
    ),
    "customers_by_industry": AnalyticsOperationSpec(
        "Customers by industry",
        "Customers created in the period, grouped by their company's industry.",
        metrics.get_customer_metrics,
        ("by_industry",),
        "distribution",
        basis="customers created",
    ),
    "customers_created_over_time": AnalyticsOperationSpec(
        "Customers created over time",
        "Number of customers created per day, week or month.",
        metrics.get_customer_metrics,
        ("created_over_time",),
        "time_series",
        basis="created",
        uses_grain=True,
    ),
    "leads_by_status": AnalyticsOperationSpec(
        "Sales leads by status",
        "Sales leads created in the period, grouped by status "
        "(open, in_progress, converted, closed, lost).",
        metrics.get_enquiry_metrics,
        ("by_status",),
        "distribution",
        basis="leads created",
    ),
    "leads_by_priority": AnalyticsOperationSpec(
        "Sales leads by priority",
        "Sales leads created in the period, grouped by priority "
        "(low, medium, normal, high, urgent).",
        metrics.get_enquiry_metrics,
        ("by_priority",),
        "distribution",
        basis="leads created",
    ),
    "leads_by_product": AnalyticsOperationSpec(
        "Sales leads by product",
        "Sales leads created in the period, grouped by product.",
        metrics.get_enquiry_metrics,
        ("by_product",),
        "distribution",
        basis="leads created",
    ),
    "open_leads_by_qualification": AnalyticsOperationSpec(
        "Open sales leads by qualification",
        "Sales leads created in the period without a won/lost outcome, grouped by "
        "qualification (ready, not_ready), with estimated value.",
        metrics.get_enquiry_metrics,
        ("by_qualification",),
        "distribution",
        basis="leads created",
    ),
    "closed_leads_by_outcome": AnalyticsOperationSpec(
        "Closed sales leads by outcome",
        "Sales leads created in the period that were closed, grouped by outcome "
        "(won, lost), with estimated value.",
        metrics.get_enquiry_metrics,
        ("by_outcome",),
        "distribution",
        basis="leads created",
        note="Outcomes count whenever the lead was closed; leads closed in this period but "
        "created earlier are not included.",
    ),
    "lead_value_by_status": AnalyticsOperationSpec(
        "Sales lead count and estimated value by status",
        "Count and total estimated value of sales leads created in the period, by status.",
        metrics.get_enquiry_metrics,
        ("by_status_value",),
        "distribution",
        basis="leads created",
    ),
    "total_lead_value": AnalyticsOperationSpec(
        "Total estimated sales lead value",
        "Total estimated value of sales leads created in the period.",
        metrics.get_enquiry_metrics,
        ("total_estimated_value",),
        "amount",
        basis="leads created",
    ),
    "lead_win_rate": AnalyticsOperationSpec(
        "Sales lead win rate",
        "Win rate: won / (won + lost) for sales leads created in the period that have an "
        "outcome.",
        metrics.get_enquiry_metrics,
        ("win_rate_percent",),
        "percent",
        basis="leads created",
        reason_field="win_rate_unavailable_reason",
        note="Won ÷ (won + lost) among leads created in the period, whenever they were "
        "closed; leads closed in this period but created earlier are not included.",
    ),
    "leads_created_over_time": AnalyticsOperationSpec(
        "Sales leads created over time",
        "Number of sales leads created per day, week or month.",
        metrics.get_enquiry_metrics,
        ("over_time",),
        "time_series",
        basis="created",
        uses_grain=True,
    ),
    "pipeline_value_by_stage": AnalyticsOperationSpec(
        "Pipeline by customer sales stage",
        "Number and estimated value of sales leads created in the period, grouped by "
        "their customer's sales stage.",
        metrics.get_pipeline_metrics,
        ("by_sales_stage",),
        "distribution",
        basis="leads created",
    ),
    "lead_to_meeting_conversion": AnalyticsOperationSpec(
        "Sales lead to meeting conversion",
        "Percent of sales leads created in the period that have a linked meeting "
        "scheduled in the period.",
        metrics.get_pipeline_metrics,
        ("enquiry_to_meeting_conversion_percent",),
        "percent",
        basis="leads created",
        reason_field="enquiry_to_meeting_conversion_unavailable_reason",
        note="Only meetings scheduled in the same period count.",
    ),
    "activity_over_time": AnalyticsOperationSpec(
        "Activity over time",
        "Meetings (by scheduled time), calls (by actual, else scheduled, else created "
        "time) and follow-ups (by due date) per day, week or month.",
        metrics.get_activity_metrics,
        ("over_time",),
        "activity_series",
        basis="meetings by scheduled time, calls by call time and follow-ups by due date",
        uses_grain=True,
    ),
    "meetings_by_status": AnalyticsOperationSpec(
        "Meetings by status",
        "Meetings scheduled in the period, grouped by status.",
        metrics.get_activity_metrics,
        ("meetings_by_status",),
        "distribution",
        basis="scheduled",
    ),
    "calls_by_status": AnalyticsOperationSpec(
        "Calls by status",
        "Calls in the period (by actual, else scheduled, else created time), grouped by "
        "status.",
        metrics.get_activity_metrics,
        ("calls_by_status",),
        "distribution",
        basis="by call time",
    ),
    "followup_summary": AnalyticsOperationSpec(
        "Follow-up status",
        "Follow-ups due in the period: overdue, due today, due in the next 7 days, "
        "pending, completed and cancelled.",
        metrics.get_followup_metrics,
        ("overdue", "due_today", "due_next_7_days", "pending", "completed", "cancelled"),
        "distribution",
        basis="due",
        note="Overdue, due today, due in the next 7 days and pending are measured against "
        "the current UTC date.",
        current_fields=("due_today", "due_next_7_days", "pending"),
        past_note="This period has ended: overdue means follow-ups due in it that are still "
        "open, and counts measured against today are omitted.",
    ),
}


def _check_registry() -> None:
    # Runs at import, so the schema enum, this registry and the selected fields cannot drift apart.
    if set(OPERATIONS) != set(get_args(AnalyticsOperation)):
        raise RuntimeError("The analytics operation registry does not match AnalyticsOperation.")
    for name, spec in OPERATIONS.items():
        response_fields = get_type_hints(spec.fetch)["return"].model_fields
        missing = [
            field
            for field in (*spec.fields, spec.reason_field)
            if field is not None and field not in response_fields
        ]
        if missing:
            raise RuntimeError(f"Analytics operation {name} selects unknown fields: {missing}")
        if spec.uses_grain and "grain" not in inspect.signature(spec.fetch).parameters:
            raise RuntimeError(f"Analytics operation {name} uses a grain its service does not take.")
        if not set(spec.current_fields) <= set(spec.fields):
            raise RuntimeError(f"Analytics operation {name} has current_fields it does not select.")
        if not spec.all_time and not spec.basis:
            raise RuntimeError(f"Analytics operation {name} needs a basis for its summary.")


_check_registry()


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _system_prompt(today: date) -> str:
    catalog = "\n".join(f"- {name}: {spec.description}" for name, spec in OPERATIONS.items())
    return ANALYTICS_QUERY_SYSTEM_PROMPT.format(today=today.isoformat(), operations=catalog)


def _resolve_period(plan: AnalyticsQueryPlan, today: date) -> AnalyticsQueryPlan:
    """Turn a relative period into inclusive dates; explicit dates pass through unchanged."""
    forms = [
        plan.start_date is not None or plan.end_date is not None,
        plan.month is not None,
        plan.last_week,
        plan.last_n_days is not None,
    ]
    if sum(forms) > 1:
        # Checked here, not in the schema, because the resolved plan legitimately has both.
        logger.error("Analytics plan named more than one period form [operation=%s]", plan.operation)
        raise APIError(
            "The AI assistant returned an unusable response.",
            status_code=502,
            code="malformed_ai_output",
        )
    if plan.month is not None:
        # The most recent occurrence that has started: this year unless it is still ahead.
        year = today.year if plan.month <= today.month else today.year - 1
        start = date(year, plan.month, 1)
        end = date(year, plan.month, calendar.monthrange(year, plan.month)[1])
    elif plan.last_week:
        this_monday = today - timedelta(days=today.weekday())
        start, end = this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    elif plan.last_n_days is not None:
        start, end = today - timedelta(days=plan.last_n_days - 1), today
    else:
        return plan
    return plan.model_copy(update={"start_date": start, "end_date": end})


def _execute(
    db: Session,
    spec: AnalyticsOperationSpec,
    plan: AnalyticsQueryPlan,
    organization_id: int,
    today: date,
) -> AnalyticsAskResult:
    grain = {"grain": plan.grain} if spec.uses_grain else {}
    response = spec.fetch(
        db, plan.start_date, plan.end_date, organization_id=organization_id, **grain
    )
    period_ended = plan.end_date is not None and plan.end_date < today
    fields = tuple(
        field for field in spec.fields if not (period_ended and field in spec.current_fields)
    )
    values = [getattr(response, field) for field in fields]
    value = None
    if len(spec.fields) > 1:
        items = [DistributionMetric(label=field, count=v) for field, v in zip(fields, values)]
    elif spec.kind in ("number", "percent", "amount"):
        value, items = values[0], None
    else:
        items = values[0]
    return AnalyticsAskResult(
        kind=spec.kind,
        value=value,
        items=items,
        unavailable_reason=getattr(response, spec.reason_field) if spec.reason_field else None,
        all_time=spec.all_time,
        note=spec.past_note if period_ended and spec.past_note else spec.note,
    )


def _heading(spec: AnalyticsOperationSpec, plan: AnalyticsQueryPlan) -> str:
    if spec.all_time:
        return spec.label
    if plan.start_date and plan.end_date:
        period = f"from {plan.start_date} to {plan.end_date}"
    elif plan.start_date:
        period = f"since {plan.start_date}"
    elif plan.end_date:
        period = f"up to {plan.end_date}"
    else:
        return f"{spec.label} (all records)"
    return f"{spec.label} ({spec.basis} {period})"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _item_text(item: DistributionMetric) -> str:
    text = f"{item.label} {item.count}"
    return text if item.value is None else f"{text} (estimated value {item.value})"


def _summary(
    spec: AnalyticsOperationSpec, plan: AnalyticsQueryPlan, result: AnalyticsAskResult
) -> str:
    items = result.items or []
    # Time series list only periods that have records, so say so rather than imply coverage.
    with_data = f"across {_count(len(items), GRAIN_UNITS.get(plan.grain or '', 'period'))} with data"
    if result.kind == "percent":
        body = (
            f"{result.value}%"
            if result.value is not None
            else f"unavailable. {result.unavailable_reason}"
        )
    elif result.value is not None:
        body = str(result.value)
    elif not items:
        body = "no matching records."
    elif result.kind == "distribution":
        shown = [
            _item_text(item)
            for item in items[:MAX_SUMMARY_ITEMS]
            if isinstance(item, DistributionMetric)
        ]
        extra = len(items) - len(shown)
        body = ", ".join(shown) + (f", and {extra} more" if extra else "")
    elif result.kind == "time_series":
        total = sum(item.count for item in items if isinstance(item, CountTimePoint))
        body = f"{total} in total {with_data}."
    else:
        points = [item for item in items if isinstance(item, ActivityTimePoint)]
        body = (
            f"{_count(sum(p.meetings for p in points), 'meeting')}, "
            f"{_count(sum(p.calls for p in points), 'call')} and "
            f"{_count(sum(p.followups for p in points), 'follow-up')} {with_data}."
        )
    text = f"{_heading(spec, plan)}: {body}"
    text = text if text.endswith(".") else f"{text}."
    return f"{text} {result.note}" if result.note else text


async def ask(
    db: Session,
    question: str,
    *,
    organization_id: int,
    model: BaseChatModel | None = None,
) -> AnalyticsAskResponse:
    # organization_id is already a plain int; closing returns the pooled connection so no
    # transaction stays open during inference. The session reconnects for _execute.
    await run_in_threadpool(db.close)
    today = _utc_today()  # One reference date for the prompt, relative periods and summaries.
    started = time.perf_counter()
    chat_model = model or await run_in_threadpool(create_chat_model)
    try:
        # wait_for cancels the provider coroutine and waits for that cancellation to finish,
        # which closes its HTTP connection before the 504 is returned.
        plan = await asyncio.wait_for(
            ainvoke_structured_output(
                chat_model,
                AnalyticsQueryPlan,
                system_prompt=_system_prompt(today),
                user_content=f"Question: {question}",
            ),
            timeout=settings.analytics_ask_timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "Analytics question timed out [timeout_s=%s]", settings.analytics_ask_timeout_seconds
        )
        raise APIError(
            "The AI assistant took too long to interpret this question. Please try again.",
            status_code=504,
            code="llm_timeout",
        ) from None
    if not isinstance(plan, AnalyticsQueryPlan):
        plan = AnalyticsQueryPlan.model_validate(plan)
    interpreted = time.perf_counter()

    result: AnalyticsAskResult | None = None
    if plan.operation == "unsupported":
        summary = UNSUPPORTED_SUMMARY
    else:
        # The interpretation returned below is this resolved plan: the dates actually executed.
        plan = _resolve_period(plan, today)
        if error := date_range_error(plan.start_date, plan.end_date):
            raise APIError(
                f"The question asks for an invalid date range: {error}",
                status_code=422,
                code="invalid_date_range",
                details={"interpretation": plan.model_dump(mode="json")},
            )
        spec = OPERATIONS[plan.operation]
        plan = plan.model_copy(
            update={"grain": (plan.grain or "monthly") if spec.uses_grain else None}
        )
        result = await run_in_threadpool(_execute, db, spec, plan, organization_id, today)
        summary = _summary(spec, plan, result)
    finished = time.perf_counter()

    timings = AnalyticsAskTimings(
        interpret=round((interpreted - started) * 1000),
        execute=round((finished - interpreted) * 1000),
    )
    # The question itself is not logged; it may contain customer details.
    logger.info(
        "Analytics question answered [operation=%s interpret_ms=%d execute_ms=%d]",
        plan.operation,
        timings.interpret,
        timings.execute,
    )
    return AnalyticsAskResponse(
        status="unsupported" if result is None else "answered",
        question=question,
        interpretation=plan,
        result=result,
        summary=summary,
        timings_ms=timings,
    )
