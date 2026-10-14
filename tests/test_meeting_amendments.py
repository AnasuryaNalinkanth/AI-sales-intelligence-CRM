"""Amending a pending meeting proposal in chat before it is approved."""

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import APIError
from app.models import (
    Customer,
    HitlActionAudit,
    HitlActionProposal,
    HitlTask,
    Meeting,
    Organization,
    User,
)
from app.services.agent_action_state import pending_actions
from tests.conftest import TEST_USER_PASSWORD_HASH, legacy_organization_id
from tests.test_agent_actions import _count
from tests.test_agent_conversations import _model
from tests.test_meeting_drafts import DUBAI, _book, _chat, _user_id, _utc

DUBAI_1750 = datetime(2026, 10, 15, 13, 50, tzinfo=timezone.utc)


def _amend(client: TestClient, customer_id: int | None, message: str, action_id: str, *, status: int = 200) -> Any:
    body: dict[str, Any] = {"message": message, "timezone_name": DUBAI, "pending_action_id": action_id}
    if customer_id is not None:
        body["customer_id"] = customer_id
    response = client.post("/api/agent/chat", json=body)
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
def pending(agent_client: TestClient, agent_records: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> str:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "p1", scheduled_date="2026-10-15", scheduled_time="17:50"))
    body = _chat(agent_client, customer_id, "Schedule a meeting for 2026-10-15 5:50 PM.")
    assert body["pending_action"]["payload"]["contact_id"] is None
    return body["pending_action"]["action_id"]


def _proposals(sessions: sessionmaker[Session]) -> list[tuple[str, str]]:
    with sessions() as db:
        return [(row.action_id, row.status) for row in db.query(HitlActionProposal).order_by(HitlActionProposal.created_at)]


def test_adding_contact_enquiry_duration_and_agenda_revises_the_proposal(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    meetings = _count(agent_sessions, Meeting)
    _model(
        monkeypatch,
        _book(
            customer_id,
            "a1",
            contact_name="Alex Smith",
            enquiry_reference="CRM",
            duration=45,
            agenda="External penetration test",
        ),
    )
    body = _amend(agent_client, customer_id, "Alex Smith, CRM enquiry, 45 minutes, external penetration test", pending)

    revised = body["pending_action"]
    assert revised["action_id"] != pending
    payload = revised["payload"]
    assert _utc(payload["scheduled_at"]) == DUBAI_1750
    assert payload["scheduled_timezone"] == DUBAI
    assert (payload["contact_id"], payload["enquiry_id"]) == (agent_records["contact_id"], agent_records["enquiry_id"])
    assert (payload["duration"], payload["agenda"]) == (45, "External penetration test")
    assert body["response"] == (
        "I updated the meeting proposal. Meeting for 2026-10-15 17:50 Asia/Dubai (UTC+04:00); "
        f"contact #{agent_records['contact_id']} Alex Smith; sales enquiry #{agent_records['enquiry_id']} CRM; "
        "45 minutes; agenda: External penetration test. Please confirm this revised version."
    )
    assert _proposals(agent_sessions) == [(pending, "cancelled"), (revised["action_id"], "pending")]
    with agent_sessions() as db:
        old = db.get(HitlActionProposal, pending)
        new = db.get(HitlActionProposal, revised["action_id"])
        assert old.task_id == new.task_id
        assert db.get(HitlTask, new.task_id).status == "awaiting_approval"
        superseded = db.scalar(
            select(HitlActionAudit).where(
                HitlActionAudit.action_id == pending, HitlActionAudit.event_type == "cancelled"
            )
        )
        assert superseded is not None
    assert _count(agent_sessions, Meeting) == meetings  # Nothing written before approval.


def test_details_stated_without_a_model_tool_call_still_revise_the_proposal(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, AIMessage(content="Noted."))
    body = _amend(agent_client, customer_id, "Alex Smith will join, 45 minutes", pending)

    payload = body["pending_action"]["payload"]
    assert body["pending_action"]["action_id"] != pending
    assert (payload["contact_id"], payload["duration"]) == (agent_records["contact_id"], 45)
    assert _utc(payload["scheduled_at"]) == DUBAI_1750


def test_changing_one_field_keeps_every_other_detail(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "a1", contact_name="Alex Smith", duration=45, agenda="Pen test"))
    first = _amend(agent_client, customer_id, "Alex Smith, 45 minutes, agenda pen test", pending)["pending_action"]

    # The model also guesses a new date and timezone the user never mentioned; both are ignored.
    _model(
        monkeypatch,
        _book(customer_id, "a2", duration=30, scheduled_date="2026-10-20", scheduled_timezone="UTC"),
    )
    second = _amend(agent_client, customer_id, "Make it 30 minutes", first["action_id"])["pending_action"]

    payload = second["payload"]
    assert payload["duration"] == 30
    assert _utc(payload["scheduled_at"]) == DUBAI_1750 and payload["scheduled_timezone"] == DUBAI
    assert payload["contact_id"] == agent_records["contact_id"] and payload["agenda"] == "Pen test"
    statuses = [status for _, status in _proposals(agent_sessions)]
    assert statuses == ["cancelled", "cancelled", "pending"]


def test_an_old_approval_cannot_execute_the_superseded_proposal(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    meetings = _count(agent_sessions, Meeting)
    _model(monkeypatch, _book(customer_id, "a1", duration=45))
    revised = _amend(agent_client, customer_id, "Make it 45 minutes", pending)["pending_action"]["action_id"]

    stale = agent_client.post(f"/api/agent/actions/{pending}/confirm")
    assert stale.status_code == 404 and stale.json()["error"]["code"] == "action_not_found"
    assert _count(agent_sessions, Meeting) == meetings

    confirmed = agent_client.post(f"/api/agent/actions/{revised}/confirm")
    assert confirmed.json()["status"] == "completed", confirmed.text
    with agent_sessions() as db:
        meeting = db.get(Meeting, confirmed.json()["record_id"])
        assert meeting.duration == 45 and _utc(meeting.scheduled_at) == DUBAI_1750
        assert db.get(HitlTask, db.get(HitlActionProposal, revised).task_id).status == "completed"
    assert _count(agent_sessions, Meeting) == meetings + 1


def test_repeating_the_same_details_does_not_create_another_proposal(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "a1", scheduled_time="17:50"))
    body = _amend(agent_client, customer_id, "5:50 PM", pending)

    assert body["pending_action"]["action_id"] == pending
    assert body["response"].startswith("The pending meeting proposal already has these details.")
    assert _proposals(agent_sessions) == [(pending, "pending")]


def test_an_unrelated_question_leaves_the_proposal_pending(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(monkeypatch, AIMessage(content="The customer is qualified."))
    body = _amend(agent_client, agent_records["customer_id"], "What stage is the customer in?", pending)

    assert body["response"] == "The customer is qualified."
    assert body["pending_action"]["action_id"] == pending
    assert _proposals(agent_sessions) == [(pending, "pending")]


def test_a_revision_that_conflicts_leaves_the_proposal_unchanged(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    with agent_sessions() as db:
        db.add(
            Meeting(
                customer_id=customer_id,
                organization_id=legacy_organization_id(db),
                scheduled_at=datetime(2026, 10, 15, 14, 30, tzinfo=timezone.utc),
                duration=30,
                status="scheduled",
            )
        )
        db.commit()
    # 120 minutes from 17:50 Dubai now runs into the 18:30 meeting.
    _model(monkeypatch, _book(customer_id, "a1", duration=120), AIMessage(content="That overlaps."))
    body = _amend(agent_client, customer_id, "Make it 120 minutes", pending)

    assert body["response"].startswith("That time overlaps an existing scheduled activity.")
    assert "Your pending meeting proposal is unchanged." in body["response"]
    assert body["pending_action"]["action_id"] == pending
    assert _proposals(agent_sessions) == [(pending, "pending")]


@pytest.fixture
def outsider(agent_sessions: sessionmaker[Session]) -> int:
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


def test_amendments_outside_the_users_scope_are_rejected_before_the_model_runs(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    outsider: int,
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    with agent_sessions() as db:
        colleague = User(
            email="colleague@example.com",
            full_name="Colleague",
            password_hash=TEST_USER_PASSWORD_HASH,
            role="sales",
            organization_id=legacy_organization_id(db),
        )
        customer = db.get(Customer, customer_id)
        other_customer = Customer(company=customer.company, customer_name="Other Customer", status="active")
        db.add_all([colleague, other_customer])
        db.commit()
        colleague_id, other_customer_id = colleague.user_id, other_customer.customer_id
        meeting = dict(db.get(HitlActionProposal, pending).parameters)

    def proposal(user_id: int, action: str = "create_meeting", payload: dict[str, Any] | None = None) -> str:
        with agent_sessions() as db:
            return pending_actions.add(db, action, payload or meeting, "t", user_id=user_id).action_id  # type: ignore[arg-type]

    followup = proposal(
        _user_id(agent_sessions),
        "create_followup",
        {"customer_id": customer_id, "type": "call", "due_date": "2026-10-20T10:00:00Z", "status": "pending"},
    )
    seen = _model(monkeypatch, AIMessage(content="unused"))
    for action_id, target, status, code in [
        (proposal(colleague_id), customer_id, 404, "action_not_found"),
        (proposal(outsider), customer_id, 404, "action_not_found"),
        ("no-such-action", customer_id, 404, "action_not_found"),
        (pending, other_customer_id, 422, "invalid_pending_action"),
        (pending, None, 422, "invalid_pending_action"),
        (followup, customer_id, 422, "invalid_pending_action"),
    ]:
        body = _amend(agent_client, target, "Make it 30 minutes", action_id, status=status)
        assert body["error"]["code"] == code, (action_id, body)
    both = agent_client.post(
        "/api/agent/chat",
        json={
            "message": "30 minutes",
            "customer_id": customer_id,
            "pending_action_id": pending,
            "task_id": "00000000-0000-4000-8000-000000000000",
        },
    )
    assert both.status_code == 422
    assert seen.seen == []
    with agent_sessions() as db:
        assert db.get(HitlActionProposal, pending).status == "pending"


def test_an_approved_proposal_can_no_longer_be_amended(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert agent_client.post(f"/api/agent/actions/{pending}/confirm").json()["status"] == "completed"
    seen = _model(monkeypatch, AIMessage(content="unused"))
    body = _amend(agent_client, agent_records["customer_id"], "Make it 30 minutes", pending, status=409)

    assert body["error"]["code"] == "action_already_processed"
    assert seen.seen == []
    assert [status for _, status in _proposals(agent_sessions)] == ["completed"]


def test_a_revision_racing_an_approval_or_another_revision_changes_nothing(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
) -> None:
    me = _user_id(agent_sessions)
    with agent_sessions() as db:
        revised = {**db.get(HitlActionProposal, pending).parameters, "duration": 30}

    # Two revisions of the same proposal: the second sees it already superseded.
    with agent_sessions() as first, agent_sessions() as second:
        second.get(HitlActionProposal, pending)  # Loaded while still pending.
        winner = pending_actions.supersede(first, pending, revised, user_id=me)
        with pytest.raises(APIError) as lost:
            pending_actions.supersede(second, pending, {**revised, "duration": 60}, user_id=me)
    assert lost.value.code == "action_already_processed"
    assert _proposals(agent_sessions) == [(pending, "cancelled"), (winner.action_id, "pending")]

    # A revision whose proposal was approved (claimed) after it was loaded.
    with agent_sessions() as reviser, agent_sessions() as approver:
        reviser.get(HitlActionProposal, winner.action_id)
        approver.get(HitlActionProposal, winner.action_id).status = "executing"
        approver.commit()
        with pytest.raises(APIError) as lost:
            pending_actions.supersede(reviser, winner.action_id, {**revised, "duration": 90}, user_id=me)
    assert lost.value.code == "action_already_processed"
    assert [status for _, status in _proposals(agent_sessions)] == ["cancelled", "executing"]
