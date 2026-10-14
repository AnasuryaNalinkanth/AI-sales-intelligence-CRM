"""Proactive sales intelligence: deterministic, explainable recommendations.

Each recommendation covers one subject: an open enquiry, or a customer for issues that
belong to no open enquiry. The same issue is therefore never listed twice; every
signal about a subject is combined into its evidence. Detection and scoring are the
rules in RULES below; no model is involved. Every query is scoped to one organization,
and every ID and link in a result comes from those scoped queries.

Activity uses the attention queue's definitions (intelligence_service): counted past
activity is a completed meeting, a completed/attempted/missed call or a completed
follow-up; a next step is an upcoming scheduled meeting or call, or an open follow-up
due today or later.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Customer, FollowUp, Meeting, SalesEnquiry
from app.schemas.intelligence import (
    AttentionReasonCode,
    InsightCategory,
    InsightEvidence,
    InsightLevel,
    InsightLink,
    ProactiveInsight,
    ProactiveInsights,
)
from app.services.analytics_service import OPEN_ENQUIRY_STATUSES, _database_errors
from app.services.intelligence_service import (
    DEFAULT_NEW_ENQUIRY_GRACE_DAYS,
    DEFAULT_STALE_DAYS,
    _as_utc,
    _earliest,
    _latest,
    activity_facts,
    overdue_followup,
    suggest_actions,
)

# The single place to tune detection and scoring. Returned with every response.
RULES: dict[str, Any] = {
    # Detection thresholds, in days.
    "stale_days": DEFAULT_STALE_DAYS,  # no counted activity for this long = inactive
    "new_enquiry_grace_days": DEFAULT_NEW_ENQUIRY_GRACE_DAYS,  # before "no next step" counts
    "stalled_min_age_days": 30,  # an enquiry younger than this is never "stalled"
    "meeting_prep_days": 2,  # upcoming meetings within this window need preparation
    # Stages: leads are customers in an open stage; deals can stall only past "new".
    "open_sales_stages": ["new", "qualified", "proposal", "negotiation"],
    "stalled_stages": ["qualified", "proposal", "negotiation"],
    # Normalization: a component reaches 1.0 at this many days.
    "overdue_full_days": 14,
    "inactivity_full_days": 60,
    "priority_values": {"urgent": 1.0, "high": 0.75, "medium": 0.4, "normal": 0.4, "low": 0.1},
    "stage_values": {"negotiation": 1.0, "proposal": 0.8, "qualified": 0.6, "new": 0.3},
    # urgency_score = 100 * sum(weight * component); same for impact_score.
    "urgency_weights": {"overdue": 0.5, "inactivity": 0.3, "imminence": 0.2},
    # value = estimated value / largest known open-enquiry value; unknown counts as 0.
    "impact_weights": {"priority": 0.5, "stage": 0.3, "value": 0.2},
    # priority_score = 0.6 * urgency + 0.4 * impact + 5 per extra warning signal (max 15),
    # clamped to 0-100.
    "score_weights": {"urgency": 0.6, "impact": 0.4},
    "extra_signal_bonus": 5,
    "max_signal_bonus": 15,
    # Score -> level: >= high is "high", >= medium is "medium", otherwise "low".
    "level_thresholds": {"high": 60, "medium": 30},
}
CATEGORIES: tuple[InsightCategory, ...] = (
    "overdue_followup",
    "stalled_deal",
    "neglected_lead",
    "needs_attention",
)
HIGH_PRIORITIES = ("high", "urgent")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _level(score: int) -> InsightLevel:
    thresholds = RULES["level_thresholds"]
    if score >= thresholds["high"]:
        return "high"
    return "medium" if score >= thresholds["medium"] else "low"


def _day(value: datetime) -> str:
    return value.date().isoformat()


def _customer_href(customer_id: int, anchor: str | None = None) -> str:
    return f"/customer?customer_id={customer_id}" + (f"#{anchor}" if anchor else "")


class _Subject:
    """Everything known about one enquiry, or one customer's enquiry-less issues."""

    def __init__(self, customer: Customer, company_name: str, enquiry: SalesEnquiry | None):
        self.customer = customer
        self.company_name = company_name
        self.enquiry = enquiry
        self.overdue: list[FollowUp] = []
        self.meetings: list[Meeting] = []
        self.last_activity: datetime | None = None
        self.next_step: datetime | None = None
        self.can_be_neglected = enquiry is not None


def _build(
    subject: _Subject,
    *,
    now: datetime,
    max_value: Decimal | None,
    customer_overdue: int,
) -> ProactiveInsight | None:
    customer, enquiry = subject.customer, subject.enquiry
    customer_id = customer.customer_id
    enquiry_id = enquiry.enquiry_id if enquiry is not None else None
    created_at = _as_utc(enquiry.created_at if enquiry is not None else customer.created_at)
    evidence: list[InsightEvidence] = []
    signals: list[AttentionReasonCode | str] = []

    # --- Overdue follow-ups --------------------------------------------------------
    overdue = sorted(
        subject.overdue, key=lambda item: (_as_utc(item.due_date), item.followup_id)
    )
    overdue_days = max(((now - _as_utc(item.due_date)).days for item in overdue), default=0)
    for item in overdue:
        days = max((now - _as_utc(item.due_date)).days, 0)
        linked = (
            f" (linked to sales lead #{item.enquiry_id}, which is no longer open)"
            if item.enquiry_id is not None and enquiry_id is None
            else ""
        )
        evidence.append(
            InsightEvidence(
                kind="fact",
                detail=f"Follow-up #{item.followup_id} ({item.type}) was due "
                f"{_day(_as_utc(item.due_date))}, {days} day(s) ago, and is still "
                f"{item.status}{linked}.",
                record_type="followup",
                record_id=item.followup_id,
            )
        )
    if overdue:
        signals.append("overdue_followup")
        if customer_overdue > len(overdue):
            evidence.append(
                InsightEvidence(
                    kind="fact",
                    detail=f"The customer has {customer_overdue} overdue follow-ups in total.",
                    record_type="customer",
                    record_id=customer_id,
                )
            )
    overdue_c = _clamp(max(overdue_days, 1) / RULES["overdue_full_days"]) if overdue else 0.0

    # --- Inactivity (only where neglect applies) -----------------------------------
    inactive = False
    inactive_days = 0
    if subject.can_be_neglected and created_at is not None:
        reference = subject.last_activity or created_at
        inactive_days = max((now - reference).days, 0)
        inactive = inactive_days >= RULES["stale_days"]
        if subject.last_activity is not None:
            evidence.append(
                InsightEvidence(
                    kind="fact",
                    detail=f"Last counted activity was on {_day(subject.last_activity)}, "
                    f"{inactive_days} day(s) ago (threshold {RULES['stale_days']} days).",
                )
            )
        else:
            record = "enquiry" if enquiry is not None else "customer"
            evidence.append(
                InsightEvidence(
                    kind="missing_data",
                    detail=f"No completed meeting, counted call or completed follow-up is "
                    f"recorded for this {record}; inactivity is measured from its creation on "
                    f"{_day(created_at)} ({inactive_days} day(s)). This does not mean the lead "
                    "is lost.",
                )
            )
        if inactive:
            signals.append("no_recent_activity")
    inactivity_c = _clamp(inactive_days / RULES["inactivity_full_days"]) if inactive else 0.0

    no_next_step = (
        subject.can_be_neglected
        and subject.next_step is None
        and created_at is not None
        and now - created_at >= timedelta(days=RULES["new_enquiry_grace_days"])
    )
    if no_next_step:
        signals.append("no_next_step")
        evidence.append(
            InsightEvidence(
                kind="fact", detail="No upcoming meeting, call or open follow-up is scheduled."
            )
        )

    # --- Upcoming meetings needing preparation -------------------------------------
    meetings = sorted(
        subject.meetings, key=lambda item: (_as_utc(item.scheduled_at), item.meeting_id)
    )
    imminence_c = 0.0
    if meetings:
        signals.append("upcoming_meeting")
        window = RULES["meeting_prep_days"] * 24
        hours = (_as_utc(meetings[0].scheduled_at) - now).total_seconds() / 3600
        imminence_c = _clamp(1 - hours / window)
        for meeting in meetings:
            evidence.append(
                InsightEvidence(
                    kind="fact",
                    detail=f"Meeting #{meeting.meeting_id} is scheduled for "
                    f"{_as_utc(meeting.scheduled_at).isoformat(timespec='minutes')} "
                    f"({meeting.agenda or 'no agenda recorded'}).",
                    record_type="meeting",
                    record_id=meeting.meeting_id,
                )
            )

    # --- Category: the strongest signal names the recommendation -------------------
    age_days = max((now - created_at).days, 0) if created_at is not None else 0
    stage = customer.sales_stage
    category: InsightCategory
    if overdue:
        category = "overdue_followup"
    elif (
        enquiry is not None
        and inactive
        and no_next_step
        and age_days >= RULES["stalled_min_age_days"]
        and stage in RULES["stalled_stages"]
    ):
        category = "stalled_deal"
        evidence.append(
            InsightEvidence(
                kind="estimate",
                detail=f"Sales lead has been open for {age_days} days since it was created. The CRM "
                "records no stage-change dates, so time in the current stage is unknown; this "
                "is the lead's age, not its time in stage.",
                record_type="enquiry",
                record_id=enquiry_id,
            )
        )
    elif inactive:
        category = "neglected_lead"
    elif meetings or (
        enquiry is not None and no_next_step and enquiry.priority in HIGH_PRIORITIES
    ):
        category = "needs_attention"
    else:
        return None

    # --- Impact ---------------------------------------------------------------------
    priority = enquiry.priority if enquiry is not None else None
    priority_c = RULES["priority_values"].get(priority, 0.0) if priority else 0.0
    stage_c = RULES["stage_values"].get(stage, 0.0)
    value = enquiry.estimated_value if enquiry is not None else None
    value_c = 0.0
    if enquiry is not None:
        evidence.append(
            InsightEvidence(
                kind="fact",
                detail=f"Sales lead priority is {enquiry.priority}; customer sales stage is {stage}.",
                record_type="enquiry",
                record_id=enquiry_id,
            )
        )
        if value is not None and value >= 0 and max_value:
            value_c = _clamp(float(value / max_value))
            evidence.append(
                InsightEvidence(
                    kind="fact",
                    detail=f"Estimated value is {value}.",
                    record_type="enquiry",
                    record_id=enquiry_id,
                )
            )
        else:
            evidence.append(
                InsightEvidence(
                    kind="missing_data",
                    detail="No estimated value is recorded; potential impact excludes deal value.",
                    record_type="enquiry",
                    record_id=enquiry_id,
                )
            )

    # --- Scores -----------------------------------------------------------------------
    uw, iw, sw = RULES["urgency_weights"], RULES["impact_weights"], RULES["score_weights"]
    components = {
        "overdue": overdue_c, "inactivity": inactivity_c, "imminence": imminence_c,
        "priority": priority_c, "stage": stage_c, "value": value_c,
    }
    urgency_score = round(100 * sum(weight * components[name] for name, weight in uw.items()))
    impact_score = round(100 * sum(weight * components[name] for name, weight in iw.items()))
    bonus = min(RULES["extra_signal_bonus"] * max(len(signals) - 1, 0), RULES["max_signal_bonus"])
    weighted = round(sw["urgency"] * urgency_score + sw["impact"] * impact_score)
    priority_score = int(_clamp(weighted + bonus, 0, 100))

    # The reason names the component that added the most to the score.
    explanations = {
        "overdue": lambda: f"{len(overdue)} overdue follow-up(s); the oldest, "
        f"#{overdue[0].followup_id}, is {overdue_days} day(s) overdue.",
        "inactivity": lambda: (
            f"No counted activity for {inactive_days} days."
            if subject.last_activity
            else f"No counted activity recorded since creation, {inactive_days} days ago."
        ),
        "imminence": lambda: f"Meeting #{meetings[0].meeting_id} is within "
        f"{RULES['meeting_prep_days']} days.",
        "priority": lambda: f"Sales lead priority is {priority}.",
        "stage": lambda: f"Customer is in the {stage} stage.",
        "value": lambda: f"Estimated value {value} is large relative to other open sales leads.",
    }
    contributions = {
        name: (sw["urgency"] * uw[name] if name in uw else sw["impact"] * iw[name]) * component
        for name, component in components.items()
    }
    strongest = max(contributions, key=lambda name: contributions[name])
    reason = (
        f"Strongest factor: {explanations[strongest]()}"
        if contributions[strongest] > 0
        else "Flagged by the rules below; no scored factor is significant."
    )

    name = customer.customer_name
    subject_label = f"sales lead #{enquiry_id}" if enquiry is not None else "customer-level records"
    if category == "overdue_followup":
        title = f"{len(overdue)} overdue follow-up(s) for {name}"
        action = (
            f"Complete or reschedule follow-up #{overdue[0].followup_id}, then record the outcome."
        )
    elif category == "stalled_deal":
        title = f"Sales lead #{enquiry_id} for {name} may be stalled"
        action = (
            "Contact the customer to confirm whether the deal is still active and agree a dated "
            "next step."
        )
    elif category == "neglected_lead":
        title = f"No recent activity with {name}"
        action = "Reach out to the customer, record the outcome, and schedule a next step."
    elif meetings:
        title = f"Prepare for the meeting with {name}"
        action = (
            "Review the customer and generate a meeting brief for meeting "
            f"#{meetings[0].meeting_id}."
        )
    else:
        title = f"High-priority sales lead #{enquiry_id} has no next step"
        action = "Plan a dated next step (call, meeting or follow-up) for this sales lead."
    product = enquiry.product if enquiry is not None and enquiry.product else None
    summary = f"{name} ({subject.company_name}), {subject_label}"
    summary += (f": {product}" if product else "") + "."

    def link(label: str, record_type: Any, record_id: int) -> InsightLink:
        anchor = None if record_type == "customer" else f"{record_type}-{record_id}"
        return InsightLink(
            label=f"{label} #{record_id}",
            record_type=record_type,
            record_id=record_id,
            href=_customer_href(customer_id, anchor),
        )

    links = [link("Customer", "customer", customer_id)]
    if enquiry_id is not None:
        links.append(link("Enquiry", "enquiry", enquiry_id))
    links += [link("Follow-up", "followup", item.followup_id) for item in overdue]
    links += [link("Meeting", "meeting", item.meeting_id) for item in meetings]
    codes = [
        code
        for code in ("overdue_followup", "no_recent_activity", "no_next_step")
        if code in signals
    ]

    return ProactiveInsight(
        recommendation_id=(
            f"enquiry-{enquiry_id}" if enquiry is not None else f"customer-{customer_id}"
        ),
        category=category,
        title=title,
        summary=summary,
        reason=reason,
        urgency=_level(urgency_score),
        potential_impact=_level(impact_score),
        priority_score=priority_score,
        urgency_score=urgency_score,
        impact_score=impact_score,
        score_components={key: round(value, 3) for key, value in components.items()},
        evidence=evidence,
        recommended_next_action=action,
        customer_id=customer_id,
        customer_name=name,
        company_name=subject.company_name,
        enquiry_id=enquiry_id,
        followup_id=overdue[0].followup_id if overdue else None,
        meeting_id=meetings[0].meeting_id if meetings else None,
        links=links,
        # Task drafts only (the existing HITL flow); nothing here writes to the CRM.
        suggested_actions=(
            suggest_actions(customer_id, enquiry_id, codes) if enquiry_id and codes else []
        ),
        evaluated_at=now,
    )


def _facts(rows: Iterable[Any]) -> tuple[datetime | None, datetime | None]:
    rows = [row for row in rows if row is not None]
    return (
        _latest(*(_as_utc(row.last_activity) for row in rows)),
        _earliest(*(_as_utc(row.next_step) for row in rows)),
    )


@_database_errors
def get_proactive_insights(
    db: Session,
    *,
    organization_id: int,
    now: datetime,
    category: InsightCategory | None = None,
    urgency: InsightLevel | None = None,
    min_score: int = 0,
    customer_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> ProactiveInsights:
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError("page must be >= 1 and page_size between 1 and 100")
    now = _as_utc(now)
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)

    enquiries = db.execute(
        select(SalesEnquiry, Customer, Company.company_name)
        .join(Customer, SalesEnquiry.customer_id == Customer.customer_id)
        .join(Company, Customer.company_id == Company.company_id)
        .where(
            SalesEnquiry.organization_id == organization_id,
            SalesEnquiry.status.in_(OPEN_ENQUIRY_STATUSES),
        )
    ).all()
    leads = db.execute(
        select(Customer, Company.company_name)
        .join(Company, Customer.company_id == Company.company_id)
        .where(
            Customer.organization_id == organization_id,
            Customer.sales_stage.in_(RULES["open_sales_stages"]),
            Customer.status != "inactive",
        )
    ).all()
    overdue_rows = db.execute(
        select(FollowUp, Customer, Company.company_name)
        .join(Customer, FollowUp.customer_id == Customer.customer_id)
        .join(Company, Customer.company_id == Company.company_id)
        .where(FollowUp.organization_id == organization_id, overdue_followup(today_start))
    ).all()
    meeting_rows = db.execute(
        select(Meeting, Customer, Company.company_name)
        .join(Customer, Meeting.customer_id == Customer.customer_id)
        .join(Company, Customer.company_id == Company.company_id)
        .where(
            Meeting.organization_id == organization_id,
            Meeting.status == "scheduled",
            Meeting.scheduled_at > now,
            Meeting.scheduled_at <= now + timedelta(days=RULES["meeting_prep_days"]),
        )
    ).all()
    meetings, calls, followups = activity_facts(
        db,
        organization_id=organization_id,
        now=now,
        today_start=today_start,
        customers=select(Customer.customer_id).where(Customer.organization_id == organization_id),
    )

    subjects: dict[tuple[int, int | None], _Subject] = {}
    for enquiry, customer, company_name in enquiries:
        key = (customer.customer_id, enquiry.enquiry_id)
        subjects[key] = _Subject(customer, company_name, enquiry)

    def customer_subject(customer: Customer, company_name: str) -> _Subject:
        key = (customer.customer_id, None)
        if key not in subjects:
            subjects[key] = _Subject(customer, company_name, None)
        return subjects[key]

    customers_with_open_enquiries = {key[0] for key in subjects}
    for customer, company_name in leads:
        # A lead with open enquiries is covered by them; otherwise it is judged as a whole.
        if customer.customer_id not in customers_with_open_enquiries:
            customer_subject(customer, company_name).can_be_neglected = True

    customer_overdue: dict[int, int] = defaultdict(int)
    for item, customer, company_name in overdue_rows:
        customer_overdue[customer.customer_id] += 1
        key = (customer.customer_id, item.enquiry_id)
        target = subjects.get(key) or customer_subject(customer, company_name)
        target.overdue.append(item)
    for meeting, customer, company_name in meeting_rows:
        key = (customer.customer_id, meeting.enquiry_id)
        target = subjects.get(key) or customer_subject(customer, company_name)
        target.meetings.append(meeting)

    by_customer: dict[int, list[Any]] = defaultdict(list)
    for source in (meetings, calls, followups):
        for (row_customer_id, _), row in source.items():
            by_customer[row_customer_id].append(row)
    for (subject_customer_id, subject_enquiry_id), subject in subjects.items():
        if subject_enquiry_id is not None:
            key = (subject_customer_id, subject_enquiry_id)
            rows = [source.get(key) for source in (meetings, calls, followups)]
        else:
            rows = by_customer[subject_customer_id]  # The whole customer, all enquiries.
        subject.last_activity, subject.next_step = _facts(rows)

    known_values = [
        enquiry.estimated_value
        for enquiry, _, _ in enquiries
        if enquiry.estimated_value is not None and enquiry.estimated_value > 0
    ]
    max_value = max(known_values) if known_values else None
    built = (
        _build(
            subject,
            now=now,
            max_value=max_value,
            customer_overdue=customer_overdue[subject.customer.customer_id],
        )
        for subject in subjects.values()
    )
    insights = [insight for insight in built if insight is not None]
    insights.sort(
        key=lambda item: (
            -item.priority_score, -item.urgency_score, item.customer_id, item.enquiry_id or 0
        )
    )

    counts = {name: 0 for name in CATEGORIES}
    for insight in insights:
        counts[insight.category] += 1
    selected = [
        insight
        for insight in insights
        if (category is None or insight.category == category)
        and (urgency is None or insight.urgency == urgency)
        and insight.priority_score >= min_score
        and (customer_id is None or insight.customer_id == customer_id)
    ]
    start = (page - 1) * page_size
    return ProactiveInsights(
        generated_at=now,
        today_utc=now.date(),
        rules=RULES,
        counts_by_category=counts,
        total=len(selected),
        page=page,
        page_size=page_size,
        items=selected[start : start + page_size],
    )
