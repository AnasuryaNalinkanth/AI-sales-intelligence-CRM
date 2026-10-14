from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Call, Company, Customer, FollowUp, Meeting, SalesEnquiry
from app.services.hitl_action_registry import validate_operation
from app.services.intelligence_service import get_attention_queue, suggest_actions
from tests.conftest import legacy_organization_id

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _customer(db: Session, name: str) -> Customer:
    company = Company(organization_id=legacy_organization_id(db), company_name=f"{name} Ltd")
    db.add(company)
    db.flush()
    customer = Customer(company=company, customer_name=name, created_at=NOW - timedelta(days=90))
    db.add(customer)
    db.flush()
    return customer


def _enquiry(
    db: Session,
    customer: Customer,
    *,
    status: str = "open",
    priority: str = "normal",
    value: Decimal | None = Decimal("1000.00"),
    created_days_ago: int = 60,
    created_at: datetime | None = None,
) -> SalesEnquiry:
    enquiry = SalesEnquiry(
        customer_id=customer.customer_id,
        enquiry_text="Needs a quote.",
        status=status,
        priority=priority,
        estimated_value=value,
        created_at=created_at or NOW - timedelta(days=created_days_ago),
    )
    db.add(enquiry)
    db.flush()
    return enquiry


def _meeting(db: Session, enquiry: SalesEnquiry | None, customer: Customer, at: datetime, status: str) -> None:
    db.add(Meeting(customer_id=customer.customer_id, enquiry_id=enquiry and enquiry.enquiry_id, scheduled_at=at, status=status))


def _call(db: Session, enquiry: SalesEnquiry | None, customer: Customer, at: datetime, status: str) -> None:
    db.add(Call(customer_id=customer.customer_id, enquiry_id=enquiry and enquiry.enquiry_id, scheduled_at=at, status=status))


def _followup(
    db: Session,
    enquiry: SalesEnquiry | None,
    customer: Customer,
    due: datetime,
    status: str = "pending",
    completed_at: datetime | None = None,
) -> None:
    db.add(
        FollowUp(
            customer_id=customer.customer_id,
            enquiry_id=enquiry and enquiry.enquiry_id,
            type="email",
            due_date=due,
            status=status,
            completed_at=completed_at,
            created_at=NOW - timedelta(days=1),
        )
    )


def _codes(queue, enquiry_id: int) -> list[str] | None:
    for item in queue.items:
        if item.enquiry_id == enquiry_id:
            return [reason.code for reason in item.reasons]
    return None


def test_each_reason_is_reported_with_its_facts(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Reasons")
        overdue = _enquiry(db, customer)
        _meeting(db, overdue, customer, NOW - timedelta(days=5), "completed")
        _call(db, overdue, customer, NOW + timedelta(days=2), "scheduled")
        _followup(db, overdue, customer, NOW - timedelta(days=5))
        _followup(db, overdue, customer, NOW - timedelta(days=2), status="overdue")

        stale = _enquiry(db, customer)
        _meeting(db, stale, customer, NOW - timedelta(days=30), "completed")
        _meeting(db, stale, customer, NOW + timedelta(days=3), "scheduled")

        no_next = _enquiry(db, customer)
        _call(db, no_next, customer, NOW - timedelta(days=2), "completed")

        untouched = _enquiry(db, customer, priority="urgent", value=None, created_days_ago=40)

        healthy = _enquiry(db, customer, priority="high", value=None)
        _meeting(db, healthy, customer, NOW - timedelta(days=1), "completed")
        _followup(db, healthy, customer, NOW + timedelta(days=4))
        db.commit()

        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)

    assert _codes(queue, overdue.enquiry_id) == ["overdue_followup"]
    overdue_item = next(item for item in queue.items if item.enquiry_id == overdue.enquiry_id)
    assert overdue_item.overdue_followup_count == 2
    assert overdue_item.oldest_overdue_due_at == NOW - timedelta(days=5)
    assert "oldest was due 2026-10-15" in overdue_item.reasons[0].detail

    assert _codes(queue, stale.enquiry_id) == ["no_recent_activity"]
    stale_item = next(item for item in queue.items if item.enquiry_id == stale.enquiry_id)
    assert stale_item.last_activity_at == NOW - timedelta(days=30)
    assert stale_item.days_since_activity == 30
    assert stale_item.activity_measured_from == "last_activity"
    assert stale_item.next_step_at == NOW + timedelta(days=3)

    assert _codes(queue, no_next.enquiry_id) == ["no_next_step"]

    assert _codes(queue, untouched.enquiry_id) == [
        "no_recent_activity",
        "no_next_step",
        "high_priority",
        "missing_value",
    ]
    untouched_item = next(item for item in queue.items if item.enquiry_id == untouched.enquiry_id)
    assert untouched_item.last_activity_at is None
    assert untouched_item.activity_measured_from == "enquiry_created"
    assert untouched_item.days_since_activity == 40
    assert untouched_item.estimated_value is None

    # High priority and a missing value explain an item but never add one on their own.
    assert _codes(queue, healthy.enquiry_id) is None
    assert queue.open_enquiries == 5
    assert queue.needing_attention == 4


def test_only_open_and_in_progress_enquiries_are_considered(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Statuses")
        included = [_enquiry(db, customer, status=status) for status in ("open", "in_progress")]
        excluded = [_enquiry(db, customer, status=status) for status in ("converted", "closed", "lost")]
        db.commit()
        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)

    returned = {item.enquiry_id for item in queue.items}
    assert returned == {enquiry.enquiry_id for enquiry in included}
    assert not returned & {enquiry.enquiry_id for enquiry in excluded}
    assert queue.open_enquiries == 2


def test_only_completed_contact_counts_as_activity(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Activity")
        not_counted = _enquiry(db, customer)
        recent = NOW - timedelta(days=1)
        _call(db, not_counted, customer, recent, "failed")
        _call(db, not_counted, customer, recent, "cancelled")
        _meeting(db, not_counted, customer, recent, "cancelled")
        _meeting(db, not_counted, customer, recent, "no_show")
        _meeting(db, not_counted, customer, recent, "scheduled")  # past but never completed
        _followup(db, not_counted, customer, NOW + timedelta(days=1))  # created yesterday, still open

        attempted = _enquiry(db, customer)
        _call(db, attempted, customer, recent, "attempted")

        missed = _enquiry(db, customer)
        _call(db, missed, customer, recent, "missed")

        followup_done = _enquiry(db, customer)
        _followup(db, followup_done, customer, NOW - timedelta(days=40), status="completed", completed_at=recent)
        db.commit()
        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)

    item = next(item for item in queue.items if item.enquiry_id == not_counted.enquiry_id)
    assert "no_recent_activity" in [reason.code for reason in item.reasons]
    assert item.last_activity_at is None
    assert item.activity_measured_from == "enquiry_created"
    for counted in (attempted, missed, followup_done):
        assert "no_recent_activity" not in (_codes(queue, counted.enquiry_id) or [])
    followup_item = next(item for item in queue.items if item.enquiry_id == followup_done.enquiry_id)
    assert followup_item.last_activity_at == recent
    # A completed follow-up is never overdue, whatever its due date.
    assert followup_item.overdue_followup_count == 0


def test_future_scheduled_activity_is_a_next_step(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Next steps")
        meeting = _enquiry(db, customer)
        _meeting(db, meeting, customer, NOW + timedelta(days=7), "scheduled")
        call = _enquiry(db, customer)
        _call(db, call, customer, NOW + timedelta(hours=1), "scheduled")
        due_today = _enquiry(db, customer)
        _followup(db, due_today, customer, NOW - timedelta(hours=3))  # earlier today: not yet overdue
        cancelled = _enquiry(db, customer)
        _meeting(db, cancelled, customer, NOW + timedelta(days=7), "cancelled")
        due_yesterday = _enquiry(db, customer)
        _followup(db, due_yesterday, customer, NOW - timedelta(days=1))
        db.commit()
        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)

    for enquiry in (meeting, call, due_today):
        assert "no_next_step" not in _codes(queue, enquiry.enquiry_id)
    assert next(i for i in queue.items if i.enquiry_id == call.enquiry_id).next_step_at == NOW + timedelta(hours=1)
    assert "no_next_step" in _codes(queue, cancelled.enquiry_id)
    yesterday_codes = _codes(queue, due_yesterday.enquiry_id)
    assert "overdue_followup" in yesterday_codes and "no_next_step" in yesterday_codes
    assert "overdue_followup" not in _codes(queue, due_today.enquiry_id)


def _item(queue, enquiry_id: int):
    return next(item for item in queue.items if item.enquiry_id == enquiry_id)


def test_activity_on_one_enquiry_does_not_clear_its_sibling(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Siblings")
        worked = _enquiry(db, customer)
        neglected = _enquiry(db, customer)
        _meeting(db, worked, customer, NOW - timedelta(days=2), "completed")
        _call(db, worked, customer, NOW + timedelta(days=2), "scheduled")
        _followup(db, worked, customer, NOW - timedelta(days=4))
        db.commit()
        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)

    assert _codes(queue, worked.enquiry_id) == ["overdue_followup"]
    assert _codes(queue, neglected.enquiry_id) == ["no_recent_activity", "no_next_step"]
    neglected_item = _item(queue, neglected.enquiry_id)
    assert neglected_item.last_activity_at is None
    assert neglected_item.next_step_at is None
    assert neglected_item.overdue_followup_count == 0
    assert neglected_item.customer_level_last_activity_at is None


def test_customer_only_activity_clears_no_enquiry(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Customer only")
        first = _enquiry(db, customer)
        second = _enquiry(db, customer)
        _meeting(db, None, customer, NOW - timedelta(days=2), "completed")
        _call(db, None, customer, NOW - timedelta(days=1), "completed")
        _followup(db, None, customer, NOW - timedelta(days=40), status="completed", completed_at=NOW - timedelta(hours=5))
        _call(db, None, customer, NOW + timedelta(days=2), "scheduled")
        db.commit()
        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)

    for enquiry in (first, second):
        assert _codes(queue, enquiry.enquiry_id) == ["no_recent_activity", "no_next_step"]
        item = _item(queue, enquiry.enquiry_id)
        assert item.last_activity_at is None
        assert item.activity_measured_from == "enquiry_created"
        assert item.next_step_at is None
        # The customer-level work is still visible, labelled as customer-level.
        assert item.customer_level_last_activity_at == NOW - timedelta(hours=5)
        assert item.customer_level_next_step_at == NOW + timedelta(days=2)


def test_customer_only_overdue_followup_is_not_presented_as_enquiry_specific(
    agent_sessions: sessionmaker[Session],
) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Customer overdue")
        first = _enquiry(db, customer)
        second = _enquiry(db, customer)
        for enquiry in (first, second):
            _meeting(db, enquiry, customer, NOW - timedelta(days=1), "completed")
            _meeting(db, enquiry, customer, NOW + timedelta(days=5), "scheduled")
        _followup(db, None, customer, NOW - timedelta(days=6))
        _followup(db, None, customer, NOW - timedelta(days=3), status="overdue")
        _followup(db, second, customer, NOW - timedelta(days=2))
        db.commit()
        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)

    # Healthy enquiry with only customer-level overdue work: not queued as overdue.
    assert _codes(queue, first.enquiry_id) is None
    second_item = _item(queue, second.enquiry_id)
    assert [reason.code for reason in second_item.reasons] == ["overdue_followup"]
    assert second_item.overdue_followup_count == 1
    assert second_item.oldest_overdue_due_at == NOW - timedelta(days=2)
    assert "1 overdue follow-up(s)" in second_item.reasons[0].detail
    assert second_item.customer_level_overdue_followup_count == 2
    assert second_item.customer_level_oldest_overdue_due_at == NOW - timedelta(days=6)


def test_fixed_clock_threshold_boundary_and_configurable_days(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Clock")
        enquiry = _enquiry(db, customer)
        _meeting(db, enquiry, customer, NOW - timedelta(days=21), "completed")
        _meeting(db, enquiry, customer, NOW + timedelta(days=30), "scheduled")
        db.commit()

        assert _codes(get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW), enquiry.enquiry_id) == ["no_recent_activity"]
        just_before = NOW - timedelta(seconds=1)
        assert _codes(get_attention_queue(db, organization_id=legacy_organization_id(db), now=just_before), enquiry.enquiry_id) is None
        assert _codes(get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW, stale_days=30), enquiry.enquiry_id) is None
        # A naive `now` is read as UTC, the storage convention.
        naive = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW.replace(tzinfo=None))
        assert _codes(naive, enquiry.enquiry_id) == ["no_recent_activity"]
        assert naive.today_utc.isoformat() == "2026-10-20"
        with pytest.raises(ValueError):
            get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW, stale_days=0)
        with pytest.raises(ValueError):
            get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW, limit=0)


def test_ordering_is_rule_based_and_deterministic(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Order")
        upcoming = NOW + timedelta(days=3)
        # Only a missing next step, urgent: still after every stale or overdue item.
        no_next_urgent = _enquiry(db, customer, priority="urgent")
        _meeting(db, no_next_urgent, customer, NOW - timedelta(days=1), "completed")
        stale_normal_older = _enquiry(db, customer, created_days_ago=50)
        _meeting(db, stale_normal_older, customer, upcoming, "scheduled")
        stale_normal_tie_a = _enquiry(db, customer, created_days_ago=30)
        _meeting(db, stale_normal_tie_a, customer, upcoming, "scheduled")
        stale_normal_tie_b = _enquiry(db, customer, created_days_ago=30)
        _meeting(db, stale_normal_tie_b, customer, upcoming, "scheduled")
        stale_urgent = _enquiry(db, customer, priority="urgent", created_days_ago=25)
        _meeting(db, stale_urgent, customer, upcoming, "scheduled")
        overdue_low = _enquiry(db, customer, priority="low")
        _meeting(db, overdue_low, customer, NOW - timedelta(days=1), "completed")
        _followup(db, overdue_low, customer, NOW - timedelta(days=3))
        db.commit()

        first = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)
        second = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)
        limited = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW, limit=2)

    expected = [
        overdue_low.enquiry_id,
        stale_urgent.enquiry_id,
        stale_normal_older.enquiry_id,
        stale_normal_tie_a.enquiry_id,
        stale_normal_tie_b.enquiry_id,
        no_next_urgent.enquiry_id,
    ]
    assert [item.enquiry_id for item in first.items] == expected
    assert first == second
    assert [item.enquiry_id for item in limited.items] == expected[:2]
    assert limited.needing_attention == 6
    assert first.ordering[0].startswith("Sales leads with an overdue follow-up")


def test_query_count_does_not_grow_with_enquiries(
    agent_engine: Engine,
    agent_sessions: sessionmaker[Session],
) -> None:
    def seed(count: int) -> None:
        with agent_sessions() as db:
            for index in range(count):
                customer = _customer(db, f"Load {count}-{index}")
                enquiry = _enquiry(db, customer)
                _meeting(db, enquiry, customer, NOW - timedelta(days=index + 1), "completed")
                _call(db, enquiry, customer, NOW + timedelta(days=index + 1), "scheduled")
                _followup(db, enquiry, customer, NOW - timedelta(days=index + 2))
            db.commit()

    statements: list[str] = []

    def record(*args) -> None:
        statements.append(args[2])

    def run() -> int:
        with agent_sessions() as db:
            organization_id = legacy_organization_id(db)
        statements.clear()
        event.listen(agent_engine, "before_cursor_execute", record)
        try:
            with agent_sessions() as db:
                get_attention_queue(db, organization_id=organization_id, now=NOW)
        finally:
            event.remove(agent_engine, "before_cursor_execute", record)
        return len(statements)

    seed(1)
    small = run()
    seed(30)
    large = run()
    assert small == large == 4


def test_attention_endpoint_is_authenticated_read_only_and_validated(
    agent_sessions: sessionmaker[Session],
    agent_client: TestClient,
) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Endpoint")
        _enquiry(db, customer, created_days_ago=400)
        db.commit()

    response = agent_client.get("/api/analytics/attention?stale_days=21&limit=10")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stale_days"] == 21
    assert body["needing_attention"] == 1
    assert [reason["code"] for reason in body["items"][0]["reasons"]] == ["no_recent_activity", "no_next_step"]
    assert not {"score", "probability"} & (set(body) | set(body["items"][0]))
    assert [action["kind"] for action in body["items"][0]["suggested_actions"]] == ["schedule_call"]

    assert body["new_enquiry_grace_days"] == 3
    for query in ("new_enquiry_grace_days=0", "new_enquiry_grace_days=30"):
        assert agent_client.get(f"/api/analytics/attention?{query}").status_code == 200, query
    for query in (
        "stale_days=0",
        "stale_days=366",
        "limit=0",
        "limit=201",
        "stale_days=abc",
        "new_enquiry_grace_days=-1",
        "new_enquiry_grace_days=31",
    ):
        assert agent_client.get(f"/api/analytics/attention?{query}").status_code == 422, query
    for method in ("post", "put", "patch", "delete"):
        assert getattr(agent_client, method)("/api/analytics/attention").status_code == 405

    del agent_client.headers["Authorization"]
    assert agent_client.get("/api/analytics/attention").status_code == 401


def test_new_enquiry_grace_period_delays_only_no_next_step(agent_sessions: sessionmaker[Session]) -> None:
    grace = timedelta(days=3)
    with agent_sessions() as db:
        customer = _customer(db, "Grace")
        inside = _enquiry(db, customer, created_at=NOW - grace + timedelta(seconds=1))
        exactly = _enquiry(db, customer, created_at=NOW - grace)
        beyond = _enquiry(db, customer, created_at=NOW - grace - timedelta(seconds=1))
        booked = _enquiry(db, customer, created_days_ago=5)
        _call(db, booked, customer, NOW + timedelta(days=1), "scheduled")
        db.commit()

        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)
        disabled = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW, new_enquiry_grace_days=0)
        longer = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW, new_enquiry_grace_days=7)
        with pytest.raises(ValueError):
            get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW, new_enquiry_grace_days=-1)

    assert queue.new_enquiry_grace_days == 3
    assert _codes(queue, inside.enquiry_id) is None
    assert _codes(queue, exactly.enquiry_id) == ["no_next_step"]
    assert _codes(queue, beyond.enquiry_id) == ["no_next_step"]
    assert _codes(queue, booked.enquiry_id) is None
    # 0 disables the grace period; a longer one holds back older enquiries too.
    assert _codes(disabled, inside.enquiry_id) == ["no_next_step"]
    assert disabled.new_enquiry_grace_days == 0
    assert _codes(longer, beyond.enquiry_id) is None
    assert _codes(longer, booked.enquiry_id) is None


def test_grace_period_never_hides_overdue_or_inactivity(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Grace overdue")
        overdue = _enquiry(db, customer, created_days_ago=1)
        _followup(db, overdue, customer, NOW - timedelta(hours=23))  # due yesterday (UTC)
        quiet = _enquiry(db, customer, created_days_ago=2)
        db.commit()
        defaults = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)
        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW, stale_days=1)

    assert _codes(defaults, overdue.enquiry_id) == ["overdue_followup"]
    assert _codes(defaults, quiet.enquiry_id) is None
    assert _codes(queue, overdue.enquiry_id) == ["overdue_followup", "no_recent_activity"]
    assert _codes(queue, quiet.enquiry_id) == ["no_recent_activity"]


def _kinds(codes: list[str]) -> list[tuple[str, list[str]]]:
    return [(action.kind, action.reason_codes) for action in suggest_actions(4, 9, codes)]


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (["no_recent_activity"], [("schedule_call", ["no_recent_activity"])]),
        (["no_next_step"], [("create_followup", ["no_next_step"])]),
        (["overdue_followup"], [("review_followups", ["overdue_followup"])]),
        (["missing_value"], [("review_value", ["missing_value"])]),
        (["high_priority"], []),
        # A scheduled call is also a next step, so one suggestion answers both reasons.
        (["no_recent_activity", "no_next_step"], [("schedule_call", ["no_recent_activity", "no_next_step"])]),
        # Overdue work comes first and no new task is suggested on top of it.
        (["overdue_followup", "no_next_step"], [("review_followups", ["overdue_followup"])]),
        (
            ["overdue_followup", "no_recent_activity", "no_next_step", "high_priority", "missing_value"],
            [("review_followups", ["overdue_followup"]), ("review_value", ["missing_value"])],
        ),
        (
            ["no_next_step", "high_priority", "missing_value"],
            [("create_followup", ["no_next_step"]), ("review_value", ["missing_value"])],
        ),
    ],
)
def test_suggestions_follow_reason_precedence_and_cap(codes: list[str], expected) -> None:
    assert _kinds(codes) == expected
    actions = suggest_actions(4, 9, codes)
    assert len(actions) <= 2
    assert [a.category for a in actions].count("workflow") <= 1
    assert [a.category for a in actions].count("data_quality") <= 1
    for action in actions:
        assert set(action.reason_codes) <= set(codes)
    assert suggest_actions(4, 9, codes) == actions


def test_task_suggestion_prefills_match_the_action_registry() -> None:
    user_values = {
        "schedule_call": {"scheduled_at": "2026-10-25T10:00:00+04:00"},
        "create_followup": {"type": "email", "due_date": "2026-10-25T10:00:00"},
    }
    seen = set()
    for codes in (["no_recent_activity"], ["no_next_step"]):
        (action,) = suggest_actions(4, 9, codes)
        assert action.opens_task
        assert action.prefill == {"customer_id": 4, "enquiry_id": 9}
        prefilled = {**action.prefill, action.timezone_field: "Asia/Dubai"}
        payload, missing = validate_operation(action.kind, prefilled)
        assert payload is None and sorted(missing) == sorted(action.user_must_provide)
        payload, missing = validate_operation(action.kind, {**prefilled, **user_values[action.kind]})
        assert missing == [] and payload["enquiry_id"] == 9
        seen.add(action.kind)
    assert seen == {"schedule_call", "create_followup"}
    for codes in (["overdue_followup"], ["missing_value"]):
        (action,) = suggest_actions(4, 9, codes)
        assert not action.opens_task and action.prefill == {"customer_id": 4}
        assert action.timezone_field is None and action.user_must_provide == []


def test_queue_suggestions_use_only_the_enquirys_own_facts(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Suggestions")
        quiet = _enquiry(db, customer, value=None)
        busy = _enquiry(db, customer)
        _meeting(db, busy, customer, NOW - timedelta(days=1), "completed")
        _followup(db, busy, customer, NOW - timedelta(days=3))
        # Customer-level overdue work and a customer-level next step: context only.
        _followup(db, None, customer, NOW - timedelta(days=9))
        _call(db, None, customer, NOW + timedelta(days=1), "scheduled")
        db.commit()
        queue = get_attention_queue(db, organization_id=legacy_organization_id(db), now=NOW)

    quiet_item = next(item for item in queue.items if item.enquiry_id == quiet.enquiry_id)
    assert quiet_item.customer_level_overdue_followup_count == 1
    assert [(a.kind, a.reason_codes) for a in quiet_item.suggested_actions] == [
        ("schedule_call", ["no_recent_activity", "no_next_step"]),
        ("review_value", ["missing_value"]),
    ]
    assert quiet_item.suggested_actions[0].prefill == {
        "customer_id": customer.customer_id,
        "enquiry_id": quiet.enquiry_id,
    }
    busy_item = next(item for item in queue.items if item.enquiry_id == busy.enquiry_id)
    assert [(a.kind, a.reason_codes) for a in busy_item.suggested_actions] == [
        ("review_followups", ["overdue_followup"]),
    ]
    for item in queue.items:
        for action in item.suggested_actions:
            assert action.prefill.get("enquiry_id", item.enquiry_id) == item.enquiry_id
