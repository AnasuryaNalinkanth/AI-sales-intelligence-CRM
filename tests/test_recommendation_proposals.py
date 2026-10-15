"""AI proposals for rule-ranked recommendations, and their path through the existing HITL tasks."""

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.rate_limit import ai_rate_limiter
from app.core.security import hash_password
from app.models import FollowUp, HitlActionProposal, Organization, User
from app.services import recommendation_proposal_service
from app.services.hitl_policy import set_action_policy
from tests.conftest import legacy_organization_id
from tests.test_hitl_tasks import _create_second_user
from tests.test_proactive_insights import _at, _customer, _enquiry, _followup

INSIGHTS = "/api/analytics/proactive-insights"


class FakeModel:
    def __init__(self, reply: Any = None, *, error: Exception | None = None, delay: float = 0) -> None:
        self.reply = reply
        self.error = error
        self.delay = delay
        self.messages: list[Any] = []

    async def ainvoke(self, messages: Any) -> AIMessage:
        self.messages.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return AIMessage(content=content)


def _reply(action: str | None = None, **values: str) -> dict[str, Any]:
    return {
        "explanation": "The lead has gone quiet for months.",
        "next_step": "Agree a dated next step with the customer.",
        "action": action,
        "values": values,
    }


def _install(monkeypatch: pytest.MonkeyPatch, model: FakeModel) -> FakeModel:
    monkeypatch.setattr(recommendation_proposal_service, "create_chat_model", lambda **_: model)
    return model


@pytest.fixture
def records(
    agent_sessions: sessionmaker[Session], frozen_now: None
) -> dict[str, int]:
    with agent_sessions() as db:
        quiet = _customer(db, "Quiet Co")
        quiet_lead = _enquiry(db, quiet, created=_at(5, 1), priority="high")
        late = _customer(db, "Late Co")
        late_lead = _enquiry(db, late, created=_at(9, 1))
        overdue = _followup(db, late, late_lead, _at(9, 20))
        rival_org = Organization(name="Rival Corp", slug="rival-corp")
        db.add(rival_org)
        db.flush()
        rival = _customer(db, "Rival Co", organization_id=rival_org.organization_id)
        rival_lead = _enquiry(db, rival, created=_at(5, 1), priority="high")
        db.commit()
        return {
            "quiet_customer": quiet.customer_id,
            "quiet_lead": quiet_lead.enquiry_id,
            "late_customer": late.customer_id,
            "overdue": overdue.followup_id,
            "late_lead": late_lead.enquiry_id,
            "rival_customer": rival.customer_id,
            "rival_lead": rival_lead.enquiry_id,
        }


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import _FrozenSchedulingClock

    monkeypatch.setattr("app.api.routes.analytics.datetime", _FrozenSchedulingClock)


def _propose(client: TestClient, recommendation_id: str):
    return client.post(f"{INSIGHTS}/{recommendation_id}/proposal")


def _disable(sessions: sessionmaker[Session], action: str) -> None:
    with sessions() as db:
        admin = User(
            email="policy.admin@example.com", full_name="Policy Admin",
            password_hash=hash_password("AdminPassword123!"), role="admin", is_active=True,
            organization_id=legacy_organization_id(db),
        )
        db.add(admin)
        db.commit()
        set_action_policy(
            db, action, "disabled", organization_id=admin.organization_id, changed_by=admin.email
        )


def _followups(sessions: sessionmaker[Session]) -> int:
    with sessions() as db:
        return db.scalar(select(func.count()).select_from(FollowUp)) or 0


def test_scores_and_ranking_stay_rule_based(
    agent_client: TestClient, records: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = agent_client.get(INSIGHTS).json()
    _install(monkeypatch, FakeModel(_reply("schedule_call", call_type="Check-in")))

    body = _propose(agent_client, f"enquiry-{records['quiet_lead']}").json()

    assert agent_client.get(INSIGHTS).json() == before
    assert set(body) == {"recommendation_id", "explanation", "next_step", "proposed_action"}


def test_model_sees_only_rule_text_for_this_recommendation(
    agent_client: TestClient, records: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install(monkeypatch, FakeModel(_reply()))

    _propose(agent_client, f"enquiry-{records['quiet_lead']}")

    sent = " ".join(str(message.content) for message in model.messages[0])
    for private in ("Quiet Co", "Late Co", "Rival Co", "Needs a CRM", "score"):
        assert private not in sent.split("Recommendation (data")[1], private
    assert "allowed_actions" in sent


def test_a_proposal_becomes_an_approval_task_and_writes_only_once_after_approval(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        FakeModel(_reply("create_followup", type="Call back", description="Confirm budget")),
    )

    proposal = _propose(agent_client, f"enquiry-{records['quiet_lead']}").json()

    action = proposal["proposed_action"]
    assert action["action"] == "create_followup"
    assert action["values"] == {
        "customer_id": records["quiet_customer"],
        "enquiry_id": records["quiet_lead"],
        "type": "Call back",
        "description": "Confirm budget",
    }
    assert action["user_must_provide"] == ["due_date"]  # never a model-invented deadline
    assert action["policy"] == "approval_required"
    assert action["timezone_field"] == "due_timezone"

    before = _followups(agent_sessions)
    values = {**action["values"], "due_timezone": "Asia/Dubai"}
    task = agent_client.post(
        "/api/agent/tasks", json={"operations": [{"action": "create_followup", "values": values}]}
    ).json()
    assert task["status"] == "collecting_information"
    assert task["operations"][0]["missing_fields"] == ["due_date"]

    task = agent_client.post(
        f"/api/agent/tasks/{task['task_id']}/inputs",
        json={"operation_index": 0, "values": {"due_date": "2026-10-10T10:00:00"}},
    ).json()
    assert task["status"] == "awaiting_approval"
    action_id = task["operations"][0]["action_id"]
    assert _followups(agent_sessions) == before  # nothing written before approval

    confirmed = agent_client.post(f"/api/agent/actions/{action_id}/confirm")
    assert confirmed.json()["status"] == "completed"
    assert _followups(agent_sessions) == before + 1

    again = agent_client.post(f"/api/agent/actions/{action_id}/confirm")
    assert again.status_code == 404  # a processed action is single-use
    assert _followups(agent_sessions) == before + 1


def test_complete_followup_uses_the_recommendations_followup_and_waits_for_approval(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeModel(_reply("complete_followup")))

    action = _propose(agent_client, f"enquiry-{records['late_lead']}").json()["proposed_action"]

    assert action["values"] == {
        "customer_id": records["late_customer"],
        "followup_id": records["overdue"],
    }
    assert action["user_must_provide"] == []
    task = agent_client.post(
        "/api/agent/tasks",
        json={"operations": [{"action": "complete_followup", "values": action["values"]}]},
    ).json()
    assert task["status"] == "awaiting_approval"
    with agent_sessions() as db:
        assert db.get(FollowUp, records["overdue"]).status == "pending"


@pytest.mark.parametrize(
    "reply",
    [
        _reply("create_followup", type="Call", due_date="2026-10-05T09:00:00"),  # a deadline
        _reply("create_followup", type="Call", customer_id="999"),  # an ID
        _reply("schedule_call", call_type="x" * 51),  # fails the action schema
        _reply("complete_followup"),  # not allowed: this lead has no overdue follow-up
        _reply("close_opportunity"),  # not an action recommendations may propose
        _reply(None, notes="Stray"),  # values without an action
        {**_reply("schedule_call"), "policy": "automatic"},  # the model cannot set policy
        {**_reply("schedule_call"), "approved": True},
        "Sure! I'd schedule a call.",  # not JSON
    ],
)
def test_malformed_or_overreaching_model_output_is_rejected(
    agent_client: TestClient, records: dict[str, int], monkeypatch: pytest.MonkeyPatch, reply: Any
) -> None:
    _install(monkeypatch, FakeModel(reply))

    response = _propose(agent_client, f"enquiry-{records['quiet_lead']}")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "malformed_ai_output"
    assert agent_client.get(INSIGHTS).status_code == 200  # the panel still works


def test_no_action_is_proposed_when_none_fits(
    agent_client: TestClient, records: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeModel(_reply()))

    body = _propose(agent_client, f"enquiry-{records['quiet_lead']}").json()

    assert body["proposed_action"] is None and body["explanation"]


def test_timeouts_and_provider_errors_degrade_gracefully(
    agent_client: TestClient, records: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "analytics_ask_timeout_seconds", 0.1)
    _install(monkeypatch, FakeModel(_reply(), delay=5))
    timed_out = _propose(agent_client, f"enquiry-{records['quiet_lead']}")

    _install(monkeypatch, FakeModel(error=RuntimeError("connection refused")))
    failed = _propose(agent_client, f"enquiry-{records['quiet_lead']}")

    assert (timed_out.status_code, timed_out.json()["error"]["code"]) == (504, "llm_timeout")
    assert (failed.status_code, failed.json()["error"]["code"]) == (502, "llm_provider_error")
    assert agent_client.get(INSIGHTS).status_code == 200


def test_disabled_actions_stay_blocked(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeModel(_reply("schedule_call", call_type="Check-in")))
    _disable(agent_sessions, "schedule_call")

    action = _propose(agent_client, f"enquiry-{records['quiet_lead']}").json()["proposed_action"]
    task = agent_client.post(
        "/api/agent/tasks",
        json={"operations": [{"action": "schedule_call", "values": action["values"]}]},
    )

    assert action["policy"] == "disabled"
    assert task.status_code == 403 and task.json()["error"]["code"] == "action_disabled"


def test_a_pending_action_disabled_later_cannot_execute(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeModel(_reply("complete_followup")))
    action = _propose(agent_client, f"enquiry-{records['late_lead']}").json()["proposed_action"]
    task = agent_client.post(
        "/api/agent/tasks",
        json={"operations": [{"action": "complete_followup", "values": action["values"]}]},
    ).json()
    _disable(agent_sessions, "complete_followup")

    response = agent_client.post(f"/api/agent/actions/{task['operations'][0]['action_id']}/confirm")

    assert response.status_code == 403 and response.json()["error"]["code"] == "action_disabled"
    with agent_sessions() as db:
        assert db.get(FollowUp, records["overdue"]).status == "pending"


def test_changed_arguments_invalidate_the_approval(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeModel(_reply("complete_followup")))
    action = _propose(agent_client, f"enquiry-{records['late_lead']}").json()["proposed_action"]
    task = agent_client.post(
        "/api/agent/tasks",
        json={"operations": [{"action": "complete_followup", "values": action["values"]}]},
    ).json()
    action_id = task["operations"][0]["action_id"]
    with agent_sessions() as db:
        other = _followup(db, db.get(FollowUp, records["overdue"]).customer, None, _at(9, 1))
        proposal = db.get(HitlActionProposal, action_id)
        proposal.parameters = {**proposal.parameters, "followup_id": other.followup_id}
        db.commit()
        other_id = other.followup_id

    response = agent_client.post(f"/api/agent/actions/{action_id}/confirm")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "action_parameters_changed"
    with agent_sessions() as db:
        assert db.get(FollowUp, other_id).status == "pending"
        assert db.get(FollowUp, records["overdue"]).status == "pending"


def test_other_organizations_recommendations_look_missing(
    agent_client: TestClient, records: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install(monkeypatch, FakeModel(_reply()))

    for recommendation_id in (
        f"enquiry-{records['rival_lead']}",
        f"customer-{records['rival_customer']}",
        "enquiry-999999",
        "lead-1",
        "enquiry-0",
    ):
        response = _propose(agent_client, recommendation_id)
        assert response.status_code == 404, recommendation_id
        assert response.json()["error"]["code"] == "recommendation_not_found"
    assert model.messages == []  # the model is never asked about them


def test_a_cross_organization_reference_cannot_be_submitted_as_a_task(
    agent_client: TestClient, records: dict[str, int]
) -> None:
    response = agent_client.post(
        "/api/agent/tasks",
        json={"operations": [{"action": "schedule_call", "values": {
            "customer_id": records["rival_customer"], "enquiry_id": records["rival_lead"],
        }}]},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "customer_not_found"


def test_another_user_cannot_approve_the_action(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeModel(_reply("complete_followup")))
    action = _propose(agent_client, f"enquiry-{records['late_lead']}").json()["proposed_action"]
    task = agent_client.post(
        "/api/agent/tasks",
        json={"operations": [{"action": "complete_followup", "values": action["values"]}]},
    ).json()
    _create_second_user(agent_sessions, agent_client)

    response = agent_client.post(f"/api/agent/actions/{task['operations'][0]['action_id']}/confirm")

    assert response.status_code in (403, 404)
    with agent_sessions() as db:
        assert db.get(FollowUp, records["overdue"]).status == "pending"


def test_unauthenticated_requests_are_rejected(
    agent_client: TestClient, records: dict[str, int]
) -> None:
    agent_client.headers.pop("Authorization", None)

    assert _propose(agent_client, f"enquiry-{records['quiet_lead']}").status_code == 401


def test_proposals_are_rate_limited(
    agent_client: TestClient, records: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_rate_limit_requests", 1)
    ai_rate_limiter.reset()
    _install(monkeypatch, FakeModel(_reply()))

    statuses = [
        _propose(agent_client, f"enquiry-{records['quiet_lead']}").status_code for _ in range(2)
    ]

    assert statuses == [200, 429]
