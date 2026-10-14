"""Meeting requests completed across chat turns through the user's own draft task."""

from datetime import datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.tools.time_grounding import ground_meeting_time, stated_meeting_time
from app.models import (
    Company,
    Contact,
    Customer,
    HitlActionProposal,
    HitlTask,
    Meeting,
    Organization,
    SalesEnquiry,
    User,
)
from app.services.agent_action_state import pending_actions
from app.services.meeting_draft import resolve_reference
from tests.conftest import TEST_USER_PASSWORD_HASH, legacy_organization_id
from tests.test_agent_actions import _count, _tool_call
from tests.test_agent_conversations import _model

DUBAI = "Asia/Dubai"


def _chat(
    client: TestClient,
    customer_id: int | None,
    message: str,
    task_id: str | None = None,
    *,
    status: int = 200,
) -> Any:
    body: dict[str, Any] = {"message": message, "timezone_name": DUBAI}
    if customer_id is not None:
        body["customer_id"] = customer_id
    if task_id is not None:
        body["task_id"] = task_id
    response = client.post("/api/agent/chat", json=body)
    assert response.status_code == status, response.text
    return response.json()


def _book(customer_id: int, call_id: str, **args: Any) -> AIMessage:
    return _tool_call("create_meeting", {"customer_id": customer_id, **args}, call_id)


def _user_id(sessions: sessionmaker[Session], email: str = "sales.rep@example.com") -> int:
    with sessions() as db:
        return db.scalar(select(User.user_id).where(User.email == email))


def _draft(sessions: sessionmaker[Session], *, user_id: int, action: str = "create_meeting", **values: Any) -> str:
    with sessions() as db:
        task = pending_actions.create_task(
            db,
            user_id=user_id,
            action_type=action,  # type: ignore[arg-type]
            collected_data={
                "operations": [
                    {"action": action, "values": values, "status": "collecting_information"}
                ]
            },
        )
        return task.task_id


def _open_tasks(sessions: sessionmaker[Session]) -> int:
    with sessions() as db:
        return db.query(HitlTask).filter(HitlTask.status == "collecting_information").count()


def _utc(value: str | datetime) -> datetime:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Schedule a meeting for tomorrow 550 pm", time(17, 50)),
        ("tomorrow 5.50 pm", time(17, 50)),
        ("1130am works", time(11, 30)),
        ("5:50 PM", time(17, 50)),
        ("550", None),
        ("in 2026", None),
        ("1500 units", None),
        ("2026 pm", None),
    ],
)
def test_compact_times_need_am_or_pm(message: str, expected: time | None) -> None:
    assert stated_meeting_time(message) == (expected, None)
    if expected is not None:
        assert ground_meeting_time(message, expected) == (expected, None)


def test_time_only_reply_completes_the_same_draft_with_correct_utc(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    meetings = _count(agent_sessions, Meeting)
    _model(monkeypatch, _book(customer_id, "m1", scheduled_date="2026-10-15"))
    first = _chat(agent_client, customer_id, "Schedule a meeting on 15 October")
    assert first["response"] == "What time would you like to schedule the meeting?"
    task_id = first["clarification_task"]["task_id"]

    # The model passes only the new detail; the draft supplies the date and timezone.
    _model(monkeypatch, _book(customer_id, "m2", scheduled_time="17:50"))
    second = _chat(agent_client, customer_id, "550 pm", task_id)

    assert second["response"] == (
        "I am ready to create meeting for 2026-10-15 17:50 Asia/Dubai (UTC+04:00). Please confirm."
    )
    payload = second["pending_action"]["payload"]
    assert _utc(payload["scheduled_at"]) == datetime(2026, 10, 15, 13, 50, tzinfo=timezone.utc)
    with agent_sessions() as db:
        assert db.query(HitlTask).count() == 1
        proposal = db.scalar(select(HitlActionProposal))
        assert proposal.task_id == task_id and proposal.status == "pending"
    assert _open_tasks(agent_sessions) == 0
    assert _count(agent_sessions, Meeting) == meetings  # Nothing written before approval.

    # The draft is no longer open, so it cannot produce a second proposal.
    _model(monkeypatch, _book(customer_id, "m3", scheduled_time="17:50"))
    repeated = agent_client.post(
        "/api/agent/chat",
        json={"message": "550 pm", "customer_id": customer_id, "timezone_name": DUBAI, "task_id": task_id},
    )
    assert repeated.status_code == 409
    assert _count(agent_sessions, HitlActionProposal) == 1

    confirmed = agent_client.post(f"/api/agent/actions/{second['pending_action']['action_id']}/confirm")
    assert confirmed.json()["status"] == "completed", confirmed.text
    with agent_sessions() as db:
        meeting = db.get(Meeting, confirmed.json()["record_id"])
        assert _utc(meeting.scheduled_at) == datetime(2026, 10, 15, 13, 50, tzinfo=timezone.utc)


def test_backend_fills_the_draft_when_the_model_makes_no_tool_call(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "m1", scheduled_date="2026-10-15"))
    task_id = _chat(agent_client, customer_id, "Book a meeting on 15 October")["clarification_task"]["task_id"]

    _model(monkeypatch, AIMessage(content="Which contact, enquiry, duration and agenda?"))
    body = _chat(agent_client, customer_id, "5:50 PM.", task_id)

    assert body["response"].startswith("I am ready to create meeting for 2026-10-15 17:50 Asia/Dubai")
    assert _utc(body["pending_action"]["payload"]["scheduled_at"]) == datetime(
        2026, 10, 15, 13, 50, tzinfo=timezone.utc
    )
    assert _count(agent_sessions, HitlTask) == 1
    assert _count(agent_sessions, Meeting) == 1  # Only the fixture's meeting.


def test_unrelated_reply_keeps_the_draft_open_without_using_history_times(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "m1", scheduled_date="2026-10-15"))
    task_id = _chat(agent_client, customer_id, "Book a meeting on 15 October")["clarification_task"]["task_id"]

    # The model copies a time the user never stated in this message; it is not used.
    _model(monkeypatch, _book(customer_id, "m2", scheduled_time="17:50"))
    body = _chat(agent_client, customer_id, "Thanks, what was that customer's stage again?", task_id)

    assert body["response"] == "What time would you like to schedule the meeting?"
    assert body["clarification_task"]["task_id"] == task_id
    assert body["pending_action"] is None
    assert _count(agent_sessions, HitlTask) == 1
    assert _count(agent_sessions, HitlActionProposal) == 0


def test_contact_agenda_and_duration_across_turns_keep_date_and_time(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(
        monkeypatch,
        _book(customer_id, "m1", scheduled_date="2026-10-15", contact_name="alex smith"),
    )
    first = _chat(agent_client, customer_id, "Meeting on 15 October with Alex Smith")
    task_id = first["clarification_task"]["task_id"]
    assert first["clarification_task"]["operations"][0]["values"]["contact_id"] == agent_records["contact_id"]

    _model(monkeypatch, _book(customer_id, "m2", agenda="External penetration test"))
    second = _chat(agent_client, customer_id, "Agenda: external penetration test", task_id)
    assert second["response"] == "What time would you like to schedule the meeting?"
    values = second["clarification_task"]["operations"][0]["values"]
    assert values["scheduled_date"] == "2026-10-15"
    assert values["contact_id"] == agent_records["contact_id"]

    _model(monkeypatch, AIMessage(content="Noted."))
    third = _chat(agent_client, customer_id, "45 minutes at 5:50 PM", task_id)

    payload = third["pending_action"]["payload"]
    assert payload["contact_id"] == agent_records["contact_id"]
    assert payload["agenda"] == "External penetration test"
    assert payload["duration"] == 45
    assert _utc(payload["scheduled_at"]) == datetime(2026, 10, 15, 13, 50, tzinfo=timezone.utc)
    assert _count(agent_sessions, HitlTask) == 1
    assert _open_tasks(agent_sessions) == 0


@pytest.fixture
def named_records(agent_sessions: sessionmaker[Session], agent_records: dict[str, int]) -> dict[str, int]:
    with agent_sessions() as db:
        customer = db.get(Customer, agent_records["customer_id"])
        other = Customer(company=customer.company, customer_name="Other Customer", status="active")
        first = Contact(customer=customer, name="Rohan Kapoor", job_title="CTO")
        second = Contact(customer=customer, name="Rohan  kapoor", job_title="Engineer")
        foreign = Contact(customer=other, name="Priya Nair")
        upgrade = SalesEnquiry(
            customer=customer,
            product="Network Infrastructure Upgrade",
            enquiry_text="Core switch refresh",
            priority="normal",
            status="open",
        )
        audit = SalesEnquiry(
            customer=customer,
            product="Security audit",
            enquiry_text="Network penetration testing",
            priority="normal",
            status="open",
        )
        db.add_all([other, first, second, foreign, upgrade, audit])
        db.commit()
        return {
            "rohan_cto": first.contact_id,
            "rohan_engineer": second.contact_id,
            "upgrade": upgrade.enquiry_id,
            "audit": audit.enquiry_id,
            "other_customer": other.customer_id,
        }


def test_reference_matching_is_unique_ambiguous_or_missing_within_the_customer(
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    named_records: dict[str, int],
) -> None:
    customer_id = agent_records["customer_id"]
    with agent_sessions() as db:
        organization_id = legacy_organization_id(db)

        def resolve(kind: str, phrase: str, customer: int = customer_id) -> tuple[int | None, str | None]:
            return resolve_reference(db, kind, phrase, organization_id=organization_id, customer_id=customer)  # type: ignore[arg-type]

        assert resolve("contact", "ALEX smith") == (agent_records["contact_id"], None)
        assert resolve("enquiry", "network infrastructure upgrade") == (named_records["upgrade"], None)
        assert resolve("enquiry", "CRM subscription") == (agent_records["enquiry_id"], None)

        contact_id, question = resolve("contact", "Rohan Kapoor")
        assert contact_id is None
        assert f"#{named_records['rohan_cto']} Rohan Kapoor (CTO)" in question
        assert f"#{named_records['rohan_engineer']}" in question

        enquiry_id, question = resolve("enquiry", "network")
        assert enquiry_id is None and "More than one sales enquiry" in question

        # Another customer's contact is never matched, even by its exact name.
        contact_id, question = resolve("contact", "Priya Nair")
        assert contact_id is None and question.startswith("I could not find a contact")
        # Another organization sees nothing for this customer.
        assert resolve_reference(
            db, "contact", "Alex Smith", organization_id=organization_id + 999, customer_id=customer_id
        )[0] is None


def test_ambiguous_contact_asks_for_a_choice_instead_of_proposing(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    named_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(
        monkeypatch,
        _book(
            customer_id,
            "m1",
            scheduled_date="2026-10-15",
            scheduled_time="17:50",
            contact_name="Rohan Kapoor",
            enquiry_reference="Network Infrastructure Upgrade",
        ),
    )
    body = _chat(agent_client, customer_id, "Meeting 2026-10-15 5:50 PM with Rohan Kapoor about the upgrade")

    assert body["pending_action"] is None
    assert "More than one contact matches" in body["response"]
    with agent_sessions() as db:
        task = db.get(HitlTask, body["clarification_task"]["task_id"])
        values = task.collected_data["operations"][0]["values"]
    assert task.status == "collecting_information"
    assert values["scheduled_time"] == "17:50:00" and values["enquiry_id"] == named_records["upgrade"]
    assert "contact_id" not in values
    assert _count(agent_sessions, HitlActionProposal) == 0

    # Choosing by an ID the read tools returned completes the same draft.
    _model(
        monkeypatch,
        _tool_call("get_customer_profile", {"customer_id": customer_id}, "read"),
        _book(customer_id, "m2", contact_id=named_records["rohan_cto"]),
    )
    chosen = _chat(agent_client, customer_id, "The CTO", body["clarification_task"]["task_id"])
    assert chosen["pending_action"]["payload"]["contact_id"] == named_records["rohan_cto"]
    assert chosen["pending_action"]["payload"]["enquiry_id"] == named_records["upgrade"]
    assert _count(agent_sessions, HitlTask) == 1


def test_unknown_contact_name_is_asked_about_together_with_the_missing_time(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(
        monkeypatch,
        _book(customer_id, "m1", scheduled_date="2026-10-15", contact_name="Nobody Known"),
    )
    body = _chat(agent_client, customer_id, "Meeting on 15 October with Nobody Known")

    assert body["response"] == (
        "What time would you like to schedule the meeting? I could not find a contact matching "
        '"Nobody Known" for this customer. Which contact should I use, or should I leave it out?'
    )
    assert _count(agent_sessions, HitlTask) == 1


@pytest.fixture
def other_organization_user(agent_sessions: sessionmaker[Session]) -> int:
    with agent_sessions() as db:
        organization = Organization(name="Other Org", slug="other-org")
        db.add(organization)
        db.flush()
        user = User(
            email="outsider@example.com",
            full_name="Outsider",
            password_hash=TEST_USER_PASSWORD_HASH,
            role="sales",
            organization_id=organization.organization_id,
        )
        db.add(user)
        db.commit()
        return user.user_id


def test_drafts_that_are_not_this_users_open_meeting_for_this_customer_are_rejected(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    named_records: dict[str, int],
    other_organization_user: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    me = _user_id(agent_sessions)
    seen = _model(monkeypatch, AIMessage(content="unused"))
    values = {"customer_id": customer_id, "scheduled_date": "2026-10-15", "scheduled_timezone": DUBAI}
    with agent_sessions() as db:
        colleague = User(
            email="colleague@example.com",
            full_name="Colleague",
            password_hash=TEST_USER_PASSWORD_HASH,
            role="sales",
            organization_id=legacy_organization_id(db),
        )
        db.add(colleague)
        db.commit()
        colleague_id = colleague.user_id

    expired = _draft(agent_sessions, user_id=me, **values)
    with agent_sessions() as db:
        db.get(HitlTask, expired).expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    cases = [
        (str(uuid4()), customer_id, 404, "task_not_found"),
        (_draft(agent_sessions, user_id=colleague_id, **values), customer_id, 404, "task_not_found"),
        (_draft(agent_sessions, user_id=other_organization_user, **values), customer_id, 404, "task_not_found"),
        (_draft(agent_sessions, user_id=me, **values), named_records["other_customer"], 422, "invalid_meeting_draft"),
        (_draft(agent_sessions, user_id=me, **values), None, 422, "invalid_meeting_draft"),
        (
            _draft(agent_sessions, user_id=me, action="create_followup", customer_id=customer_id, type="call"),
            customer_id,
            422,
            "invalid_meeting_draft",
        ),
        (expired, customer_id, 410, "task_expired"),
    ]
    for task_id, target, status, code in cases:
        body = _chat(agent_client, target, "5:50 PM", task_id, status=status)
        assert body["error"]["code"] == code, (task_id, body)
    assert _chat(agent_client, customer_id, "5:50 PM", "not-a-uuid", status=422)
    assert seen.seen == []  # Rejected before the model ran.
    assert _count(agent_sessions, HitlActionProposal) == 0


def test_early_morning_dubai_time_falls_on_the_previous_utc_date(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    task_id = _draft(
        agent_sessions,
        user_id=_user_id(agent_sessions),
        customer_id=customer_id,
        status="scheduled",
        scheduled_date="2026-10-16",
        scheduled_timezone=DUBAI,
    )
    _model(monkeypatch, AIMessage(content="Okay."))
    body = _chat(agent_client, customer_id, "1:30 am", task_id)

    assert body["response"].startswith("I am ready to create meeting for 2026-10-16 01:30 Asia/Dubai")
    assert _utc(body["pending_action"]["payload"]["scheduled_at"]) == datetime(
        2026, 10, 15, 21, 30, tzinfo=timezone.utc
    )


def test_mentioning_a_product_in_passing_does_not_link_an_enquiry(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "m1", scheduled_date="2026-10-15"))
    task_id = _chat(agent_client, customer_id, "Book a meeting on 15 October")["clarification_task"]["task_id"]

    _model(monkeypatch, AIMessage(content="Okay."))
    body = _chat(agent_client, customer_id, "5:50 PM, they also asked about CRM pricing", task_id)

    assert body["pending_action"]["payload"]["enquiry_id"] is None


def test_ambiguous_contact_blocks_the_proposal_until_chosen_or_declined(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    named_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    full = {"scheduled_date": "2026-10-15", "scheduled_time": "17:50"}
    _model(monkeypatch, _book(customer_id, "m1", contact_name="Rohan Kapoor", **full))
    task_id = _chat(agent_client, customer_id, "Meeting 2026-10-15 5:50 PM with Rohan Kapoor")[
        "clarification_task"
    ]["task_id"]

    # A later call without the contact keeps waiting for the choice and asks again.
    _model(monkeypatch, _book(customer_id, "m2", agenda="Pen test"))
    again = _chat(agent_client, customer_id, "Agenda: pen test", task_id)
    assert again["pending_action"] is None
    assert again["response"].startswith('More than one contact matches "Rohan Kapoor"')
    assert again["clarification_task"]["operations"][0]["missing_fields"] == ["contact_id"]

    # The approvals panel cannot finalize it with unrelated details either.
    panel = agent_client.post(
        f"/api/agent/tasks/{task_id}/inputs", json={"operation_index": 0, "values": {"duration": 30}}
    )
    assert panel.status_code == 200, panel.text
    assert panel.json()["status"] == "collecting_information"
    assert _count(agent_sessions, HitlActionProposal) == 0
    with agent_sessions() as db:
        # Partial panel input is saved even though the task stays open.
        assert db.get(HitlTask, task_id).collected_data["operations"][0]["values"]["duration"] == 30

    # A clear decision to go ahead without a contact completes it.
    _model(monkeypatch, AIMessage(content="Sure."))
    declined = _chat(agent_client, customer_id, "Go ahead without a contact", task_id)
    payload = declined["pending_action"]["payload"]
    assert payload["contact_id"] is None and payload["agenda"] == "Pen test" and payload["duration"] == 30
    assert "contact_name" not in payload


def test_approvals_panel_resolves_an_ambiguous_contact_by_choice_or_by_clearing_it(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    named_records: dict[str, int],
) -> None:
    customer_id = agent_records["customer_id"]
    me = _user_id(agent_sessions)
    values = {
        "customer_id": customer_id,
        "status": "scheduled",
        "scheduled_date": "2026-10-15",
        "scheduled_time": "17:50",
        "scheduled_timezone": DUBAI,
        "contact_name": "Rohan Kapoor",
    }
    for choice, expected in (
        ({"contact_id": named_records["rohan_engineer"]}, named_records["rohan_engineer"]),
        ({"contact_name": None}, None),
    ):
        task_id = _draft(agent_sessions, user_id=me, **values)
        response = agent_client.post(
            f"/api/agent/tasks/{task_id}/inputs", json={"operation_index": 0, "values": choice}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "awaiting_approval"
        with agent_sessions() as db:
            proposal = db.scalar(select(HitlActionProposal).where(HitlActionProposal.task_id == task_id))
            assert proposal.parameters["contact_id"] == expected
            assert "contact_name" not in proposal.parameters
            # Reject it so the next case can propose the same slot.
            proposal.status = "rejected"
            db.commit()


@pytest.fixture
def busy_slot(agent_sessions: sessionmaker[Session], agent_records: dict[str, int]) -> None:
    # 17:50-18:50 Dubai on 15 October is already taken.
    with agent_sessions() as db:
        db.add(
            Meeting(
                customer_id=agent_records["customer_id"],
                organization_id=legacy_organization_id(db),
                scheduled_at=datetime(2026, 10, 15, 13, 50, tzinfo=timezone.utc),
                scheduled_timezone=DUBAI,
                duration=60,
                status="scheduled",
            )
        )
        db.commit()


def _choose(client: TestClient, task_id: str, suggestion: dict[str, Any]) -> Any:
    # The values the chat UI submits for a chosen alternative (scheduleValuesFromSuggestion).
    values = {
        "scheduled_date": suggestion["starts_at"][:10],
        "scheduled_time": suggestion["starts_at"][11:16],
        "scheduled_timezone": suggestion["timezone"],
        "scheduled_time_occurrence": None,
    }
    response = client.post(f"/api/agent/tasks/{task_id}/inputs", json={"operation_index": 0, "values": values})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("model_calls_tool", [True, False])
def test_conflict_while_completing_a_draft_keeps_the_task_and_offers_alternatives(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    busy_slot: None,
    monkeypatch: pytest.MonkeyPatch,
    model_calls_tool: bool,
) -> None:
    customer_id = agent_records["customer_id"]
    meetings = _count(agent_sessions, Meeting)
    _model(monkeypatch, _book(customer_id, "m1", scheduled_date="2026-10-15"))
    task_id = _chat(agent_client, customer_id, "Book a meeting on 15 October")["clarification_task"]["task_id"]

    _model(
        monkeypatch,
        *(
            [_book(customer_id, "m2", scheduled_time="17:50"), AIMessage(content="That time is taken.")]
            if model_calls_tool
            else [AIMessage(content="Okay.")]
        ),
    )
    body = _chat(agent_client, customer_id, "5:50 PM", task_id)

    assert body["pending_action"] is None
    assert body["schedule_reason"] == "conflict"
    assert body["schedule_alternatives"]
    assert body["clarification_task"]["task_id"] == task_id  # Same task, not a new one.
    with agent_sessions() as db:
        task = db.get(HitlTask, task_id)
        assert task.status == "collecting_information"
        assert task.collected_data["operations"][0]["values"]["scheduled_date"] == "2026-10-15"
        assert db.query(HitlTask).count() == 1
    assert _count(agent_sessions, HitlActionProposal) == 0

    suggestion = body["schedule_alternatives"][0]
    chosen = _choose(agent_client, task_id, suggestion)
    assert chosen["status"] == "awaiting_approval"
    with agent_sessions() as db:
        proposal = db.scalar(select(HitlActionProposal))
        assert proposal.task_id == task_id and proposal.status == "pending"
        assert _utc(proposal.parameters["scheduled_at"]) == _utc(suggestion["starts_at"])
    assert _count(agent_sessions, HitlActionProposal) == 1
    assert _count(agent_sessions, Meeting) == meetings  # Still awaiting approval.


@pytest.fixture
def production_sessions(agent_engine: Any) -> sessionmaker[Session]:
    # The app's SessionLocal runs with autoflush off; the default test sessions do not.
    return sessionmaker(bind=agent_engine, autoflush=False, expire_on_commit=False)


@pytest.mark.parametrize("via_draft", [False, True])
def test_task_completes_with_its_proposal_when_autoflush_is_off(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    production_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    via_draft: bool,
) -> None:
    from app.database.connection import get_db
    from app.main import app

    def production_db() -> Any:
        with production_sessions() as db:
            yield db

    # agent_client removes this override on teardown.
    app.dependency_overrides[get_db] = production_db
    customer_id = agent_records["customer_id"]
    if via_draft:
        task_id = _draft(
            agent_sessions,
            user_id=_user_id(agent_sessions),
            customer_id=customer_id,
            status="scheduled",
            scheduled_date="2026-10-15",
            scheduled_timezone=DUBAI,
        )
        _model(monkeypatch, AIMessage(content="Okay."))
        body = _chat(agent_client, customer_id, "5:50 PM", task_id)
    else:
        _model(monkeypatch, _book(customer_id, "m1", scheduled_date="2026-10-15", scheduled_time="17:50"))
        body = _chat(agent_client, customer_id, "Book a meeting on 2026-10-15 at 5:50 PM")
    confirmed = agent_client.post(f"/api/agent/actions/{body['pending_action']['action_id']}/confirm")
    assert confirmed.json()["status"] == "completed", confirmed.text

    with agent_sessions() as db:
        proposal = db.scalar(select(HitlActionProposal))
        task = db.get(HitlTask, proposal.task_id)
        assert (proposal.status, task.status) == ("completed", "completed")


def test_time_written_in_the_message_replaces_a_utc_converted_model_time(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seen live with Qwen: "5:50 PM" in Dubai passed to the tool as 13:50.
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "m1", scheduled_date="2026-10-15", scheduled_time="13:50"))
    body = _chat(agent_client, customer_id, "Schedule a meeting for 2026-10-15 5:50 PM.")

    assert body["response"].startswith("I am ready to create meeting for 2026-10-15 17:50 Asia/Dubai")
    assert _utc(body["pending_action"]["payload"]["scheduled_at"]) == datetime(
        2026, 10, 15, 13, 50, tzinfo=timezone.utc
    )


def test_reference_phrases_with_extra_words_still_match_one_record(
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    named_records: dict[str, int],
) -> None:
    customer_id = agent_records["customer_id"]
    with agent_sessions() as db:
        organization_id = legacy_organization_id(db)

        def resolve(kind: str, phrase: str) -> tuple[int | None, str | None]:
            return resolve_reference(db, kind, phrase, organization_id=organization_id, customer_id=customer_id)  # type: ignore[arg-type]

        # Seen live with Qwen: the agenda appended to the enquiry name.
        assert resolve(
            "enquiry", "Network Infrastructure Upgrade - 45 minute external penetration test"
        ) == (named_records["upgrade"], None)
        assert resolve("contact", "Alex Smith (Buyer)") == (agent_records["contact_id"], None)
        assert resolve("contact", "Rohan Kapoor CTO")[0] is None  # Both Rohans still need a choice.


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Schedule a meeting tomorrow 550 pm", "2026-10-02"),  # Clock frozen at 2026-10-01.
        ("today at 3pm", "2026-10-01"),
        ("on 2026-10-20", "2026-10-20"),
        ("not tomorrow, Tuesday", None),
        ("day after tomorrow", None),
        ("next Monday", None),
        ("tomorrow or 2026-10-20", None),
    ],
)
def test_stated_meeting_date_only_reads_unambiguous_dates(message: str, expected: str | None) -> None:
    from app.services.meeting_draft import stated_meeting_date

    assert stated_meeting_date(message, DUBAI) == expected


def test_tomorrow_in_the_message_replaces_the_models_date_arithmetic(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seen live with Qwen: "tomorrow" sent to the tool as the day after tomorrow.
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "m1", scheduled_date="2026-10-03", scheduled_time="17:50"))
    body = _chat(agent_client, customer_id, "Schedule a meeting tomorrow 550 pm")

    assert body["response"].startswith("I am ready to create meeting for 2026-10-02 17:50 Asia/Dubai")


def test_repeating_a_request_reuses_the_identical_pending_proposal(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seen live with Qwen: "5:50 PM." after the proposal queued a second, identical one.
    customer_id = agent_records["customer_id"]
    booking = {"scheduled_date": "2026-10-15", "scheduled_time": "17:50"}
    _model(monkeypatch, _book(customer_id, "m1", **booking))
    first = _chat(agent_client, customer_id, "Schedule a meeting for 2026-10-15 5:50 PM.")
    _model(monkeypatch, _book(customer_id, "m2", **booking))
    second = _chat(agent_client, customer_id, "5:50 PM.")

    assert second["pending_action"]["action_id"] == first["pending_action"]["action_id"]
    assert _count(agent_sessions, HitlActionProposal) == 1
    confirmed = agent_client.post(f"/api/agent/actions/{first['pending_action']['action_id']}/confirm")
    assert confirmed.json()["status"] == "completed"
    assert _count(agent_sessions, Meeting) == 2  # The fixture's meeting plus exactly one new one.
