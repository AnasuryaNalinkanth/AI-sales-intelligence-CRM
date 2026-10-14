"""Sales leads: capture, Ready/Not Ready qualification, Won/Lost closure, isolation, and HITL."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.tools import AgentToolContext
from app.agent.tools.write_tools import build_action_proposal_tools
from app.core.exceptions import APIError
from app.core.security import create_access_token
from app.models import FollowUp, Meeting, Organization, SalesEnquiry, User
from app.services.hitl_policy import get_action_policy, set_action_policy
from tests.conftest import TEST_USER_PASSWORD_HASH, legacy_organization_id
from tests.test_agent_actions import _chat_model, _configure_action_policy, _post_chat, _tool_call


def _create_lead(client: TestClient, customer_id: int, **values: Any) -> dict[str, Any]:
    response = client.post(
        "/api/enquiries",
        json={"customer_id": customer_id, "enquiry_text": "Needs 20 seats", **values},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _qualify(client: TestClient, enquiry_id: int, status: str, **values: Any):
    return client.post(
        f"/api/enquiries/{enquiry_id}/qualification",
        json={"qualification_status": status, **values},
    )


def _close(client: TestClient, enquiry_id: int, outcome: str, loss_reason: str | None = None):
    return client.post(
        f"/api/enquiries/{enquiry_id}/close",
        json={"outcome": outcome, "loss_reason": loss_reason},
    )


def test_create_lead_defaults_to_not_ready_and_links_existing_records(
    agent_client: TestClient, agent_records: dict[str, int]
) -> None:
    rep = agent_client.get("/api/enquiries/assignees").json()[0]
    lead = _create_lead(
        agent_client,
        agent_records["customer_id"],
        product="Analytics add-on",
        lead_source="referral",
        contact_id=agent_records["contact_id"],
        assigned_user_id=rep["user_id"],
        estimated_value="5000.00",
        expected_close_date="2026-12-31",
    )

    assert lead["qualification_status"] == "not_ready"
    assert lead["outcome"] is None and lead["closed_at"] is None
    assert lead["contact_id"] == agent_records["contact_id"]
    listed = agent_client.get("/api/enquiries", params={"search": "Analytics"}).json()
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["customer_name"] == "ABC Trading Customer"
    assert item["company_name"] == "ABC Trading"
    assert item["contact_name"] == "Alex Smith"
    assert item["assigned_user_name"] == rep["full_name"]
    customer = agent_client.get(f"/api/customers/{agent_records['customer_id']}").json()
    assert lead["enquiry_id"] in {row["enquiry_id"] for row in customer["sales_enquiries"]}


@pytest.mark.parametrize(
    ("values", "status_code", "code"),
    [
        ({"enquiry_text": ""}, 422, None),
        ({"estimated_value": "-1"}, 422, None),
        ({"lead_source": "cold_call"}, 422, None),
        ({"qualification_status": "won"}, 422, None),
        ({"contact_id": 999}, 404, "contact_not_found"),
        ({"assigned_user_id": 999}, 404, "assignee_not_found"),
        ({"customer_id": 999}, 404, "customer_not_found"),
    ],
)
def test_create_lead_rejects_invalid_input(
    agent_client: TestClient,
    agent_records: dict[str, int],
    values: dict[str, Any],
    status_code: int,
    code: str | None,
) -> None:
    body = {"customer_id": agent_records["customer_id"], "enquiry_text": "Needs seats", **values}
    response = agent_client.post("/api/enquiries", json=body)
    assert response.status_code == status_code, response.text
    if code:
        assert response.json()["error"]["code"] == code


def test_qualification_transitions_and_filters(
    agent_client: TestClient, agent_records: dict[str, int], agent_sessions: sessionmaker[Session]
) -> None:
    lead_id = _create_lead(agent_client, agent_records["customer_id"])["enquiry_id"]

    ready = _qualify(agent_client, lead_id, "ready", qualification_reason="Budget approved")
    assert ready.status_code == 200
    assert ready.json()["qualification_status"] == "ready"
    assert ready.json()["qualification_reason"] == "Budget approved"

    # A follow-up belongs only with a Not Ready decision.
    assert _qualify(
        agent_client, lead_id, "ready",
        next_followup={"type": "call", "due_date": "2026-10-10T09:00:00Z"},
    ).status_code == 422

    not_ready = _qualify(
        agent_client,
        lead_id,
        "not_ready",
        qualification_reason="Waiting for next budget cycle",
        next_followup={"type": "call", "due_date": "2026-10-10T09:00:00Z", "description": "Check budget"},
    )
    assert not_ready.status_code == 200, not_ready.text
    with agent_sessions() as db:
        followup = db.scalar(select(FollowUp).where(FollowUp.enquiry_id == lead_id))
        assert followup is not None and followup.description == "Check budget"

    ready_ids = {i["enquiry_id"] for i in agent_client.get(
        "/api/enquiries", params={"qualification_status": "ready"}).json()["items"]}
    not_ready_ids = {i["enquiry_id"] for i in agent_client.get(
        "/api/enquiries", params={"qualification_status": "not_ready"}).json()["items"]}
    assert lead_id in not_ready_ids and lead_id not in ready_ids
    assert agent_client.get("/api/enquiries", params={"qualification_status": "won"}).status_code == 422


def test_failed_followup_rolls_back_the_qualification(
    agent_client: TestClient, agent_records: dict[str, int], agent_sessions: sessionmaker[Session]
) -> None:
    lead_id = _create_lead(agent_client, agent_records["customer_id"])["enquiry_id"]
    _qualify(agent_client, lead_id, "ready")

    # A past due date is refused by the follow-up service, so nothing changes.
    response = _qualify(
        agent_client, lead_id, "not_ready",
        next_followup={"type": "call", "due_date": "2026-09-01T09:00:00Z"},
    )
    assert response.status_code >= 400
    with agent_sessions() as db:
        assert db.get(SalesEnquiry, lead_id).qualification_status == "ready"


def test_only_ready_open_opportunities_close_and_closed_ones_are_read_only(
    agent_client: TestClient, agent_records: dict[str, int], agent_sessions: sessionmaker[Session]
) -> None:
    lead_id = agent_records["enquiry_id"]  # Has a meeting, call and follow-up linked.

    not_active = _close(agent_client, lead_id, "won")
    assert not_active.status_code == 409
    assert not_active.json()["error"]["code"] == "opportunity_not_active"

    _qualify(agent_client, lead_id, "ready")
    assert _close(agent_client, lead_id, "lost").status_code == 422  # Loss reason required.
    assert _close(agent_client, lead_id, "won", "n/a").status_code == 422

    lost = _close(agent_client, lead_id, "lost", "Chose a competitor")
    assert lost.status_code == 200, lost.text
    body = lost.json()
    assert body["outcome"] == "lost" and body["loss_reason"] == "Chose a competitor"
    assert body["closed_at"] is not None
    assert body["status"] == "lost"
    assert body["qualification_status"] == "ready"  # Outcome never overwrites qualification.

    for response in (
        agent_client.patch(f"/api/enquiries/{lead_id}", json={"status": "open"}),
        agent_client.patch(f"/api/enquiries/{lead_id}", json={"estimated_value": "1"}),
        _qualify(agent_client, lead_id, "not_ready"),
        _close(agent_client, lead_id, "won"),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "opportunity_closed"

    with agent_sessions() as db:
        assert db.scalar(select(Meeting.enquiry_id).where(Meeting.enquiry_id == lead_id)) == lead_id
        assert db.scalar(select(FollowUp.enquiry_id).where(FollowUp.enquiry_id == lead_id)) == lead_id
    lost_ids = {i["enquiry_id"] for i in agent_client.get(
        "/api/enquiries", params={"outcome": "lost"}).json()["items"]}
    open_ids = {i["enquiry_id"] for i in agent_client.get(
        "/api/enquiries", params={"outcome": "open"}).json()["items"]}
    assert lead_id in lost_ids and lead_id not in open_ids


def test_won_outcome_is_reported_in_analytics(
    agent_client: TestClient, agent_records: dict[str, int]
) -> None:
    won_id = _create_lead(agent_client, agent_records["customer_id"], estimated_value="1000")["enquiry_id"]
    lost_id = _create_lead(agent_client, agent_records["customer_id"], estimated_value="400")["enquiry_id"]
    for lead_id in (won_id, lost_id):
        _qualify(agent_client, lead_id, "ready")
    assert _close(agent_client, won_id, "won").json()["status"] == "converted"
    _close(agent_client, lost_id, "lost", "No budget")

    metrics = agent_client.get("/api/analytics/enquiries").json()
    outcomes = {item["label"]: (item["count"], float(item["value"])) for item in metrics["by_outcome"]}
    assert outcomes == {"won": (1, 1000.0), "lost": (1, 400.0)}
    assert float(metrics["win_rate_percent"]) == 50.0
    qualification = {item["label"]: item["count"] for item in metrics["by_qualification"]}
    assert qualification == {"not_ready": 1}  # Only the fixture's open lead remains.
    overview = agent_client.get("/api/analytics/overview").json()
    assert overview["converted_enquiries"] == 1


def test_legacy_enquiry_routes_still_work(
    agent_client: TestClient, agent_records: dict[str, int]
) -> None:
    lead_id = agent_records["enquiry_id"]
    patched = agent_client.patch(f"/api/enquiries/{lead_id}", json={"status": "in_progress"})
    assert patched.status_code == 200 and patched.json()["status"] == "in_progress"
    assert agent_client.get(f"/api/enquiries/{lead_id}").status_code == 200
    listed = agent_client.get(f"/api/customers/{agent_records['customer_id']}/enquiries")
    assert [row["enquiry_id"] for row in listed.json()] == [lead_id]


def test_other_organization_cannot_see_or_change_leads(
    agent_client: TestClient, agent_records: dict[str, int], agent_sessions: sessionmaker[Session]
) -> None:
    lead_id = agent_records["enquiry_id"]
    with agent_sessions() as db:
        other = Organization(name="Other Org", slug="other-org")
        db.add(other)
        db.flush()
        outsider = User(
            email="outsider@example.com",
            full_name="Outsider",
            password_hash=TEST_USER_PASSWORD_HASH,
            role="admin",
            organization_id=other.organization_id,
        )
        db.add(outsider)
        db.commit()
        db.refresh(outsider)
    token, _ = create_access_token(outsider)
    agent_client.headers["Authorization"] = f"Bearer {token}"

    assert agent_client.get("/api/enquiries").json()["total"] == 0
    assert "Outsider" in {u["full_name"] for u in agent_client.get("/api/enquiries/assignees").json()}
    assert "Sales Representative" not in {
        u["full_name"] for u in agent_client.get("/api/enquiries/assignees").json()
    }
    for response in (
        agent_client.get(f"/api/enquiries/{lead_id}"),
        _qualify(agent_client, lead_id, "ready"),
        _close(agent_client, lead_id, "won"),
    ):
        assert response.status_code == 404
    assert agent_client.post(
        "/api/enquiries",
        json={"customer_id": agent_records["customer_id"], "enquiry_text": "Steal"},
    ).status_code == 404
    with agent_sessions() as db:
        assert db.get(SalesEnquiry, lead_id).qualification_status == "not_ready"


def test_lead_actions_cannot_be_made_automatic(agent_sessions: sessionmaker[Session]) -> None:
    for action in ("update_lead_qualification", "close_opportunity"):
        with pytest.raises(APIError) as error:
            _configure_action_policy(agent_sessions, action, "automatic")
        assert error.value.code == "automatic_not_allowed"
        with agent_sessions() as db:
            organization_id = legacy_organization_id(db)
            assert get_action_policy(db, action, organization_id=organization_id) == "approval_required"


def test_agent_closure_waits_for_approval_then_executes(
    agent_client: TestClient,
    agent_records: dict[str, int],
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id, lead_id = agent_records["customer_id"], agent_records["enquiry_id"]
    _qualify(agent_client, lead_id, "ready")
    _chat_model(
        monkeypatch,
        _tool_call("get_sales_enquiries", {"customer_id": customer_id}, "read"),
        _tool_call(
            "close_opportunity",
            {"customer_id": customer_id, "enquiry_id": lead_id, "outcome": "won"},
            "close",
        ),
    )
    pending = _post_chat(agent_client, customer_id, "We won this deal, close it as won.")["pending_action"]
    assert pending["action"] == "close_opportunity"
    with agent_sessions() as db:
        assert db.get(SalesEnquiry, lead_id).outcome is None  # Nothing written yet.

    result = agent_client.post(f"/api/agent/actions/{pending['action_id']}/confirm")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"
    with agent_sessions() as db:
        lead = db.get(SalesEnquiry, lead_id)
        assert lead.outcome == "won" and lead.closed_at is not None


def test_agent_tools_refuse_lost_without_reason_and_inactive_or_closed_leads(
    agent_client: TestClient,
    agent_records: dict[str, int],
    agent_sessions: sessionmaker[Session],
) -> None:
    customer_id, lead_id = agent_records["customer_id"], agent_records["enquiry_id"]
    with agent_sessions() as db:
        context = AgentToolContext(organization_id=legacy_organization_id(db))
        context.remember_enquiry(customer_id, lead_id)
        tools = {tool.name: tool for tool in build_action_proposal_tools(context, db)}

        def close(**args: Any) -> str:
            result = tools["close_opportunity"].invoke(
                {"customer_id": customer_id, "enquiry_id": lead_id, **args}
            )
            return json.loads(result)["error"]["code"]

        assert close(outcome="lost") == "loss_reason_required"
        assert close(outcome="won") == "opportunity_not_active"  # Still Not Ready.
        _qualify(agent_client, lead_id, "ready")
        _close(agent_client, lead_id, "won")
        assert close(outcome="lost", loss_reason="Late change") == "opportunity_closed"
        qualify = tools["update_lead_qualification"].invoke(
            {"customer_id": customer_id, "enquiry_id": lead_id, "qualification_status": "not_ready"}
        )
        assert json.loads(qualify)["error"]["code"] == "opportunity_closed"


def test_disabled_policy_blocks_approved_qualification(
    agent_client: TestClient,
    agent_records: dict[str, int],
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id, lead_id = agent_records["customer_id"], agent_records["enquiry_id"]
    _chat_model(
        monkeypatch,
        _tool_call("get_sales_enquiries", {"customer_id": customer_id}, "read"),
        _tool_call(
            "update_lead_qualification",
            {"customer_id": customer_id, "enquiry_id": lead_id, "qualification_status": "ready"},
            "qualify",
        ),
    )
    pending = _post_chat(agent_client, customer_id, "Mark this lead Ready.")["pending_action"]
    assert pending["action"] == "update_lead_qualification"
    _configure_action_policy(agent_sessions, "update_lead_qualification", "disabled")

    response = agent_client.post(f"/api/agent/actions/{pending['action_id']}/confirm")
    assert response.status_code == 403
    with agent_sessions() as db:
        assert db.get(SalesEnquiry, lead_id).qualification_status == "not_ready"


def test_structured_task_asks_for_loss_reason_before_review() -> None:
    from app.services.hitl_action_registry import validate_operation

    base = {"customer_id": 1, "enquiry_id": 2}
    assert validate_operation("close_opportunity", {**base, "outcome": "lost"}) == (None, ["loss_reason"])
    payload, missing = validate_operation(
        "close_opportunity", {**base, "outcome": "lost", "loss_reason": "Price"}
    )
    assert missing == [] and payload["outcome"] == "lost"
    assert validate_operation(
        "update_lead_qualification", {**base, "qualification_status": "ready"}
    )[1] == []
    with pytest.raises(APIError):
        validate_operation("update_lead_qualification", {**base, "qualification_status": "won"})


@pytest.mark.parametrize("link", ["contact", "assigned_user"])
def test_tenant_guard_rejects_cross_organization_contact_or_assignee(
    agent_records: dict[str, int], agent_sessions: sessionmaker[Session], link: str
) -> None:
    from app.models import Company, Contact, Customer
    from app.models.tenancy import CrossOrganizationReference

    with agent_sessions() as db:
        other = Organization(name="Other Org", slug="other-org")
        db.add(other)
        db.flush()
        company = Company(organization_id=other.organization_id, company_name="Other Co")
        customer = Customer(company=company, customer_name="Other Customer")
        foreign_contact = Contact(customer=customer, name="Other Contact")
        foreign_user = User(
            email="other.rep@example.com",
            full_name="Other Rep",
            password_hash=TEST_USER_PASSWORD_HASH,
            organization_id=other.organization_id,
        )
        db.add_all([company, customer, foreign_contact, foreign_user])
        db.commit()

        lead = db.get(SalesEnquiry, agent_records["enquiry_id"])
        setattr(lead, link, foreign_contact if link == "contact" else foreign_user)
        with pytest.raises(CrossOrganizationReference):
            db.flush()
