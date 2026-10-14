from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.rate_limit import ai_rate_limiter
from app.core.security import create_access_token, hash_password
from app.database.connection import Base, get_db
from app.main import app
from app.models import (
    Call,
    Company,
    Contact,
    Customer,
    FollowUp,
    Meeting,
    Organization,
    SalesEnquiry,
    SchedulingLock,
    User,
)
from app.models.organization import LEGACY_ORGANIZATION_NAME, LEGACY_ORGANIZATION_SLUG

TEST_USER_PASSWORD = "TestPassword123!"
TEST_USER_PASSWORD_HASH = hash_password(TEST_USER_PASSWORD)
# Test data uses fixed 2026 dates, so scheduling "now" (past-time checks and
# suggestions) is pinned instead of following the real clock.
FROZEN_SCHEDULING_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _FrozenSchedulingClock(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return FROZEN_SCHEDULING_NOW if tz else FROZEN_SCHEDULING_NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def freeze_scheduling_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.scheduling_service.datetime", _FrozenSchedulingClock)


@pytest.fixture(autouse=True)
def default_ai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests assume the shipped default, whatever a developer's local .env selects.
    monkeypatch.setattr(settings, "ai_provider", "gemini")


@pytest.fixture(autouse=True)
def ensure_jwt_test_secret(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    ai_rate_limiter.reset()
    if len(settings.jwt_secret_key.get_secret_value().strip()) < 32:
        monkeypatch.setattr(
            settings,
            "jwt_secret_key",
            SecretStr("test-only-jwt-secret-key-at-least-32-bytes-long"),
        )
    yield
    ai_rate_limiter.reset()



def legacy_organization_id(db: Session) -> int:
    """The legacy workspace every pre-existing user belongs to, created on first use."""
    organization = db.scalar(
        select(Organization).where(Organization.slug == LEGACY_ORGANIZATION_SLUG)
    )
    if organization is None:
        organization = Organization(name=LEGACY_ORGANIZATION_NAME, slug=LEGACY_ORGANIZATION_SLUG)
        db.add(organization)
        db.flush()
    return organization.organization_id


def authorize_test_client(
    sessions: sessionmaker[Session],
    client: TestClient,
    *,
    email: str = "sales.rep@example.com",
    full_name: str = "Sales Representative",
    role: str = "sales",
    is_active: bool = True,
) -> User:
    with sessions() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                full_name=full_name,
                password_hash=TEST_USER_PASSWORD_HASH,
                role=role,
                is_active=is_active,
                organization_id=legacy_organization_id(db),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
    token, _ = create_access_token(user)
    client.headers["Authorization"] = f"Bearer {token}"
    return user


@pytest.fixture
def agent_engine() -> Generator[Engine, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(SchedulingLock(lock_id=1))
        db.commit()
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def agent_sessions(agent_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=agent_engine, expire_on_commit=False)


@pytest.fixture
def agent_client(
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    monkeypatch.setattr("app.agent.agent.SessionLocal", agent_sessions)

    def override_get_db() -> Generator[Session, None, None]:
        with agent_sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        authorize_test_client(agent_sessions, client)
        yield client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def agent_records(agent_sessions: sessionmaker[Session]) -> dict[str, int]:
    now = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    with agent_sessions() as db:
        company = Company(organization_id=legacy_organization_id(db), company_name="ABC Trading", industry="Retail")
        db.add(company)
        db.flush()
        customer = Customer(
            company=company,
            customer_name="ABC Trading Customer",
            status="active",
            sales_stage="qualified",
            created_at=now,
            updated_at=now,
        )
        db.add(customer)
        db.flush()
        contact = Contact(
            customer=customer,
            name="Alex Smith",
            job_title="Buyer",
            email="alex@example.com",
            is_primary=True,
        )
        enquiry = SalesEnquiry(
            customer=customer,
            product="CRM",
            enquiry_text="Interested in a CRM subscription",
            priority="normal",
            status="open",
            created_at=now,
            updated_at=now,
        )
        db.add_all([contact, enquiry])
        db.flush()
        meeting = Meeting(
            customer=customer,
            contact=contact,
            enquiry=enquiry,
            scheduled_at=now,
            status="scheduled",
            agenda="Product demonstration",
        )
        call = Call(
            customer=customer,
            contact=contact,
            enquiry=enquiry,
            call_type="Discovery",
            scheduled_at=now,
            status="completed",
            outcome="Interested",
        )
        db.add_all([meeting, call])
        db.flush()
        followup = FollowUp(
            customer=customer,
            enquiry=enquiry,
            meeting=meeting,
            call=call,
            type="proposal",
            due_date=now,
            status="pending",
            description="Send proposal",
        )
        db.add(followup)
        db.commit()
        return {
            "customer_id": customer.customer_id,
            "enquiry_id": enquiry.enquiry_id,
            "meeting_id": meeting.meeting_id,
            "call_id": call.call_id,
            "followup_id": followup.followup_id,
            "contact_id": contact.contact_id,
        }
