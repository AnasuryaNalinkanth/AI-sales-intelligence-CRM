import asyncio
import json
import os
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, get_args

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.tools import AgentToolContext, build_read_only_tools
from app.core.config import settings
from app.core.rate_limit import ai_rate_limiter
from app.models import Company, Customer, Organization, SalesEnquiry, User
from app.schemas.analytics import (
    MAX_ANALYTICS_DATE,
    MIN_ANALYTICS_DATE,
    AnalyticsDateRange,
    AnalyticsOperation,
    date_range_error,
)
from app.services import analytics_query_service, analytics_service
from tests.conftest import legacy_organization_id

ASK = "/api/analytics/ask"
START, END = "2026-01-01", "2026-12-31"

# Written out independently of the registry, so a mis-mapped operation fails here.
EXPECTED_SOURCES: dict[str, tuple[Any, tuple[str, ...]]] = {
    "customer_totals": (
        analytics_service.get_overview_metrics,
        ("total_customers", "active_customers", "inactive_customers", "prospects"),
    ),
    "new_customers": (analytics_service.get_overview_metrics, ("new_customers",)),
    "customers_by_status": (analytics_service.get_customer_metrics, ("by_status",)),
    "customers_by_sales_stage": (analytics_service.get_customer_metrics, ("by_sales_stage",)),
    "customers_by_industry": (analytics_service.get_customer_metrics, ("by_industry",)),
    "customers_created_over_time": (analytics_service.get_customer_metrics, ("created_over_time",)),
    "leads_by_status": (analytics_service.get_enquiry_metrics, ("by_status",)),
    "leads_by_priority": (analytics_service.get_enquiry_metrics, ("by_priority",)),
    "leads_by_product": (analytics_service.get_enquiry_metrics, ("by_product",)),
    "open_leads_by_qualification": (analytics_service.get_enquiry_metrics, ("by_qualification",)),
    "closed_leads_by_outcome": (analytics_service.get_enquiry_metrics, ("by_outcome",)),
    "lead_value_by_status": (analytics_service.get_enquiry_metrics, ("by_status_value",)),
    "total_lead_value": (analytics_service.get_enquiry_metrics, ("total_estimated_value",)),
    "lead_win_rate": (analytics_service.get_enquiry_metrics, ("win_rate_percent",)),
    "leads_created_over_time": (analytics_service.get_enquiry_metrics, ("over_time",)),
    "pipeline_value_by_stage": (analytics_service.get_pipeline_metrics, ("by_sales_stage",)),
    "lead_to_meeting_conversion": (
        analytics_service.get_pipeline_metrics,
        ("enquiry_to_meeting_conversion_percent",),
    ),
    "activity_over_time": (analytics_service.get_activity_metrics, ("over_time",)),
    "meetings_by_status": (analytics_service.get_activity_metrics, ("meetings_by_status",)),
    "calls_by_status": (analytics_service.get_activity_metrics, ("calls_by_status",)),
    "followup_summary": (
        analytics_service.get_followup_metrics,
        ("overdue", "due_today", "due_next_7_days", "pending", "completed", "cancelled"),
    ),
}
GRAIN_OPERATIONS = {"customers_created_over_time", "leads_created_over_time", "activity_over_time"}
TODAY = date(2026, 10, 15)  # A Thursday; the reference date for relative periods in these tests.


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analytics_query_service, "_utc_today", lambda: TODAY)


class FakePlanModel:
    """Returns one fixed reply and records what the model was shown."""

    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.calls: list[list[Any]] = []

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return AIMessage(content=content)


def _install(monkeypatch: pytest.MonkeyPatch, reply: object) -> FakePlanModel:
    model = FakePlanModel(reply)
    monkeypatch.setattr(analytics_query_service, "create_chat_model", lambda **_: model)
    return model


def _add_closed_leads(sessions: sessionmaker[Session], customer_id: int) -> None:
    closed_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
    with sessions() as db:
        customer = db.get(Customer, customer_id)
        db.add_all(
            [
                SalesEnquiry(
                    customer=customer, product="CRM", enquiry_text="Won deal", priority="high",
                    status="closed", estimated_value=Decimal("3000.00"), outcome="won",
                    closed_at=closed_at, created_at=closed_at, updated_at=closed_at,
                ),
                SalesEnquiry(
                    customer=customer, product="ERP", enquiry_text="Lost deal", priority="low",
                    status="lost", estimated_value=Decimal("1000.00"), outcome="lost",
                    closed_at=closed_at, loss_reason="Budget", created_at=closed_at,
                    updated_at=closed_at,
                ),
            ]
        )
        db.commit()


def test_registry_matches_operation_schema_and_service_fields() -> None:
    assert set(analytics_query_service.OPERATIONS) == set(get_args(AnalyticsOperation))
    assert set(EXPECTED_SOURCES) == set(get_args(AnalyticsOperation))
    for name, spec in analytics_query_service.OPERATIONS.items():
        assert (spec.fetch, spec.fields) == EXPECTED_SOURCES[name], name
        assert spec.uses_grain == (name in GRAIN_OPERATIONS), name


@pytest.mark.parametrize(
    "drift",
    [
        lambda ops: ops.pop("lead_win_rate"),
        lambda ops: ops.update(extra=ops["new_customers"]),
        lambda ops: ops.update(new_customers=replace(ops["new_customers"], fields=("new_clients",))),
        lambda ops: ops.update(
            lead_win_rate=replace(ops["lead_win_rate"], reason_field="win_rate_reason")
        ),
        lambda ops: ops.update(
            new_customers=replace(ops["new_customers"], uses_grain=True)  # overview takes no grain
        ),
    ],
)
def test_registry_drift_fails_loudly(monkeypatch: pytest.MonkeyPatch, drift) -> None:
    operations = dict(analytics_query_service.OPERATIONS)
    drift(operations)
    monkeypatch.setattr(analytics_query_service, "OPERATIONS", operations)
    with pytest.raises(RuntimeError):
        analytics_query_service._check_registry()


@pytest.mark.parametrize("operation", sorted(get_args(AnalyticsOperation)))
def test_every_operation_returns_the_existing_service_figures(
    operation: str,
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_closed_leads(agent_sessions, agent_records["customer_id"])
    grain = "weekly" if operation in GRAIN_OPERATIONS else None
    _install(
        monkeypatch,
        {"operation": operation, "start_date": START, "end_date": END, "grain": grain},
    )

    response = agent_client.post(ASK, json={"question": f"Show me {operation}"})

    assert response.status_code == 200, response.text
    body = response.json()
    fetch, fields = EXPECTED_SOURCES[operation]
    with agent_sessions() as db:
        kwargs = {"grain": grain} if grain else {}
        expected = fetch(
            db, date(2026, 1, 1), date(2026, 12, 31),
            organization_id=legacy_organization_id(db), **kwargs,
        ).model_dump(mode="json")
    result = body["result"]
    assert body["status"] == "answered"
    assert body["interpretation"] == {
        "operation": operation, "start_date": START, "end_date": END, "grain": grain,
        "month": None, "last_week": False, "last_n_days": None,
    }
    if len(fields) > 1:
        assert result["items"] == [
            {"label": field, "count": expected[field], "value": None} for field in fields
        ]
    elif result["kind"] in ("number", "percent", "amount"):
        assert result["value"] == expected[fields[0]]
        assert result["items"] is None
    else:
        assert result["items"] == expected[fields[0]]
    assert body["summary"]
    assert set(body["timings_ms"]) == {"interpret", "execute"}


def test_summaries_are_deterministic_and_state_all_time_and_relative_dates(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_closed_leads(agent_sessions, agent_records["customer_id"])

    _install(monkeypatch, {"operation": "lead_win_rate", "start_date": START, "end_date": END})
    body = agent_client.post(ASK, json={"question": "Roughly, Win rate this year?"}).json()
    assert body["result"]["value"] == "50.00"
    assert body["summary"].startswith(
        "Sales lead win rate (leads created from 2026-01-01 to 2026-12-31): 50.00%. "
    )

    # Dates are requested but do not apply to all-time totals; the result and summary say so.
    _install(monkeypatch, {"operation": "customer_totals", "start_date": "2020-01-01", "end_date": "2020-12-31"})
    body = agent_client.post(ASK, json={"question": "How many customers in 2020?"}).json()
    assert body["result"]["all_time"] is True
    assert body["result"]["items"][0] == {"label": "total_customers", "count": 1, "value": None}
    assert "all-time figure" in body["summary"].lower()

    _install(monkeypatch, {"operation": "followup_summary"})
    body = agent_client.post(ASK, json={"question": "Follow-up status?"}).json()
    assert "current UTC date" in body["result"]["note"]
    assert body["summary"].startswith("Follow-up status (all records): overdue ")


def test_unavailable_percentage_is_null_with_reason(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # agent_records has only an open lead, so no win rate can be calculated.
    _install(monkeypatch, {"operation": "lead_win_rate"})

    body = agent_client.post(ASK, json={"question": "Roughly, What is our win rate?"}).json()

    assert body["result"]["value"] is None
    assert body["result"]["unavailable_reason"].startswith("No opportunities")
    assert body["summary"].startswith("Sales lead win rate (all records): unavailable. No ")


def test_time_series_without_grain_defaults_to_monthly_and_others_drop_grain(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"operation": "activity_over_time"})
    body = agent_client.post(ASK, json={"question": "Activity trend"}).json()
    assert body["interpretation"]["grain"] == "monthly"
    assert body["result"]["items"] == [
        {"period": "2026-10", "meetings": 1, "calls": 1, "followups": 1}
    ]
    assert body["summary"].endswith("1 meeting, 1 call and 1 follow-up across 1 month with data.")

    _install(monkeypatch, {"operation": "leads_by_status", "grain": "daily"})
    body = agent_client.post(ASK, json={"question": "Roughly, Leads by status"}).json()
    assert body["interpretation"]["grain"] is None


def test_unsupported_question_returns_no_result(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, {"operation": "unsupported"})

    response = agent_client.post(ASK, json={"question": "Delete every customer record"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unsupported"
    assert body["result"] is None
    assert body["summary"] == analytics_query_service.UNSUPPORTED_SUMMARY


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": "  "},
        {"question": "x" * 501},
        {"question": "Roughly, Win rate?", "organization_id": 2},
    ],
)
def test_request_validation(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    model = _install(monkeypatch, {"operation": "lead_win_rate"})

    response = agent_client.post(ASK, json=payload)

    assert response.status_code == 422
    assert model.calls == []


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        "",
        {"operation": "drop_table"},
        {"operation": "analytics_service.get_overview_metrics"},
        {"operation": "lead_win_rate", "organization_id": 999},
        {"operation": "lead_win_rate", "sql": "SELECT * FROM customers"},
        {"operation": "lead_win_rate", "start_date": "last month"},
        {"operation": "lead_win_rate", "start_date": "2027-02-29"},
        {"operation": "lead_win_rate", "grain": "yearly"},
        ["lead_win_rate"],
    ],
)
def test_malformed_model_output_is_rejected_before_execution(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch, reply: object
) -> None:
    _install(monkeypatch, reply)
    monkeypatch.setattr(
        analytics_query_service,
        "_execute",
        lambda *args: pytest.fail("Malformed plans must not be executed."),
    )

    response = agent_client.post(ASK, json={"question": "Roughly, What is our win rate?"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "malformed_ai_output"
    assert agent_client.get("/api/analytics/overview").status_code == 200


@pytest.mark.parametrize(
    ("start", "end", "valid"),
    [
        (None, None, True),
        (MIN_ANALYTICS_DATE, MAX_ANALYTICS_DATE, True),  # widest permitted range
        (MIN_ANALYTICS_DATE, None, True),
        (None, MAX_ANALYTICS_DATE, True),
        (date(2010, 1, 1), date(2026, 10, 15), True),  # long historical range
        (date(2028, 2, 29), date(2028, 2, 29), True),  # leap day, single-day range
        (date(1899, 12, 31), None, False),
        (None, date(2101, 1, 1), False),
        (None, date.max, False),
        (date.min, None, False),
        (date(2026, 2, 1), date(2026, 1, 31), False),
    ],
)
def test_date_range_rule(start: date | None, end: date | None, valid: bool) -> None:
    assert (date_range_error(start, end) is None) is valid
    if valid:
        AnalyticsDateRange(start_date=start, end_date=end)
    else:
        with pytest.raises(ValueError):
            AnalyticsDateRange(start_date=start, end_date=end)


@pytest.mark.parametrize(
    ("plan", "status"),
    [
        ({"operation": "new_customers", "end_date": "9999-12-31"}, 422),
        ({"operation": "new_customers", "start_date": "0001-01-01"}, 422),
        ({"operation": "new_customers", "start_date": "1899-12-31"}, 422),
        ({"operation": "new_customers", "start_date": "2026-03-10", "end_date": "2026-03-01"}, 422),
        ({"operation": "new_customers", "start_date": "1900-01-01", "end_date": "2100-12-31"}, 200),
        ({"operation": "new_customers", "start_date": "1900-01-01"}, 200),
        ({"operation": "new_customers", "end_date": "2100-12-31"}, 200),
        ({"operation": "new_customers", "start_date": "2010-01-01", "end_date": "2026-10-15"}, 200),
        ({"operation": "new_customers", "start_date": "2028-02-01", "end_date": "2028-02-29"}, 200),
        # An unsupported answer runs nothing, so its dates are not judged.
        ({"operation": "unsupported", "end_date": "9999-12-31"}, 200),
    ],
)
def test_question_date_ranges_are_client_errors_not_malformed_output(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch, plan: dict[str, Any], status: int
) -> None:
    _install(monkeypatch, plan)

    response = agent_client.post(ASK, json={"question": "New customers in that period?"})

    assert response.status_code == status, response.text
    if status == 422:
        error = response.json()["error"]
        assert error["code"] == "invalid_date_range"
        assert error["details"]["interpretation"]["operation"] == "new_customers"


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ("end_date=9999-12-31", 422),
        ("start_date=0001-01-01", 422),
        ("start_date=1899-12-31", 422),
        ("start_date=2026-10-02&end_date=2026-10-01", 422),
        ("start_date=1900-01-01&end_date=2100-12-31", 200),
        ("start_date=2010-01-01", 200),
        ("end_date=2100-12-31", 200),
    ],
)
def test_dashboard_endpoints_reject_out_of_range_dates_instead_of_failing(
    agent_client: TestClient, query: str, status: int
) -> None:
    for endpoint in ("overview", "customers", "enquiries", "activities", "pipeline", "followups"):
        response = agent_client.get(f"/api/analytics/{endpoint}?{query}")
        assert response.status_code == status, (endpoint, response.text)


def test_agent_analytics_tool_rejects_out_of_range_dates(
    agent_sessions: sessionmaker[Session],
) -> None:
    with agent_sessions() as db:
        tools = {
            tool.name: tool
            for tool in build_read_only_tools(db, AgentToolContext(organization_id=legacy_organization_id(db)))
        }
        with pytest.raises(ValidationError):
            tools["get_sales_analytics"].invoke({"end_date": "9999-12-31"})
        assert "overview" in json.loads(tools["get_sales_analytics"].invoke({"end_date": "2100-12-31"}))


class QuotaError(Exception):
    status_code = 429


@pytest.mark.parametrize(
    ("reply", "status", "code"),
    [
        (QuotaError("Too many requests"), 503, "llm_quota_exceeded"),
        (RuntimeError("connection refused"), 502, "llm_provider_error"),
        (TimeoutError("timed out"), 502, "llm_provider_error"),
    ],
)
def test_provider_failures_map_to_errors_and_leave_dashboards_working(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    reply: Exception,
    status: int,
    code: str,
) -> None:
    _install(monkeypatch, reply)

    response = agent_client.post(ASK, json={"question": "Roughly, What is our win rate?"})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    for endpoint in ("overview", "customers", "enquiries", "activities", "pipeline", "followups"):
        assert agent_client.get(f"/api/analytics/{endpoint}").status_code == 200


def test_unconfigured_provider_returns_503(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", None)

    response = agent_client.post(ASK, json={"question": "Roughly, What is our win rate?"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_not_configured"
    assert agent_client.get("/api/analytics/overview").status_code == 200


@pytest.mark.parametrize("token", [None, "Bearer not-a-valid-token"])
def test_unauthenticated_request_is_rejected_before_the_model(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    model = _install(monkeypatch, {"operation": "lead_win_rate"})
    agent_client.headers.pop("Authorization")
    if token:
        agent_client.headers["Authorization"] = token

    response = agent_client.post(ASK, json={"question": "Roughly, What is our win rate?"})

    assert response.status_code == 401
    assert model.calls == []


def test_rate_limit_uses_its_own_bucket(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_rate_limit_requests", 2)
    ai_rate_limiter.reset()
    model = _install(monkeypatch, {"operation": "new_customers"})

    for _ in range(2):
        assert agent_client.post(ASK, json={"question": "Roughly, New customers?"}).status_code == 200
    limited = agent_client.post(ASK, json={"question": "Roughly, New customers?"})

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert len(model.calls) == 2
    with agent_sessions() as db:
        user_id = db.scalar(select(User.user_id).where(User.email == "sales.rep@example.com"))
    # The agent chat bucket (enforce_ai_rate_limit's key) is untouched.
    ai_rate_limiter.check(f"user:{user_id}:testclient")
    ai_rate_limiter.check(f"user:{user_id}:testclient")


def test_answers_are_scoped_to_the_callers_organization_and_model_sees_no_crm_data(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with agent_sessions() as db:
        rival = Organization(name="Rival Corp", slug="rival-corp")
        db.add(rival)
        db.flush()
        company = Company(organization_id=rival.organization_id, company_name="Rival Co", industry="Mining")
        customer = Customer(company=company, customer_name="Rival Customer", status="prospect")
        db.add(
            SalesEnquiry(
                customer=customer, product="Secret product", enquiry_text="Rival lead",
                estimated_value=Decimal("9999.00"),
            )
        )
        db.commit()
    with agent_sessions() as db:
        own_value = analytics_service.get_enquiry_metrics(
            db, organization_id=legacy_organization_id(db)
        ).total_estimated_value

    model = _install(monkeypatch, {"operation": "total_lead_value"})
    body = agent_client.post(ASK, json={"question": "Roughly, Total pipeline value?"}).json()
    assert Decimal(body["result"]["value"]) == own_value != Decimal("9999.00")

    _install(monkeypatch, {"operation": "customers_by_industry"})
    body = agent_client.post(ASK, json={"question": "Customers by industry"}).json()
    assert [item["label"] for item in body["result"]["items"]] == ["Retail"]

    _install(monkeypatch, {"operation": "customer_totals"})
    body = agent_client.post(ASK, json={"question": "How many customers?"}).json()
    assert body["result"]["items"][0]["count"] == 1

    system, human = model.calls[0]
    assert human.content == "Question: Roughly, Total pipeline value?"
    shown = system.content + human.content
    for secret in ("ABC Trading", "Rival", "Secret product", "9999", "organization"):
        assert secret not in shown


@pytest.mark.skipif(
    not os.getenv("OLLAMA_TEST_MODEL"),
    reason="OLLAMA_TEST_MODEL is not set; local Ollama structured-output check skipped",
)
@pytest.mark.parametrize(
    ("question", "operations", "start", "end"),
    [
        ("What was our win rate in September 2026?", {"lead_win_rate"}, "2026-09-01", "2026-09-30"),
        ("Break down our sales leads by priority", {"leads_by_priority"}, None, None),
        ("Write me a poem about the ocean", {"unsupported"}, None, None),
        # The review's failures, with TODAY pinned to Thursday 2026-10-15.
        ("What was our win rate in March?", {"lead_win_rate"}, "2026-03-01", "2026-03-31"),
        ("Leads created last week", {"leads_created_over_time", "leads_by_status"}, "2026-10-05", "2026-10-11"),
        (
            "Leads created in the last 30 days",
            {"leads_created_over_time", "leads_by_status"},
            "2026-09-16",
            "2026-10-15",
        ),
        (
            "New customers between December 2025 and January 2026",
            {"new_customers"},
            "2025-12-01",
            "2026-01-31",
        ),
    ],
)
def test_ollama_structured_plan_integration(
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    operations: set[str],
    start: str | None,
    end: str | None,
) -> None:
    """Opt-in: OLLAMA_TEST_MODEL=qwen3.5:4b pytest tests/test_analytics_query.py -k ollama"""
    monkeypatch.setattr(settings, "ai_provider", "ollama")
    monkeypatch.setattr(settings, "ollama_model", os.environ["OLLAMA_TEST_MODEL"])
    monkeypatch.setattr(settings, "analytics_ask_timeout_seconds", 600.0)
    with agent_sessions() as db:
        response = asyncio.run(
            analytics_query_service.ask(db, question, organization_id=legacy_organization_id(db))
        )
    print(response.interpretation, response.timings_ms)
    assert response.interpretation.operation in operations
    if start:
        assert str(response.interpretation.start_date) == start
        assert str(response.interpretation.end_date) == end


@pytest.mark.parametrize(
    ("today", "period", "start", "end"),
    [
        # "Win rate in March?": the most recent March that has started.
        (date(2026, 10, 15), {"month": 3}, "2026-03-01", "2026-03-31"),
        (date(2026, 3, 1), {"month": 3}, "2026-03-01", "2026-03-31"),  # current month counts
        (date(2026, 10, 15), {"month": 11}, "2025-11-01", "2025-11-30"),  # still ahead: last year
        (date(2028, 3, 10), {"month": 2}, "2028-02-01", "2028-02-29"),  # leap year
        (date(2027, 3, 10), {"month": 2}, "2027-02-01", "2027-02-28"),
        # "Last week": the previous complete Monday-Sunday week, whatever today's weekday.
        (date(2026, 10, 15), {"last_week": True}, "2026-10-05", "2026-10-11"),  # Thursday
        (date(2026, 10, 12), {"last_week": True}, "2026-10-05", "2026-10-11"),  # Monday
        (date(2026, 10, 18), {"last_week": True}, "2026-10-05", "2026-10-11"),  # Sunday
        (date(2026, 1, 3), {"last_week": True}, "2025-12-22", "2025-12-28"),  # across years
        # "Last N days": N calendar days ending today, both inclusive.
        (date(2026, 10, 15), {"last_n_days": 30}, "2026-09-16", "2026-10-15"),
        (date(2026, 10, 15), {"last_n_days": 1}, "2026-10-15", "2026-10-15"),
        (date(2026, 1, 10), {"last_n_days": 30}, "2025-12-12", "2026-01-10"),
        # Explicit month/year and cross-year ranges pass through unchanged.
        (date(2026, 10, 15), {"start_date": "2025-03-01", "end_date": "2025-03-31"}, "2025-03-01", "2025-03-31"),
        (date(2026, 10, 15), {"start_date": "2025-12-01", "end_date": "2026-01-31"}, "2025-12-01", "2026-01-31"),
    ],
)
def test_relative_periods_resolve_on_the_server_and_are_shown_as_executed(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    today: date,
    period: dict[str, Any],
    start: str,
    end: str,
) -> None:
    monkeypatch.setattr(analytics_query_service, "_utc_today", lambda: today)
    executed: list[tuple[date | None, date | None]] = []
    real_execute = analytics_query_service._execute

    def recording_execute(db, spec, plan, organization_id, today):
        executed.append((plan.start_date, plan.end_date))
        return real_execute(db, spec, plan, organization_id, today)

    monkeypatch.setattr(analytics_query_service, "_execute", recording_execute)
    model = _install(monkeypatch, {"operation": "lead_win_rate", **period})

    body = agent_client.post(ASK, json={"question": "Win rate for that period?"}).json()

    interpretation = body["interpretation"]
    assert (interpretation["start_date"], interpretation["end_date"]) == (start, end)
    assert executed == [(date.fromisoformat(start), date.fromisoformat(end))]
    assert f"from {start} to {end}" in body["summary"]
    system_prompt = model.calls[0][0].content
    assert f"Today's date is {today.isoformat()} (UTC)" in system_prompt


def _add_leads_created_at(sessions: sessionmaker[Session], *moments: datetime) -> None:
    with sessions() as db:
        company = Company(organization_id=legacy_organization_id(db), company_name="Boundary Co")
        customer = Customer(company=company, customer_name="Boundary Customer", status="active")
        db.add_all(
            SalesEnquiry(customer=customer, enquiry_text="lead", created_at=moment, updated_at=moment)
            for moment in moments
        )
        db.commit()


@pytest.mark.parametrize(
    ("period", "inside", "outside"),
    [
        (
            {"last_n_days": 30},
            [datetime(2026, 9, 16, 0, 0), datetime(2026, 10, 15, 23, 59)],
            [datetime(2026, 9, 15, 23, 59), datetime(2026, 10, 16, 0, 0)],
        ),
        (
            {"last_week": True},
            [datetime(2026, 10, 5, 0, 0), datetime(2026, 10, 11, 23, 59)],
            [datetime(2026, 10, 4, 23, 59), datetime(2026, 10, 12, 0, 0)],
        ),
    ],
)
def test_relative_period_boundaries_match_the_records_counted(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    period: dict[str, Any],
    inside: list[datetime],
    outside: list[datetime],
) -> None:
    _add_leads_created_at(
        agent_sessions, *(moment.replace(tzinfo=timezone.utc) for moment in inside + outside)
    )
    _install(monkeypatch, {"operation": "leads_by_status", **period})

    body = agent_client.post(ASK, json={"question": "Leads created in that period"}).json()

    assert body["result"]["items"] == [{"label": "open", "count": len(inside), "value": None}]


@pytest.mark.parametrize(
    ("plan", "status", "code"),
    [
        ({"month": 3, "last_week": True}, 502, "malformed_ai_output"),
        ({"month": 3, "start_date": "2026-03-01"}, 502, "malformed_ai_output"),
        ({"last_n_days": 30, "end_date": "2026-10-15"}, 502, "malformed_ai_output"),
        ({"month": 13}, 502, "malformed_ai_output"),
        ({"last_n_days": 0}, 502, "malformed_ai_output"),
        ({"last_n_days": 100_001}, 502, "malformed_ai_output"),
        # The arithmetic is valid but the start falls before 1900-01-01.
        ({"last_n_days": 60_000}, 422, "invalid_date_range"),
    ],
)
def test_contradictory_or_impossible_relative_periods_are_rejected(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    plan: dict[str, Any],
    status: int,
    code: str,
) -> None:
    _install(monkeypatch, {"operation": "new_customers", **plan})

    response = agent_client.post(ASK, json={"question": "Roughly, New customers?"})

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_win_rate_summary_states_the_created_date_basis(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Created in August, won in September: it counts for August, not September.
    with agent_sessions() as db:
        company = Company(organization_id=legacy_organization_id(db), company_name="Cohort Co")
        customer = Customer(company=company, customer_name="Cohort Customer", status="active")
        db.add(
            SalesEnquiry(
                customer=customer, enquiry_text="deal", status="closed", outcome="won",
                closed_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
                created_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
            )
        )
        db.commit()

    _install(monkeypatch, {"operation": "lead_win_rate", "month": 9})
    september = agent_client.post(ASK, json={"question": "Roughly, Win rate in September?"}).json()
    _install(monkeypatch, {"operation": "lead_win_rate", "month": 8})
    august = agent_client.post(ASK, json={"question": "Roughly, Win rate in August?"}).json()

    assert september["result"]["value"] is None
    assert september["summary"].startswith(
        "Sales lead win rate (leads created from 2026-09-01 to 2026-09-30): unavailable."
    )
    assert august["summary"].startswith(
        "Sales lead win rate (leads created from 2026-08-01 to 2026-08-31): 100.00%."
    )
    for body in (september, august):
        assert "leads closed in this period but created earlier are not included" in body["summary"]


def test_sparse_time_series_counts_only_periods_with_data(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        {"operation": "customers_created_over_time", "start_date": START, "end_date": END},
    )

    body = agent_client.post(ASK, json={"question": "Customers created by month this year"}).json()

    assert body["summary"] == (
        "Customers created over time (created from 2026-01-01 to 2026-12-31): "
        "1 in total across 1 month with data."
    )


def test_followups_for_a_past_period_omit_counts_measured_against_today(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # agent_records has one pending follow-up due 2026-10-01, before TODAY.
    _install(monkeypatch, {"operation": "followup_summary", "month": 10})
    current = agent_client.post(ASK, json={"question": "Follow-ups this month"}).json()
    _install(
        monkeypatch,
        {"operation": "followup_summary", "start_date": "2026-09-01", "end_date": "2026-10-01"},
    )
    past = agent_client.post(ASK, json={"question": "Follow-ups in September"}).json()

    assert [item["label"] for item in current["result"]["items"]] == [
        "overdue", "due_today", "due_next_7_days", "pending", "completed", "cancelled",
    ]
    assert "measured against the current UTC date" in current["summary"]
    assert past["result"]["items"] == [
        {"label": "overdue", "count": 1, "value": None},
        {"label": "completed", "count": 0, "value": None},
        {"label": "cancelled", "count": 0, "value": None},
    ]
    assert "due_today" not in past["summary"] and "pending" not in past["summary"]
    assert "This period has ended" in past["summary"]


def test_analytics_page_serves_the_question_panel(agent_client: TestClient) -> None:
    page = agent_client.get("/analytics")
    js = agent_client.get("/static/js/analytics.js").text
    api_js = agent_client.get("/static/js/api.js").text
    css = agent_client.get("/static/css/analytics.css").text

    assert page.status_code == 200
    for element_id in (
        "ask-form", "ask-question", "ask-submit", "ask-status", "ask-error", "ask-result",
        "ask-summary", "ask-interpretation", "ask-chart",
    ):
        assert f'id="{element_id}"' in page.text
    assert 'maxlength="500"' in page.text
    assert "The date filters below do not apply here" in page.text
    assert 'request("/analytics/ask"' in api_js and 'method: "POST"' in api_js
    assert "askAnalytics(question)" in js
    # Question, summary and labels are echoed user/database text: never parsed as HTML.
    assert "innerHTML" not in js
    assert "askPending" in js and "askButton.disabled = true" in js
    assert ".ask-panel" in css
