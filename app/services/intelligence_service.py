"""Rules-based attention queue for open sales enquiries.

Every item carries explicit reason codes and the facts behind them. Nothing here is a
score or a probability: ordering is a documented sequence of rules (ATTENTION_ORDERING).
"""

from collections.abc import Iterable
from functools import cache
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Call, Company, Customer, FollowUp, Meeting, SalesEnquiry
from app.schemas.agent import AgentActionName
from app.schemas.intelligence import (
    AttentionItem,
    AttentionQueue,
    AttentionReason,
    AttentionReasonCode,
    SuggestedAction,
)
from app.services.analytics_service import (
    OPEN_ENQUIRY_STATUSES,
    OPEN_FOLLOWUP_STATUSES,
    _database_errors,
)
from app.services.hitl_action_registry import validate_operation

DEFAULT_STALE_DAYS = 21
DEFAULT_LIMIT = 50
# New enquiries rarely have a next step booked yet; no_next_step waits this long.
DEFAULT_NEW_ENQUIRY_GRACE_DAYS = 3
HIGH_PRIORITIES = ("high", "urgent")
PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "normal": 2, "low": 3}
# Past activity that shows the deal is being worked. Scheduled, cancelled, no-show
# and failed records are not evidence of contact.
COUNTED_CALL_STATUSES = ("completed", "attempted", "missed")
ATTENTION_ORDERING = [
    "Enquiries with an overdue follow-up first.",
    "Then enquiries with no counted activity within the inactivity threshold.",
    "Then by priority: urgent, high, medium/normal, low.",
    "Then the longest time since activity first.",
    "Ties are broken by enquiry ID, oldest first.",
]


# Structured-task suggestions: action, label, and the task field the browser fills with
# the user's timezone. Only enquiry-scoped IDs are ever prefilled.
TASK_SUGGESTIONS: dict[str, tuple[AgentActionName, str, str]] = {
    "no_recent_activity": ("schedule_call", "Schedule a call", "scheduled_timezone"),
    "no_next_step": ("create_followup", "Plan a follow-up", "due_timezone"),
}


@cache
def _user_must_provide(action: AgentActionName, timezone_field: str) -> tuple[str, ...]:
    # The registry decides what is still required, so suggestions cannot drift from it.
    _, missing = validate_operation(action, {"customer_id": 1, "enquiry_id": 1})
    return tuple(field for field in missing if field != timezone_field)


def suggest_actions(
    customer_id: int, enquiry_id: int, codes: list[AttentionReasonCode]
) -> list[SuggestedAction]:
    """At most one workflow suggestion (by reason precedence) plus one data-quality one."""
    suggestions: list[SuggestedAction] = []
    if "overdue_followup" in codes:
        suggestions.append(
            SuggestedAction(
                kind="review_followups",
                category="workflow",
                label="Review overdue follow-ups",
                reason_codes=["overdue_followup"],
                opens_task=False,
                prefill={"customer_id": customer_id},
                user_must_provide=[],
            )
        )
    elif "no_recent_activity" in codes or "no_next_step" in codes:
        trigger = "no_recent_activity" if "no_recent_activity" in codes else "no_next_step"
        action, label, timezone_field = TASK_SUGGESTIONS[trigger]
        suggestions.append(
            SuggestedAction(
                kind=action,
                category="workflow",
                label=label,
                # A scheduled call is also a next step, so it answers both reasons.
                reason_codes=[code for code in ("no_recent_activity", "no_next_step") if code in codes],
                opens_task=True,
                prefill={"customer_id": customer_id, "enquiry_id": enquiry_id},
                timezone_field=timezone_field,
                user_must_provide=list(_user_must_provide(action, timezone_field)),
            )
        )
    if "missing_value" in codes:
        # Review only: apply_enrichment would record a manual value as an AI enrichment.
        suggestions.append(
            SuggestedAction(
                kind="review_value",
                category="data_quality",
                label="Review estimated value",
                reason_codes=["missing_value"],
                opens_task=False,
                prefill={"customer_id": customer_id},
                user_must_provide=[],
            )
        )
    return suggestions


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _earliest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _by_scope(rows: Iterable[Any]) -> dict[tuple[int, int | None], Any]:
    # Keyed by (customer_id, enquiry_id); enquiry_id None means a customer-level activity.
    return {(row.customer_id, row.enquiry_id): row for row in rows}


def overdue_followup(today_start: datetime) -> Any:
    # The overdue rule is the one analytics_service uses, so the counts agree.
    return (FollowUp.status == "overdue") | (
        FollowUp.status.in_(OPEN_FOLLOWUP_STATUSES) & (FollowUp.due_date < today_start)
    )


def activity_facts(
    db: Session,
    *,
    organization_id: int,
    now: datetime,
    today_start: datetime,
    customers: Any,
) -> tuple[dict[tuple[int, int | None], Any], ...]:
    """Per (customer_id, enquiry_id) activity facts for the given customers, in three queries.

    Each row has last_activity (latest counted past activity) and next_step (earliest
    upcoming active one); follow-up rows also carry overdue_count and oldest_overdue.
    """
    meetings = _by_scope(
        db.execute(
            select(
                Meeting.customer_id,
                Meeting.enquiry_id,
                func.max(
                    case(
                        (
                            (Meeting.status == "completed") & (Meeting.scheduled_at <= now),
                            Meeting.scheduled_at,
                        )
                    )
                ).label("last_activity"),
                func.min(
                    case(
                        (
                            (Meeting.status == "scheduled") & (Meeting.scheduled_at > now),
                            Meeting.scheduled_at,
                        )
                    )
                ).label("next_step"),
            )
            .where(
                Meeting.organization_id == organization_id,
                Meeting.customer_id.in_(customers),
            )
            .group_by(Meeting.customer_id, Meeting.enquiry_id)
        )
    )

    call_time = func.coalesce(Call.actual_time, Call.scheduled_at)
    calls = _by_scope(
        db.execute(
            select(
                Call.customer_id,
                Call.enquiry_id,
                func.max(
                    case(
                        (
                            Call.status.in_(COUNTED_CALL_STATUSES) & (call_time <= now),
                            call_time,
                        )
                    )
                ).label("last_activity"),
                func.min(
                    case(
                        (
                            (Call.status == "scheduled") & (Call.scheduled_at > now),
                            Call.scheduled_at,
                        )
                    )
                ).label("next_step"),
            )
            .where(
                Call.organization_id == organization_id,
                Call.customer_id.in_(customers),
            )
            .group_by(Call.customer_id, Call.enquiry_id)
        )
    )

    is_overdue = overdue_followup(today_start)
    followups = _by_scope(
        db.execute(
            select(
                FollowUp.customer_id,
                FollowUp.enquiry_id,
                func.max(
                    case(
                        (
                            (FollowUp.status == "completed") & (FollowUp.completed_at <= now),
                            FollowUp.completed_at,
                        )
                    )
                ).label("last_activity"),
                func.min(
                    case(
                        (
                            FollowUp.status.in_(OPEN_FOLLOWUP_STATUSES)
                            & (FollowUp.due_date >= today_start),
                            FollowUp.due_date,
                        )
                    )
                ).label("next_step"),
                func.count(case((is_overdue, 1))).label("overdue_count"),
                func.min(case((is_overdue, FollowUp.due_date))).label("oldest_overdue"),
            )
            .where(
                FollowUp.organization_id == organization_id,
                FollowUp.customer_id.in_(customers),
            )
            .group_by(FollowUp.customer_id, FollowUp.enquiry_id)
        )
    )

    return meetings, calls, followups


@_database_errors
def get_attention_queue(
    db: Session,
    *,
    now: datetime,
    organization_id: int,
    stale_days: int = DEFAULT_STALE_DAYS,
    limit: int = DEFAULT_LIMIT,
    new_enquiry_grace_days: int = DEFAULT_NEW_ENQUIRY_GRACE_DAYS,
) -> AttentionQueue:
    if stale_days < 1:
        raise ValueError("stale_days must be at least 1")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if new_enquiry_grace_days < 0:
        raise ValueError("new_enquiry_grace_days must be 0 or more")
    now = _as_utc(now)
    # Same "today" as analytics_service: the UTC calendar day of `now`.
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    stale_after = timedelta(days=stale_days)
    grace = timedelta(days=new_enquiry_grace_days)

    open_customers = select(SalesEnquiry.customer_id).where(
        SalesEnquiry.organization_id == organization_id,
        SalesEnquiry.status.in_(OPEN_ENQUIRY_STATUSES),
    )
    enquiries = db.execute(
        select(SalesEnquiry, Customer.customer_name, Company.company_name)
        .join(Customer, SalesEnquiry.customer_id == Customer.customer_id)
        .join(Company, Customer.company_id == Company.company_id)
        .where(
            SalesEnquiry.organization_id == organization_id,
            SalesEnquiry.status.in_(OPEN_ENQUIRY_STATUSES),
        )
    ).all()

    meetings, calls, followups = activity_facts(
        db,
        organization_id=organization_id,
        now=now,
        today_start=today_start,
        customers=open_customers,
    )

    def facts(key: tuple[int, int | None]) -> tuple[datetime | None, datetime | None, int, datetime | None]:
        rows = [row for row in (meetings.get(key), calls.get(key)) if row is not None]
        followup = followups.get(key)
        if followup is not None:
            rows.append(followup)
        return (
            _latest(*(_as_utc(row.last_activity) for row in rows)),
            _earliest(*(_as_utc(row.next_step) for row in rows)),
            int(followup.overdue_count or 0) if followup is not None else 0,
            _as_utc(followup.oldest_overdue) if followup is not None else None,
        )

    items: list[AttentionItem] = []
    for enquiry, customer_name, company_name in enquiries:
        # Reasons use only records linked to this enquiry. Customer-only records (no
        # enquiry) are reported separately and never set or clear an enquiry's reasons.
        last_activity, next_step, overdue_count, oldest_overdue = facts(
            (enquiry.customer_id, enquiry.enquiry_id)
        )
        (
            customer_last_activity,
            customer_next_step,
            customer_overdue_count,
            customer_oldest_overdue,
        ) = facts((enquiry.customer_id, None))

        created_at = _as_utc(enquiry.created_at)
        reference = last_activity or created_at
        since = now - reference
        days_since = max(since.days, 0)

        reasons: list[AttentionReason] = []
        if overdue_count:
            reasons.append(
                AttentionReason(
                    code="overdue_followup",
                    detail=f"{overdue_count} overdue follow-up(s); the oldest was due {oldest_overdue.date().isoformat()}.",
                )
            )
        if since >= stale_after:
            measured = (
                f"last counted activity on {last_activity.date().isoformat()}"
                if last_activity
                else f"no counted activity since the enquiry was created on {created_at.date().isoformat()}"
            )
            reasons.append(
                AttentionReason(
                    code="no_recent_activity",
                    detail=f"{days_since} days without activity ({measured}); threshold {stale_days} days.",
                )
            )
        # 0 disables the grace period entirely.
        if next_step is None and (not new_enquiry_grace_days or now - created_at >= grace):
            reasons.append(
                AttentionReason(
                    code="no_next_step",
                    detail="No upcoming meeting, call or open follow-up is scheduled.",
                )
            )
        # Priority and missing value explain an item; on their own they never add one.
        if not reasons:
            continue
        if enquiry.priority in HIGH_PRIORITIES:
            reasons.append(
                AttentionReason(code="high_priority", detail=f"Priority is {enquiry.priority}.")
            )
        if enquiry.estimated_value is None:
            reasons.append(
                AttentionReason(code="missing_value", detail="No estimated value is recorded.")
            )

        items.append(
            AttentionItem(
                enquiry_id=enquiry.enquiry_id,
                customer_id=enquiry.customer_id,
                customer_name=customer_name,
                company_name=company_name,
                product=enquiry.product,
                status=enquiry.status,
                priority=enquiry.priority,
                estimated_value=enquiry.estimated_value,
                enquiry_created_at=created_at,
                last_activity_at=last_activity,
                days_since_activity=days_since,
                activity_measured_from="last_activity" if last_activity else "enquiry_created",
                next_step_at=next_step,
                overdue_followup_count=overdue_count,
                oldest_overdue_due_at=oldest_overdue,
                customer_level_last_activity_at=customer_last_activity,
                customer_level_next_step_at=customer_next_step,
                customer_level_overdue_followup_count=customer_overdue_count,
                customer_level_oldest_overdue_due_at=customer_oldest_overdue,
                reasons=reasons,
                suggested_actions=suggest_actions(
                    enquiry.customer_id,
                    enquiry.enquiry_id,
                    [reason.code for reason in reasons],
                ),
            )
        )

    def order(item: AttentionItem) -> tuple[int, int, int, float, int]:
        codes = {reason.code for reason in item.reasons}
        return (
            0 if "overdue_followup" in codes else 1,
            0 if "no_recent_activity" in codes else 1,
            PRIORITY_ORDER.get(item.priority, len(PRIORITY_ORDER)),
            -(now - (item.last_activity_at or item.enquiry_created_at)).total_seconds(),
            item.enquiry_id,
        )

    items.sort(key=order)
    return AttentionQueue(
        generated_at=now,
        today_utc=now.date(),
        stale_days=stale_days,
        new_enquiry_grace_days=new_enquiry_grace_days,
        open_enquiries=len(enquiries),
        needing_attention=len(items),
        ordering=ATTENTION_ORDERING,
        items=items[:limit],
    )
