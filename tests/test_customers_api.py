from datetime import datetime
from collections.abc import Generator
from io import BytesIO
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base, get_db
from app.main import app
from app.models import (
    Call,
    Company,
    Contact,
    Customer,
    FollowUp,
    Meeting,
    SalesEnquiry,
    SchedulingLock,
)
from app.services.import_service import EXPECTED_SHEETS
from tests.conftest import authorize_test_client


@pytest.fixture
def test_engine() -> Generator[Engine, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(SchedulingLock(lock_id=1))
        db.commit()
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def test_session_factory(test_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest.fixture
def client(test_session_factory: sessionmaker[Session]) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        with test_session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        authorize_test_client(test_session_factory, test_client)
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def valid_registration(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "customer_name": "Ada Lovelace",
        "status": "active",
        "sales_stage": "qualified",
        "company": {
            "company_name": "Analytical Engines Ltd",
            "industry": "Technology",
            "city": "London",
        },
        "primary_contact": {
            "name": "Ada Lovelace",
            "job_title": "Director",
            "email": "ada@example.com",
            "phone": "+44 20 1234 5678",
        },
        "sales_enquiry": {
            "product": "CRM",
            "enquiry_text": "Interested in a team subscription.",
            "priority": "high",
            "status": "open",
            "estimated_value": "1250.50",
        },
    }
    for key, value in overrides.items():
        data[key] = value
    return data


def imported_workbook(
    *,
    priority: str = "medium",
    scheduled_at: Any = "2026-10-02T10:00:00+04:00",
    scheduled_timezone: str | None = "Asia/Dubai",
    meeting_duration: Any = 30,
    call_scheduled_at: Any = datetime(2026, 10, 3),
    call_scheduled_timezone: str | None = None,
    call_time_occurrence: str | None = None,
    followup_due_date: Any = datetime(2026, 10, 4),
    followup_due_timezone: str | None = None,
    followup_time_occurrence: str | None = None,
    include_second_customer: bool = False,
    foreign_reference: tuple[str, str] | None = None,
    second_meeting_scheduled_at: Any = "2026-10-02T11:00:00+04:00",
) -> bytes:
    workbook = Workbook()
    sheets = {
        "Companies": (
            ("company_id", "company_name", "industry", "website", "address", "city", "country", "company_size", "description"),
            (1, "Imported Company", None, None, None, None, None, None, None),
        ),
        "Customers": (
            ("customer_id", "company_id", "customer_name", "status", "sales_stage"),
            (1, 1, "Imported Customer", "prospect", "qualified"),
        ),
        "Contacts": (
            ("contact_id", "customer_id", "name", "job_title", "email", "phone", "is_primary"),
            (1, 1, "Imported Contact", None, None, None, True),
        ),
        "Sales_Enquiries": (
            ("enquiry_id", "customer_id", "product", "enquiry_text", "priority", "status", "estimated_value", "created_at"),
            (1, 1, "CRM", "Interested in a subscription.", priority, "open", None, datetime(2026, 10, 1)),
        ),
        "Meetings": (
            ("meeting_id", "customer_id", "contact_id", "enquiry_id", "scheduled_at", "duration", "status", "agenda", "notes", "summary", "scheduled_timezone"),
            (1, 1, 1, 1, scheduled_at, meeting_duration, "scheduled", "Introduction", None, None, scheduled_timezone),
        ),
        "Calls": (
            ("call_id", "customer_id", "contact_id", "enquiry_id", "call_type", "scheduled_at", "actual_time", "status", "outcome", "notes", "summary", "next_followup_date", "scheduled_timezone", "scheduled_time_occurrence"),
            (1, 1, 1, 1, "Discovery", call_scheduled_at, None, "missed", None, None, None, None, call_scheduled_timezone, call_time_occurrence),
        ),
        "Follow_Ups": (
            ("followup_id", "customer_id", "enquiry_id", "meeting_id", "call_id", "type", "due_date", "status", "description", "assigned_to", "completed_at", "due_timezone", "due_time_occurrence"),
            (1, 1, 1, 1, 1, "email", followup_due_date, "pending", None, None, None, followup_due_timezone, followup_time_occurrence),
        ),
    }

    for sheet_name in EXPECTED_SHEETS:
        worksheet = workbook.active if sheet_name == "Companies" else workbook.create_sheet(sheet_name)
        worksheet.title = sheet_name
        if sheet_name in sheets:
            headers, row = sheets[sheet_name]
            worksheet.append(headers)
            worksheet.append(row)
            if include_second_customer:
                second_rows = {
                    "Companies": (2, "Independent Company", None, None, None, None, None, None, None),
                    "Customers": (2, 2, "Independent Customer", "prospect", "new"),
                    "Contacts": (2, 2, "Independent Contact", None, None, None, False),
                    "Sales_Enquiries": (2, 2, "CRM", "Independent enquiry.", "normal", "open", None, datetime(2026, 10, 1)),
                    "Meetings": (2, 2, 2, 2, second_meeting_scheduled_at, 30, "scheduled", "Independent meeting", None, None, "Asia/Dubai"),
                    "Calls": (2, 2, 2, 2, "Discovery", datetime(2026, 10, 3), None, "missed", None, None, None, None, None, None),
                }
                if sheet_name in second_rows:
                    worksheet.append(second_rows[sheet_name])
            if foreign_reference is not None and foreign_reference[0] == sheet_name:
                worksheet.cell(
                    row=2,
                    column=headers.index(foreign_reference[1]) + 1,
                    value=2,
                )

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_valid_customer_registration_creates_all_records(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    response = client.post("/api/customers", json=valid_registration())

    assert response.status_code == 201
    result = response.json()
    assert result["message"] == "Customer registered successfully"
    assert result["customer"]["customer_name"] == "Ada Lovelace"
    assert result["company"]["company_name"] == "Analytical Engines Ltd"
    assert result["contact"]["is_primary"] is True
    assert result["sales_enquiry"]["estimated_value"] == "1250.50"

    with test_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        assert db.scalar(select(func.count()).select_from(Customer)) == 1
        assert db.scalar(select(func.count()).select_from(Contact)) == 1
        assert db.scalar(select(func.count()).select_from(SalesEnquiry)) == 1
        customer = db.get(Customer, result["customer"]["customer_id"])
        contact = db.scalar(select(Contact))
        enquiry = db.scalar(select(SalesEnquiry))
        assert customer is not None
        assert contact is not None and contact.customer_id == customer.customer_id
        assert enquiry is not None and enquiry.customer_id == customer.customer_id
        assert contact.is_primary is True
        assert customer.created_at is not None
        assert contact.created_at is not None
        assert enquiry.created_at is not None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("customer_name",), " "),
        (("company", "company_name"), " "),
        (("primary_contact", "name"), ""),
        (("sales_enquiry", "enquiry_text"), " "),
    ],
)
def test_required_registration_fields_reject_blank_values(
    client: TestClient,
    path: tuple[str, ...],
    value: str,
) -> None:
    data = valid_registration()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    response = client.post("/api/customers", json=data)

    assert response.status_code == 422


def test_invalid_contact_email_is_rejected(client: TestClient) -> None:
    data = valid_registration()
    data["primary_contact"]["email"] = "not-an-email"

    assert client.post("/api/customers", json=data).status_code == 422


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("sales_enquiry", "priority"), "critical-ish"),
        (("status",), "pending"),
        (("sales_stage",), "unqualified"),
        (("sales_enquiry", "status"), "pending"),
    ],
)
def test_registration_rejects_undefined_enum_values(
    client: TestClient,
    path: tuple[str, ...],
    value: str,
) -> None:
    data = valid_registration()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert client.post("/api/customers", json=data).status_code == 422


def test_negative_estimated_value_is_rejected(client: TestClient) -> None:
    data = valid_registration()
    data["sales_enquiry"]["estimated_value"] = "-0.01"

    assert client.post("/api/customers", json=data).status_code == 422


def test_duplicate_company_is_reused_for_another_customer(client: TestClient) -> None:
    first = client.post("/api/customers", json=valid_registration())
    second_data = valid_registration(customer_name="Grace Hopper")
    second_data["company"]["company_name"] = "  ANALYTICAL ENGINES LTD "

    second = client.post("/api/customers", json=second_data)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["company"]["company_id"] == first.json()["company"]["company_id"]


def test_duplicate_customer_under_company_returns_conflict(client: TestClient) -> None:
    assert client.post("/api/customers", json=valid_registration()).status_code == 201

    response = client.post("/api/customers", json=valid_registration(customer_name=" ADA LOVELACE "))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_customer"


def test_failed_enquiry_flush_rolls_back_entire_registration(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    def fail_on_sales_enquiry(
        session: Session,
        flush_context: object,
        instances: object,
    ) -> None:
        del flush_context, instances
        if any(isinstance(item, SalesEnquiry) for item in session.new):
            raise SQLAlchemyError("test-only database failure")

    event.listen(Session, "before_flush", fail_on_sales_enquiry)
    try:
        response = client.post("/api/customers", json=valid_registration())
    finally:
        event.remove(Session, "before_flush", fail_on_sales_enquiry)

    assert response.status_code == 500
    assert "test-only database failure" not in response.text
    with test_session_factory() as db:
        for model in (Company, Customer, Contact, SalesEnquiry):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_get_customer_returns_company_contacts_and_enquiries(client: TestClient) -> None:
    created = client.post("/api/customers", json=valid_registration()).json()

    response = client.get(f"/api/customers/{created['customer']['customer_id']}")

    assert response.status_code == 200
    result = response.json()
    assert result["customer_name"] == "Ada Lovelace"
    assert result["company"]["company_name"] == "Analytical Engines Ltd"
    assert result["contacts"][0]["is_primary"] is True
    assert result["sales_enquiries"][0]["enquiry_text"] == "Interested in a team subscription."


def test_get_missing_customer_returns_404(client: TestClient) -> None:
    response = client.get("/api/customers/999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "customer_not_found"


def test_imported_medium_priority_loads_in_customer_overview(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    upload = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert upload.status_code == 200
    overview = client.get("/api/customers/1/overview")

    assert overview.status_code == 200
    assert overview.json()["sales_enquiries"][0]["priority"] == "medium"
    # Imported leads record their source and start Not Ready; nothing is inferred.
    assert overview.json()["sales_enquiries"][0]["lead_source"] == "import"
    assert overview.json()["sales_enquiries"][0]["qualification_status"] == "not_ready"
    assert overview.json()["calls"][0]["status"] == "missed"
    assert overview.json()["meetings"][0]["scheduled_at"] == "2026-10-02T10:00:00+04:00"
    assert overview.json()["meetings"][0]["scheduled_timezone"] == "Asia/Dubai"
    with test_session_factory() as db:
        meeting = db.get(Meeting, 1)
        assert meeting is not None
        assert meeting.scheduled_at == datetime(2026, 10, 2, 6, 0)
        assert meeting.scheduled_timezone == "Asia/Dubai"


def test_import_local_call_and_followup_times_preserve_timezone(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    upload = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(
                    call_scheduled_at=datetime(2030, 1, 10, 9),
                    call_scheduled_timezone="Asia/Dubai",
                    followup_due_date=datetime(2030, 1, 11, 10),
                    followup_due_timezone="Asia/Dubai",
                ),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert upload.status_code == 200
    with test_session_factory() as db:
        call = db.get(Call, 1)
        followup = db.get(FollowUp, 1)
        assert call is not None
        assert call.scheduled_at == datetime(2030, 1, 10, 5)
        assert call.scheduled_timezone == "Asia/Dubai"
        assert followup is not None
        assert followup.due_date == datetime(2030, 1, 11, 6)
        assert followup.due_timezone == "Asia/Dubai"


@pytest.mark.parametrize(
    ("call_scheduled_at", "occurrence"),
    [
        (datetime(2030, 3, 10, 2, 30), None),
        (datetime(2030, 11, 3, 1, 30), None),
    ],
)
def test_import_rejects_nonexistent_or_ambiguous_local_call_time(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
    call_scheduled_at: datetime,
    occurrence: str | None,
) -> None:
    response = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(
                    call_scheduled_at=call_scheduled_at,
                    call_scheduled_timezone="America/New_York",
                    call_time_occurrence=occurrence,
                ),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_import"
    assert "Calls record 1" in response.json()["error"]["message"]
    with test_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0


def test_import_accepts_explicit_ambiguous_local_call_occurrence(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    upload = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(
                    call_scheduled_at=datetime(2030, 11, 3, 1, 30),
                    call_scheduled_timezone="America/New_York",
                    call_time_occurrence="later",
                ),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert upload.status_code == 200
    with test_session_factory() as db:
        call = db.get(Call, 1)
        assert call is not None
        assert call.scheduled_at == datetime(2030, 11, 3, 6, 30)
        assert call.scheduled_timezone == "America/New_York"


@pytest.mark.parametrize(
    "scheduled_at",
    ["2026-10-02", "2026-10-02T10:00:00"],
)
def test_import_rejects_meeting_timestamps_without_timezone(
    client: TestClient,
    scheduled_at: str,
) -> None:
    response = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(scheduled_at=scheduled_at, scheduled_timezone=None),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_import"


def test_import_rejects_zero_meeting_duration(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    response = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(meeting_duration=0),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 422
    assert "Meetings record 1 has an invalid duration" in response.json()["error"]["message"]
    with test_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0


def test_import_schedule_conflict_rolls_back_the_entire_workbook(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    response = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(
                    include_second_customer=True,
                    second_meeting_scheduled_at="2026-10-02T10:00:00+04:00",
                ),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "schedule_conflict"
    with test_session_factory() as db:
        for model in (Company, Customer, Contact, SalesEnquiry, Meeting, Call, FollowUp):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_import_rejects_unknown_enquiry_priority(client: TestClient) -> None:
    response = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(priority="critical"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_import"


@pytest.mark.parametrize(
    ("sheet_name", "field"),
    [
        ("Meetings", "contact_id"),
        ("Meetings", "enquiry_id"),
        ("Calls", "contact_id"),
        ("Calls", "enquiry_id"),
        ("Follow_Ups", "enquiry_id"),
        ("Follow_Ups", "meeting_id"),
        ("Follow_Ups", "call_id"),
    ],
)
def test_import_rejects_cross_customer_relationships_atomically(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
    sheet_name: str,
    field: str,
) -> None:
    response = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(
                    include_second_customer=True,
                    foreign_reference=(sheet_name, field),
                ),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_import"
    assert f"{sheet_name} row 2" in response.json()["error"]["message"]
    with test_session_factory() as db:
        for model in (Company, Customer, Contact, SalesEnquiry, Meeting, Call, FollowUp):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_import_accepts_independent_records_for_multiple_customers(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    response = client.post(
        "/api/import",
        files={
            "file": (
                "crm.xlsx",
                imported_workbook(include_second_customer=True),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200
    with test_session_factory() as db:
        customers = list(db.scalars(select(Customer).order_by(Customer.customer_id)))
        contacts = list(db.scalars(select(Contact).order_by(Contact.contact_id)))
        enquiries = list(
            db.scalars(select(SalesEnquiry).order_by(SalesEnquiry.enquiry_id))
        )
        assert [customer.customer_id for customer in customers] == [1, 2]
        assert [contact.customer_id for contact in contacts] == [1, 2]
        assert [enquiry.customer_id for enquiry in enquiries] == [1, 2]


def test_list_customers_returns_paginated_shape(client: TestClient) -> None:
    client.post("/api/customers", json=valid_registration())
    client.post("/api/customers", json=valid_registration(customer_name="Grace Hopper"))

    response = client.get("/api/customers")

    assert response.status_code == 200
    result = response.json()
    assert result["page"] == 1
    assert result["page_size"] == 20
    assert result["total"] == 2
    assert len(result["items"]) == 2
    assert result["items"][0]["company"]["company_name"] == "Analytical Engines Ltd"


@pytest.mark.parametrize(
    ("parameter", "query", "expected_name", "expected_total"),
    [
        ("search", "Analytical Engines", "Ada Lovelace", 2),
        ("search", "Grace", "Grace Hopper", 1),
        ("customer_name", "Ada", "Ada Lovelace", 1),
        ("company_name", "Analytical", "Ada Lovelace", 2),
    ],
)
def test_customer_search_filters_results(
    client: TestClient,
    parameter: str,
    query: str,
    expected_name: str,
    expected_total: int,
) -> None:
    client.post("/api/customers", json=valid_registration())
    client.post("/api/customers", json=valid_registration(customer_name="Grace Hopper"))

    response = client.get("/api/customers", params={parameter: query})

    assert response.status_code == 200
    result = response.json()
    assert result["total"] == expected_total
    assert expected_name in {item["customer_name"] for item in result["items"]}


def test_customer_list_paginates_and_limits_page_size(client: TestClient) -> None:
    for name in ("Ada Lovelace", "Grace Hopper", "Katherine Johnson"):
        client.post("/api/customers", json=valid_registration(customer_name=name))

    first_page = client.get("/api/customers", params={"page": 1, "page_size": 2}).json()
    second_page = client.get("/api/customers", params={"page": 2, "page_size": 2}).json()
    invalid_page_size = client.get("/api/customers", params={"page_size": 101})

    assert first_page["total"] == 3
    assert len(first_page["items"]) == 2
    assert second_page["page"] == 2
    assert len(second_page["items"]) == 1
    assert invalid_page_size.status_code == 422
