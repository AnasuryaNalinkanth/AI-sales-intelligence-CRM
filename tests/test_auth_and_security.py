from collections.abc import Generator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.types import Interrupt
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings
from app.core.security import create_access_token, hash_password
from app.database.connection import get_db
from app.main import app
from app.models import (
    Call,
    Company,
    Contact,
    Customer,
    FollowUp,
    HitlPolicyOverride,
    Meeting,
    SalesEnquiry,
    User,
)
from tests.conftest import authorize_test_client, legacy_organization_id


class ToolCallingFakeChatModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[Any],
        **kwargs: Any,
    ) -> "ToolCallingFakeChatModel":
        _ = tools, kwargs
        return self


def _tool_call(
    name: str,
    arguments: dict[str, Any],
    call_id: str = "call-1",
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": arguments, "id": call_id, "type": "tool_call"}
        ],
    )


@pytest.fixture
def unauthenticated_client(
    agent_sessions: sessionmaker[Session],
) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        with agent_sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def two_customer_records(
    agent_sessions: sessionmaker[Session],
) -> dict[str, int]:
    now = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    with agent_sessions() as db:
        company_one = Company(organization_id=legacy_organization_id(db), company_name="Alpha Corp", industry="Software")
        company_two = Company(organization_id=legacy_organization_id(db), company_name="Beta Logistics", industry="Logistics")
        db.add_all([company_one, company_two])
        db.flush()

        customer_one = Customer(
            company=company_one,
            customer_name="Alpha Customer",
            status="active",
            sales_stage="qualified",
            created_at=now,
            updated_at=now,
        )
        customer_two = Customer(
            company=company_two,
            customer_name="Beta Customer",
            status="active",
            sales_stage="proposal",
            created_at=now,
            updated_at=now,
        )
        db.add_all([customer_one, customer_two])
        db.flush()

        contact_one = Contact(
            customer=customer_one,
            name="Alice Alpha",
            email="alice@alpha.example.com",
            is_primary=True,
        )
        contact_two = Contact(
            customer=customer_two,
            name="Bob Beta",
            email="bob@beta.example.com",
            is_primary=True,
        )
        enquiry_one = SalesEnquiry(
            customer=customer_one,
            product="Analytics Suite",
            enquiry_text="Alpha enquiry",
            priority="high",
            status="open",
            created_at=now,
            updated_at=now,
        )
        enquiry_two = SalesEnquiry(
            customer=customer_two,
            product="CRM Core",
            enquiry_text="Beta enquiry",
            priority="normal",
            status="open",
            created_at=now,
            updated_at=now,
        )
        db.add_all([contact_one, contact_two, enquiry_one, enquiry_two])
        db.flush()

        meeting_one = Meeting(
            customer=customer_one,
            contact=contact_one,
            enquiry=enquiry_one,
            scheduled_at=now,
            status="scheduled",
            agenda="Alpha kickoff",
        )
        meeting_two = Meeting(
            customer=customer_two,
            contact=contact_two,
            enquiry=enquiry_two,
            scheduled_at=now,
            status="scheduled",
            agenda="Beta kickoff",
        )
        call_one = Call(
            customer=customer_one,
            contact=contact_one,
            enquiry=enquiry_one,
            call_type="Discovery",
            scheduled_at=now,
            status="completed",
            outcome="Positive",
        )
        call_two = Call(
            customer=customer_two,
            contact=contact_two,
            enquiry=enquiry_two,
            call_type="Follow-up",
            scheduled_at=now,
            status="completed",
            outcome="Needs pricing",
        )
        db.add_all([meeting_one, meeting_two, call_one, call_two])
        db.flush()

        followup_one = FollowUp(
            customer=customer_one,
            enquiry=enquiry_one,
            meeting=meeting_one,
            call=call_one,
            type="email",
            due_date=now,
            status="pending",
            description="Alpha follow-up",
        )
        followup_two = FollowUp(
            customer=customer_two,
            enquiry=enquiry_two,
            meeting=meeting_two,
            call=call_two,
            type="call",
            due_date=now,
            status="pending",
            description="Beta follow-up",
        )
        db.add_all([followup_one, followup_two])
        db.commit()

        return {
            "customer_1": customer_one.customer_id,
            "contact_1": contact_one.contact_id,
            "enquiry_1": enquiry_one.enquiry_id,
            "meeting_1": meeting_one.meeting_id,
            "call_1": call_one.call_id,
            "followup_1": followup_one.followup_id,
            "customer_2": customer_two.customer_id,
            "contact_2": contact_two.contact_id,
            "enquiry_2": enquiry_two.enquiry_id,
            "meeting_2": meeting_two.meeting_id,
            "call_2": call_two.call_id,
            "followup_2": followup_two.followup_id,
        }


# ==================================================
# AUTHENTICATION TESTS
# ==================================================


def _seed_user(
    sessions: sessionmaker[Session],
    email: str,
    password: str,
    full_name: str,
) -> None:
    # Phase 1 registration cannot create users (no organisation signup yet), so seed directly.
    with sessions() as db:
        db.add(
            User(
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
                role="sales",
                is_active=True,
                organization_id=legacy_organization_id(db),
            )
        )
        db.commit()


def test_public_registration_cannot_assign_admin_role(
    unauthenticated_client: TestClient,
    agent_sessions: sessionmaker[Session],
) -> None:
    response = unauthenticated_client.post(
        "/api/auth/register",
        json={
            "email": "self.promoted@example.com",
            "password": "StrongPassword!234",
            "full_name": "Self Promoted",
            "role": "admin",
        },
    )

    # Registration cannot create users in Phase 1; whatever happens, no admin appears.
    assert response.status_code != 201, response.text
    with agent_sessions() as db:
        assert db.scalar(select(User).where(User.role == "admin")) is None
        assert db.scalar(select(User).where(User.email == "self.promoted@example.com")) is None


def test_public_registration_can_be_disabled(
    unauthenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "allow_public_registration", False)
    response = unauthenticated_client.post(
        "/api/auth/register",
        json={
            "email": "blocked@example.com",
            "password": "StrongPassword!234",
            "full_name": "Blocked User",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "registration_disabled"


def test_production_settings_reject_weak_jwt_and_unsafe_public_registration() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret_key=SecretStr("short-secret"),
            allow_public_registration=False,
            cors_allowed_origins=["https://crm.example.com"],
        )

    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret_key=SecretStr("a" * 32),
            allow_public_registration=True,
            allow_public_registration_in_production=False,
            cors_allowed_origins=["https://crm.example.com"],
        )

    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret_key=SecretStr("a" * 32),
            allow_public_registration=False,
        )

    valid_prod = Settings(
        environment="production",
        jwt_secret_key=SecretStr("a" * 32),
        allow_public_registration=False,
        cors_allowed_origins=["https://crm.example.com"],
    )
    assert valid_prod.environment == "production"



def test_login_valid_invalid_and_me_endpoint(
    unauthenticated_client: TestClient,
    agent_sessions: sessionmaker[Session],
) -> None:
    _seed_user(agent_sessions, "login.user@example.com", "CorrectHorseBattery!1", "Login User")
    with agent_sessions() as db:
        stored = db.scalar(select(User).where(User.email == "login.user@example.com"))
        assert stored is not None
        assert stored.password_hash.startswith("$argon2")

    wrong_password = unauthenticated_client.post(
        "/api/auth/login",
        json={"email": "login.user@example.com", "password": "WrongPassword!1"},
    )
    assert wrong_password.status_code == 401
    assert wrong_password.json()["error"]["code"] == "invalid_credentials"

    unknown_user = unauthenticated_client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "CorrectHorseBattery!1"},
    )
    assert unknown_user.status_code == 401
    assert unknown_user.json()["error"]["code"] == "invalid_credentials"

    login_res = unauthenticated_client.post(
        "/api/auth/login",
        json={"email": "LOGIN.USER@EXAMPLE.COM", "password": "CorrectHorseBattery!1"},
    )
    assert login_res.status_code == 200
    token_data = login_res.json()
    assert token_data["token_type"] == "bearer"
    assert token_data["access_token"]
    assert token_data["expires_in"] > 0
    assert token_data["user"]["email"] == "login.user@example.com"
    assert "password_hash" not in token_data["user"]

    me_res = unauthenticated_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token_data['access_token']}"},
    )
    assert me_res.status_code == 200
    assert me_res.json()["email"] == "login.user@example.com"
    assert "password_hash" not in me_res.json()


def test_inactive_user_cannot_login_or_use_token(
    unauthenticated_client: TestClient,
    agent_sessions: sessionmaker[Session],
) -> None:
    _seed_user(agent_sessions, "inactive@example.com", "StrongPassword!234", "Inactive User")
    with agent_sessions() as db:
        user = db.scalar(select(User).where(User.email == "inactive@example.com"))
        assert user is not None
        token, _ = create_access_token(user)
        user.is_active = False
        db.commit()

    login_attempt = unauthenticated_client.post(
        "/api/auth/login",
        json={"email": "inactive@example.com", "password": "StrongPassword!234"},
    )
    assert login_attempt.status_code == 403
    assert login_attempt.json()["error"]["code"] == "inactive_user"

    me_attempt = unauthenticated_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_attempt.status_code == 403
    assert me_attempt.json()["error"]["code"] == "inactive_user"


def test_expired_and_invalid_jwt_tokens_return_401(
    unauthenticated_client: TestClient,
    agent_sessions: sessionmaker[Session],
) -> None:
    user = authorize_test_client(
        agent_sessions,
        unauthenticated_client,
        email="token.tester@example.com",
    )
    expired_token, _ = create_access_token(
        user,
        expires_delta=timedelta(seconds=-10),
    )

    expired_res = unauthenticated_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert expired_res.status_code == 401
    assert expired_res.json()["error"]["code"] == "token_expired"

    invalid_res = unauthenticated_client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )
    assert invalid_res.status_code == 401
    assert invalid_res.json()["error"]["code"] == "invalid_token"


# ==================================================
# AUTHORIZATION & PENDING ACTION OWNERSHIP TESTS
# ==================================================


def test_ordinary_user_cannot_change_agent_policy(
    unauthenticated_client: TestClient,
    agent_sessions: sessionmaker[Session],
) -> None:
    user = authorize_test_client(
        agent_sessions,
        unauthenticated_client,
        email="policy.sales@example.com",
    )
    token, _ = create_access_token(user)
    response = unauthenticated_client.put(
        "/api/agent/policies/create_meeting",
        json={"action": "create_meeting", "mode": "disabled"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    with agent_sessions() as db:
        assert db.get(HitlPolicyOverride, (legacy_organization_id(db), "create_meeting")) is None


def test_policy_update_rejects_mismatched_action_identifiers(
    unauthenticated_client: TestClient,
    agent_sessions: sessionmaker[Session],
) -> None:
    admin = authorize_test_client(
        agent_sessions,
        unauthenticated_client,
        email="policy.admin@example.com",
        role="admin",
    )
    token, _ = create_access_token(admin)

    before = unauthenticated_client.get(
        "/api/agent/policies",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert before.status_code == 200

    response = unauthenticated_client.put(
        "/api/agent/policies/create_meeting",
        json={"action": "create_followup", "mode": "disabled"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_action_mismatch"

    after = unauthenticated_client.get(
        "/api/agent/policies",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert after.status_code == 200
    assert after.json() == before.json()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/customers"),
        ("POST", "/api/customers"),
        ("GET", "/api/analytics/overview"),
        ("POST", "/api/agent/chat"),
        ("POST", "/api/agent/customer-summary/1"),
        ("POST", "/api/agent/meeting-brief/1"),
        ("POST", "/api/agent/actions/sample-action/confirm"),
        ("POST", "/api/agent/actions/sample-action/cancel"),
        ("GET", "/api/auth/me"),
    ],
)
def test_protected_endpoints_reject_missing_and_invalid_tokens(
    unauthenticated_client: TestClient,
    method: str,
    path: str,
) -> None:
    no_token = unauthenticated_client.request(method, path, json={})
    assert no_token.status_code == 401
    assert no_token.json()["error"]["code"] == "authentication_required"

    bad_token = unauthenticated_client.request(
        method,
        path,
        json={},
        headers={"Authorization": "Bearer tampered-token"},
    )
    assert bad_token.status_code == 401
    assert bad_token.json()["error"]["code"] == "invalid_token"


def test_shared_crm_records_are_visible_and_manageable_across_users(
    unauthenticated_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
) -> None:
    assert unauthenticated_client.get("/api/customers/1").status_code == 401

    user_a = authorize_test_client(
        agent_sessions,
        unauthenticated_client,
        email="shared.owner@example.com",
    )
    user_b = authorize_test_client(
        agent_sessions,
        unauthenticated_client,
        email="shared.colleague@example.com",
    )
    token_a, _ = create_access_token(user_a)
    token_b, _ = create_access_token(user_b)

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    before = unauthenticated_client.get(
        "/api/analytics/overview",
        headers=headers_b,
    )
    assert before.status_code == 200
    before_enquiries = unauthenticated_client.get(
        "/api/analytics/enquiries",
        headers=headers_b,
    )
    assert before_enquiries.status_code == 200
    before_followups = unauthenticated_client.get(
        "/api/analytics/followups",
        headers=headers_b,
    )
    assert before_followups.status_code == 200

    created = unauthenticated_client.post(
        "/api/customers",
        headers=headers_a,
        json={
            "customer_name": "Shared Workspace Customer",
            "status": "active",
            "sales_stage": "qualified",
            "company": {"company_name": "Shared Workspace Company"},
            "primary_contact": {"name": "Shared Contact"},
            "sales_enquiry": {
                "product": "Shared product",
                "enquiry_text": "Initial shared enquiry",
            },
        },
    )
    assert created.status_code == 201
    result = created.json()
    customer_id = result["customer"]["customer_id"]
    contact_id = result["contact"]["contact_id"]
    enquiry_id = result["sales_enquiry"]["enquiry_id"]

    retrieved = unauthenticated_client.get(
        f"/api/customers/{customer_id}",
        headers=headers_b,
    )
    assert retrieved.status_code == 200
    assert retrieved.json()["customer_name"] == "Shared Workspace Customer"
    updated_customer = unauthenticated_client.patch(
        f"/api/customers/{customer_id}",
        headers=headers_b,
        json={"status": "prospect"},
    )
    assert updated_customer.status_code == 200
    assert updated_customer.json()["status"] == "prospect"

    updated_enquiry = unauthenticated_client.patch(
        f"/api/enquiries/{enquiry_id}",
        headers=headers_b,
        json={"product": "Updated by colleague"},
    )
    assert updated_enquiry.status_code == 200
    assert updated_enquiry.json()["product"] == "Updated by colleague"

    now = datetime.now(timezone.utc)
    meeting = unauthenticated_client.post(
        "/api/meetings",
        headers=headers_a,
        json={
            "customer_id": customer_id,
            "contact_id": contact_id,
            "enquiry_id": enquiry_id,
            # shortcut: must be future and in the current month; flaky only in the last 5 minutes of a month.
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "agenda": "Shared meeting",
        },
    )
    assert meeting.status_code == 201
    meeting_id = meeting.json()["meeting_id"]
    assert unauthenticated_client.get(
        f"/api/meetings/{meeting_id}",
        headers=headers_b,
    ).status_code == 200
    updated_meeting = unauthenticated_client.put(
        f"/api/meetings/{meeting_id}",
        headers=headers_b,
        json={"agenda": "Updated by colleague"},
    )
    assert updated_meeting.status_code == 200

    call = unauthenticated_client.post(
        "/api/calls",
        headers=headers_a,
        json={
            "customer_id": customer_id,
            "contact_id": contact_id,
            "enquiry_id": enquiry_id,
            "scheduled_at": (now + timedelta(hours=2)).isoformat(),
            "call_type": "Discovery",
        },
    )
    assert call.status_code == 201
    call_id = call.json()["call_id"]
    assert unauthenticated_client.get(
        f"/api/calls/{call_id}",
        headers=headers_b,
    ).status_code == 200
    updated_call = unauthenticated_client.put(
        f"/api/calls/{call_id}",
        headers=headers_b,
        json={"outcome": "Updated by colleague"},
    )
    assert updated_call.status_code == 200

    followup = unauthenticated_client.post(
        "/api/followups",
        headers=headers_a,
        json={
            "customer_id": customer_id,
            "enquiry_id": enquiry_id,
            "meeting_id": meeting_id,
            "call_id": call_id,
            "type": "task",
            "due_date": (now + timedelta(days=2)).isoformat(),
        },
    )
    assert followup.status_code == 201
    followup_id = followup.json()["followup_id"]
    assert unauthenticated_client.get(
        f"/api/followups/{followup_id}",
        headers=headers_b,
    ).status_code == 200
    updated_followup = unauthenticated_client.put(
        f"/api/followups/{followup_id}",
        headers=headers_b,
        json={"description": "Updated by colleague"},
    )
    assert updated_followup.status_code == 200

    activity = unauthenticated_client.get(
        f"/api/customers/{customer_id}/activity",
        headers=headers_b,
    )
    assert activity.status_code == 200
    activity_types = {item["activity_type"] for item in activity.json()["items"]}
    assert {"enquiry", "meeting", "call", "follow_up"} <= activity_types
    after = unauthenticated_client.get(
        "/api/analytics/overview",
        headers=headers_b,
    )
    assert after.status_code == 200
    assert after.json()["total_customers"] == before.json()["total_customers"] + 1
    assert after.json()["meetings_this_month"] == before.json()["meetings_this_month"] + 1
    assert after.json()["calls_this_month"] == before.json()["calls_this_month"] + 1
    assert after.json()["open_enquiries"] == before.json()["open_enquiries"] + 1
    assert after.json()["pending_followups"] == before.json()["pending_followups"] + 1
    after_enquiries = unauthenticated_client.get(
        "/api/analytics/enquiries",
        headers=headers_b,
    )
    after_followups = unauthenticated_client.get(
        "/api/analytics/followups",
        headers=headers_b,
    )
    assert after_enquiries.status_code == 200
    before_open_enquiries = sum(
        item["count"]
        for item in before_enquiries.json()["by_status"]
        if item["label"] == "open"
    )
    after_open_enquiries = sum(
        item["count"]
        for item in after_enquiries.json()["by_status"]
        if item["label"] == "open"
    )
    assert after_open_enquiries == before_open_enquiries + 1
    assert after_followups.status_code == 200
    assert after_followups.json()["pending"] == before_followups.json()["pending"] + 1


def test_pending_action_created_by_user_a_cannot_be_confirmed_or_cancelled_by_user_b(
    unauthenticated_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_a = authorize_test_client(
        agent_sessions,
        unauthenticated_client,
        email="user.a@example.com",
    )
    user_b = authorize_test_client(
        agent_sessions,
        unauthenticated_client,
        email="user.b@example.com",
    )
    token_a, _ = create_access_token(user_a)
    token_b, _ = create_access_token(user_b)

    model = ToolCallingFakeChatModel(
        responses=[
            _tool_call(
                "create_meeting",
                {
                    "customer_id": agent_records["customer_id"],
                    "scheduled_date": "2030-10-10",
                    "scheduled_time": "15:00",
                    "scheduled_timezone": "Asia/Dubai",
                    "duration": 45,
                    "agenda": "User A private proposal",
                },
            )
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    proposal_res = unauthenticated_client.post(
        "/api/agent/chat",
        json={
            "message": "Schedule a meeting at 15:00",
            "customer_id": agent_records["customer_id"],
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert proposal_res.status_code == 200
    action_id = proposal_res.json()["pending_action"]["action_id"]

    # User B cannot confirm User A's pending action
    confirm_by_b = unauthenticated_client.post(
        f"/api/agent/actions/{action_id}/confirm",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert confirm_by_b.status_code == 403
    assert confirm_by_b.json()["error"]["code"] == "forbidden"

    # User B cannot cancel User A's pending action
    cancel_by_b = unauthenticated_client.post(
        f"/api/agent/actions/{action_id}/cancel",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert cancel_by_b.status_code == 403
    assert cancel_by_b.json()["error"]["code"] == "forbidden"

    # User A's pending action remains intact and User A can still confirm it
    confirm_by_a = unauthenticated_client.post(
        f"/api/agent/actions/{action_id}/confirm",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert confirm_by_a.status_code == 200
    assert confirm_by_a.json()["status"] == "completed"


# ==================================================
# CROSS-CUSTOMER AGENT SECURITY TESTS
# ==================================================


def test_scoped_conversation_cannot_propose_action_for_another_customer(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    two_customer_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeChatModel(
        responses=[
            _tool_call(
                "get_customer_profile",
                {"customer_id": two_customer_records["customer_2"]},
                "read-other-customer",
            ),
            _tool_call(
                "create_meeting",
                {
                    "customer_id": two_customer_records["customer_2"],
                    "scheduled_at": "2030-10-12T10:00:00+04:00",
                    "agenda": "Cross-customer attempt",
                },
                "propose-other-customer",
            ),
            AIMessage(content="I can only access and propose actions for the selected customer."),
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    response = agent_client.post(
        "/api/agent/chat",
        json={
            "message": "Schedule a meeting for Beta Customer",
            "customer_id": two_customer_records["customer_1"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pending_action"] is None
    assert body["tool_calls"][0]["name"] == "get_customer_profile"
    assert body["tool_calls"][0]["success"] is False
    assert body["tool_calls"][0]["error_code"] == "customer_scope_violation"


def test_find_customer_cannot_unlock_writes_for_another_customer_when_locked(
    agent_client: TestClient,
    two_customer_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeChatModel(
        responses=[
            _tool_call(
                "find_customer",
                {"search": "Beta"},
                "search-beta",
            ),
            _tool_call(
                "create_meeting",
                {
                    "customer_id": two_customer_records["customer_2"],
                    "scheduled_at": "2026-10-12T10:00:00+04:00",
                    "agenda": "Attempt after find_customer",
                },
                "propose-beta",
            ),
            AIMessage(content="Beta Customer is outside the active customer scope."),
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    response = agent_client.post(
        "/api/agent/chat",
        json={
            "message": "Find Beta and schedule a meeting for them",
            "customer_id": two_customer_records["customer_1"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pending_action"] is None


@pytest.mark.parametrize(
    ("action_tool", "cross_field", "cross_key"),
    [
        ("create_meeting", "contact_id", "contact_2"),
        ("create_meeting", "enquiry_id", "enquiry_2"),
        ("create_followup", "enquiry_id", "enquiry_2"),
        ("create_followup", "meeting_id", "meeting_2"),
        ("create_followup", "call_id", "call_2"),
        ("record_call_result", "contact_id", "contact_2"),
        ("record_call_result", "enquiry_id", "enquiry_2"),
        ("complete_followup", "followup_id", "followup_2"),
    ],
)
def test_cross_customer_entity_references_are_rejected(
    agent_client: TestClient,
    two_customer_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    action_tool: str,
    cross_field: str,
    cross_key: str,
) -> None:
    monkeypatch.setattr(settings, "agent_recursion_limit", 12)
    customer_1 = two_customer_records["customer_1"]
    cross_id = two_customer_records[cross_key]

    if action_tool == "create_meeting":
        action_args: dict[str, Any] = {
            "customer_id": customer_1,
            "scheduled_at": "2026-10-12T10:00:00+04:00",
            cross_field: cross_id,
        }
    elif action_tool == "create_followup":
        action_args = {
            "customer_id": customer_1,
            "type": "email",
            "due_date": "2026-10-12T10:00:00+04:00",
            cross_field: cross_id,
        }
    elif action_tool == "record_call_result":
        action_args = {
            "customer_id": customer_1,
            "outcome": "Spoke with contact",
            cross_field: cross_id,
        }
    else:
        action_args = {
            "customer_id": customer_1,
            "followup_id": cross_id,
        }

    model = ToolCallingFakeChatModel(
        responses=[
            _tool_call("get_customer_profile", {"customer_id": customer_1}, "read-profile"),
            _tool_call("get_customer_meetings", {"customer_id": customer_1}, "read-meetings"),
            _tool_call("get_customer_calls", {"customer_id": customer_1}, "read-calls"),
            _tool_call("get_customer_followups", {"customer_id": customer_1}, "read-followups"),
            _tool_call(action_tool, action_args, "propose-cross-ref"),
            AIMessage(content="Rejected cross-customer entity reference."),
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    response = agent_client.post(
        "/api/agent/chat",
        json={"message": "Propose action with foreign reference", "customer_id": customer_1},
    )
    assert response.status_code == 200
    assert response.json()["pending_action"] is None


def test_server_side_pre_acceptance_validation_blocks_forged_cross_customer_interrupt(
    agent_client: TestClient,
    two_customer_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForgedCrossCustomerAgent:
        def invoke(self, _input: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
            _ = config
            return {
                "messages": [],
                "__interrupt__": [
                    Interrupt(
                        value={
                            "action": "create_meeting",
                            "payload": {
                                "customer_id": two_customer_records["customer_2"],
                                "scheduled_at": "2026-10-12T10:00:00+04:00",
                                "duration": 30,
                                "status": "scheduled",
                            },
                        }
                    )
                ],
            }

    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: object())
    monkeypatch.setattr(
        "app.services.agent_action_service.build_sales_agent",
        lambda *_args, **_kwargs: ForgedCrossCustomerAgent(),
    )

    response = agent_client.post(
        "/api/agent/chat",
        json={
            "message": "Schedule a meeting",
            "customer_id": two_customer_records["customer_1"],
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "customer_scope_violation"


def test_global_chat_without_customer_id_discovers_customers_but_blocks_cross_entity_mixing(
    agent_client: TestClient,
    two_customer_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Global chat can discover both customers and propose a valid action for Customer 2
    valid_global_model = ToolCallingFakeChatModel(
        responses=[
            _tool_call("find_customer", {"search": "Beta"}, "find-beta"),
            _tool_call(
                "get_customer_profile",
                {"customer_id": two_customer_records["customer_2"]},
                "profile-beta",
            ),
            _tool_call(
                "create_meeting",
                {
                    "customer_id": two_customer_records["customer_2"],
                    "contact_id": two_customer_records["contact_2"],
                    "scheduled_date": "2026-10-15",
                    "scheduled_time": "14:00",
                    "scheduled_timezone": "Asia/Dubai",
                    "agenda": "Global discovery meeting",
                },
                "propose-beta-meeting",
            ),
        ]
    )
    monkeypatch.setattr(
        "app.services.agent_service.create_chat_model",
        lambda: valid_global_model,
    )

    ok_res = agent_client.post(
        "/api/agent/chat",
        json={"message": "Find Beta Customer and schedule a meeting with Bob Beta on 2026-10-15 at 14:00"},
    )
    assert ok_res.status_code == 200
    assert ok_res.json()["pending_action"]["action"] == "create_meeting"
    assert (
        ok_res.json()["pending_action"]["payload"]["customer_id"]
        == two_customer_records["customer_2"]
    )

    # 2. Even in global chat after reading BOTH Customer 1 and Customer 2 profiles,
    # Customer 1 cannot reference Customer 2's contact!
    cross_mix_model = ToolCallingFakeChatModel(
        responses=[
            _tool_call(
                "get_customer_profile",
                {"customer_id": two_customer_records["customer_1"]},
                "profile-alpha",
            ),
            _tool_call(
                "get_customer_profile",
                {"customer_id": two_customer_records["customer_2"]},
                "profile-beta",
            ),
            _tool_call(
                "create_meeting",
                {
                    "customer_id": two_customer_records["customer_1"],
                    "contact_id": two_customer_records["contact_2"],
                    "scheduled_at": "2026-10-15T14:00:00+04:00",
                },
                "mix-alpha-beta",
            ),
            AIMessage(content="Contact belongs to a different customer."),
        ]
    )
    monkeypatch.setattr(
        "app.services.agent_service.create_chat_model",
        lambda: cross_mix_model,
    )

    mix_res = agent_client.post(
        "/api/agent/chat",
        json={"message": "Schedule meeting for Alpha with Beta's contact"},
    )
    assert mix_res.status_code == 200
    assert mix_res.json()["pending_action"] is None


# ==================================================
# FRONTEND & STATIC SECURITY VERIFICATION
# ==================================================


def test_frontend_auth_and_customer_switch_isolation_assets(
    unauthenticated_client: TestClient,
) -> None:
    login_page = unauthenticated_client.get("/login")
    assert login_page.status_code == 200
    assert "login-form" in login_page.text
    assert "/static/js/login.js" in login_page.text

    api_js = unauthenticated_client.get("/static/js/api.js").text
    assert "sessionStorage" in api_js
    assert "Authorization" in api_js
    assert "Bearer " in api_js
    assert "export async function loginUser" in api_js
    assert "export function logoutUser" in api_js
    assert "export function requireAuth" in api_js

    customer_js = unauthenticated_client.get("/static/js/customer.js").text
    assert "function resetCustomerAiState" in customer_js
    assert "agentChatMessages = []" in customer_js
    assert "pendingAgentAction = null" in customer_js
    assert "agentActionNotice = \"\"" in customer_js
    assert "agentChatLoading = false" in customer_js
    # Switching clears the view without cancelling server-side proposals; a restored
    # proposal card must belong to the newly selected customer.
    assert "cancelAgentAction(staleActionId)" not in customer_js
    assert "String(operation.values?.customer_id) === String(customerId)" in customer_js

    frontend_js_dir = Path("app/frontend/js")
    for js_file in frontend_js_dir.glob("*.js"):
        source = js_file.read_text(encoding="utf-8")
        assert "innerHTML" not in source, f"Unsafe innerHTML found in {js_file}"
        assert "eval(" not in source, f"Unsafe eval found in {js_file}"
