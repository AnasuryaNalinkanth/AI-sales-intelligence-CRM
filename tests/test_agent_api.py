from collections.abc import Sequence
from typing import Any
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.langchain_compat import create_compatible_agent
from app.agent.model import create_chat_model
from app.agent.tools import AgentToolContext, build_read_only_tools
from tests.conftest import legacy_organization_id
from app.core.config import settings
from app.models import Customer
from app.schemas.agent import AgentChatRequest


class ToolCallingFakeChatModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[Any],
        **kwargs: Any,
    ) -> "ToolCallingFakeChatModel":
        _ = tools, kwargs
        return self


def _tool_outputs(db: Session) -> dict[str, Any]:
    return {tool.name: tool for tool in build_read_only_tools(db, AgentToolContext(organization_id=legacy_organization_id(db)))}


def _fake_tool_call(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "tool-call-1",
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": arguments, "id": call_id, "type": "tool_call"}
        ],
    )


def test_compatible_agent_routes_tool_calls_back_to_model() -> None:
    tool = StructuredTool.from_function(
        func=lambda value: f"read:{value}",
        name="read_value",
        description="Read a value.",
    )
    model = ToolCallingFakeChatModel(
        responses=[
            _fake_tool_call("read_value", {"value": "customer"}, call_id="read-call"),
            AIMessage(content="Customer data was read."),
        ]
    )

    agent = create_compatible_agent(model=model, tools=[tool])
    branch = agent.builder.branches["model"]["model_to_tools"]
    result = agent.invoke({"messages": [{"role": "user", "content": "Read it."}]})

    assert branch.ends["model"] == "model"
    assert result["messages"][-1].content == "Customer data was read."
    assert [message.type for message in result["messages"]].count("tool") == 1


def test_all_read_only_tools_return_structured_data(
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
) -> None:
    with agent_sessions() as db:
        tools = _tool_outputs(db)
        assert set(tools) == {
            "find_customer",
            "find_sales_leads",
            "get_customer_profile",
            "get_sales_enquiries",
            "get_customer_meetings",
            "get_customer_calls",
            "get_customer_followups",
            "get_customer_activity",
            "get_sales_analytics",
            "get_proactive_insights",
        }
        insights = json.loads(tools["get_proactive_insights"].invoke({}))
        assert isinstance(insights["data"]["items"], list)
        assert set(insights["data"]["counts_by_category"]) == {
            "overdue_followup", "stalled_deal", "neglected_lead", "needs_attention"
        }
        search = json.loads(tools["find_customer"].invoke({"search": "ABC Trading"}))
        profile = json.loads(
            tools["get_customer_profile"].invoke(
                {"customer_id": agent_records["customer_id"]}
            )
        )
        enquiries = json.loads(
            tools["get_sales_enquiries"].invoke(
                {"customer_id": agent_records["customer_id"]}
            )
        )
        meetings = json.loads(
            tools["get_customer_meetings"].invoke(
                {"customer_id": agent_records["customer_id"]}
            )
        )
        calls = json.loads(
            tools["get_customer_calls"].invoke(
                {"customer_id": agent_records["customer_id"]}
            )
        )
        followups = json.loads(
            tools["get_customer_followups"].invoke(
                {"customer_id": agent_records["customer_id"]}
            )
        )
        activity = json.loads(
            tools["get_customer_activity"].invoke(
                {
                    "customer_id": agent_records["customer_id"],
                    "activity_type": "meeting",
                    "start_date": "2026-10-01",
                    "end_date": "2026-10-01",
                }
            )
        )

        assert search["data"]["total"] == 1
        assert profile["data"]["customer"]["customer_name"] == "ABC Trading Customer"
        assert profile["data"]["company"]["company_name"] == "ABC Trading"
        assert len(enquiries["data"]["items"]) == 1
        lead = enquiries["data"]["items"][0]
        assert lead["qualification_status"] == "not_ready" and lead["outcome"] is None
        not_ready = json.loads(
            tools["find_sales_leads"].invoke({"qualification_status": "not_ready"})
        )
        assert [item["enquiry_id"] for item in not_ready["data"]["items"]] == [lead["enquiry_id"]]
        assert not_ready["data"]["items"][0]["customer_name"] == "ABC Trading Customer"
        assert json.loads(
            tools["find_sales_leads"].invoke({"qualification_status": "ready"})
        )["data"]["items"] == []
        assert len(meetings["data"]["items"]) == 1
        assert len(calls["data"]["items"]) == 1
        assert len(followups["data"]["items"]) == 1
        assert activity["data"]["items"][0]["activity_type"] == "meeting"
        assert db.query(Customer).count() == 1


def test_tool_schemas_reject_invalid_ids_and_date_ranges(
    agent_sessions: sessionmaker[Session],
) -> None:
    with agent_sessions() as db:
        tools = _tool_outputs(db)
        with pytest.raises(ValidationError):
            tools["get_customer_profile"].invoke({"customer_id": 0})
        with pytest.raises(ValidationError):
            tools["get_customer_activity"].invoke(
                {
                    "customer_id": 1,
                    "start_date": "2026-10-02",
                    "end_date": "2026-10-01",
                }
            )
        with pytest.raises(ValidationError):
            tools["get_customer_activity"].invoke(
                {"customer_id": 1, "start_date": "not-a-date"}
            )


def test_chat_request_rejects_blank_message_and_invalid_customer_id() -> None:
    with pytest.raises(ValidationError):
        AgentChatRequest(message="  ")
    with pytest.raises(ValidationError):
        AgentChatRequest(message="Find a customer", customer_id=0)


def test_chat_endpoint_uses_read_tool_and_returns_safe_metadata(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeChatModel(
        responses=[
            _fake_tool_call(
                "get_customer_profile",
                {"customer_id": agent_records["customer_id"]},
            ),
            AIMessage(content="The customer is qualified and has an open CRM enquiry."),
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    response = agent_client.post(
        "/api/agent/chat",
        json={
            "message": "Give me a basic overview of this customer.",
            "customer_id": agent_records["customer_id"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "The customer is qualified and has an open CRM enquiry."
    assert body["customer_id"] == agent_records["customer_id"]
    assert body["tool_calls"] == [
        {
            "name": "get_customer_profile",
            "input_summary": {"customer_id": agent_records["customer_id"]},
            "success": True,
            "error_code": None,
        }
    ]
    assert "prompt" not in body
    assert "api_key" not in body


def test_chat_returns_gemini_text_block_after_successful_crm_tool(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeChatModel(
        responses=[
            _fake_tool_call(
                "get_customer_profile",
                {"customer_id": agent_records["customer_id"]},
                call_id="profile-call",
            ),
            AIMessage(
                content=[
                    {
                        "type": "text",
                        "text": "This customer is qualified and has an open CRM enquiry.",
                    }
                ]
            ),
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    response = agent_client.post(
        "/api/agent/chat",
        json={
            "message": "Give me a brief overview of this customer based on the CRM data.",
            "customer_id": agent_records["customer_id"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == agent_records["customer_id"]
    assert body["response"] == "This customer is qualified and has an open CRM enquiry."
    assert body["tool_calls"][0]["name"] == "get_customer_profile"
    assert body["tool_calls"][0]["success"] is True



def test_failed_crm_read_tool_rolls_back_before_next_tool_uses_session(
    agent_client: TestClient,
    agent_records: dict[str, int],
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.agent.agent.SessionLocal",
        agent_sessions,
    )

    model = ToolCallingFakeChatModel(
        responses=[
            _fake_tool_call(
                "get_customer_profile",
                {"customer_id": agent_records["customer_id"]},
                call_id="profile-call",
            ),
            _fake_tool_call(
                "get_sales_enquiries",
                {"customer_id": agent_records["customer_id"]},
                call_id="enquiries-call",
            ),
            AIMessage(content="Customer data was retrieved."),
        ]
    )

    monkeypatch.setattr(
        "app.services.agent_service.create_chat_model",
        lambda: model,
    )

    original_scalar = Session.scalar
    original_rollback = Session.rollback
    first_read = True
    rollback_calls = 0

    def fail_first_read(
        db: Session,
        statement: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal first_read
        if first_read:
            first_read = False
            db.add(Customer(company_id=1, customer_name=None))
            db.flush()
        return original_scalar(db, statement, *args, **kwargs)

    def count_rollback(db: Session) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        original_rollback(db)

    monkeypatch.setattr(Session, "scalar", fail_first_read)
    monkeypatch.setattr(Session, "rollback", count_rollback)

    response = agent_client.post(
        "/api/agent/chat",
        json={"message": "Give me a brief overview of this customer."},
    )

    assert response.status_code == 200
    body = response.json()
    assert [call["name"] for call in body["tool_calls"]] == [
        "get_customer_profile",
        "get_sales_enquiries",
    ]
    assert body["tool_calls"][0]["success"] is False
    assert body["tool_calls"][0]["error_code"] == "database_error"
    assert body["tool_calls"][1]["success"] is True
    assert rollback_calls == 1


def test_chat_endpoint_runs_multiple_read_tools_with_same_session(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeChatModel(
        responses=[
            _fake_tool_call(
                "get_customer_profile",
                {"customer_id": agent_records["customer_id"]},
                call_id="profile-call",
            ),
            _fake_tool_call(
                "get_customer_meetings",
                {"customer_id": agent_records["customer_id"]},
                call_id="meetings-call",
            ),
            _fake_tool_call(
                "get_customer_calls",
                {"customer_id": agent_records["customer_id"]},
                call_id="calls-call",
            ),
            AIMessage(content="Customer profile, meetings, and calls are available."),
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    response = agent_client.post(
        "/api/agent/chat",
        json={
            "message": "Give me a brief overview of this customer based on CRM data.",
            "customer_id": agent_records["customer_id"],
        },
    )

    assert response.status_code == 200
    assert [call["name"] for call in response.json()["tool_calls"]] == [
        "get_customer_profile",
        "get_customer_meetings",
        "get_customer_calls",
    ]
    assert all(call["success"] for call in response.json()["tool_calls"])


def test_chat_endpoint_reports_safe_tool_failure(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeChatModel(
        responses=[
            _fake_tool_call("get_customer_profile", {"customer_id": 999}),
            AIMessage(content="Customer 999 was not found in the CRM."),
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    response = agent_client.post(
        "/api/agent/chat",
        json={"message": "Show customer 999"},
    )

    assert response.status_code == 200
    assert response.json()["tool_calls"][0]["success"] is False
    assert response.json()["tool_calls"][0]["error_code"] == "customer_not_found"


def test_chat_agent_uses_precalculated_analytics_tool(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ToolCallingFakeChatModel(
        responses=[
            _fake_tool_call(
                "get_sales_analytics",
                {"start_date": "2026-10-01", "end_date": "2026-10-31"},
            ),
            AIMessage(content="There are no CRM customers in that period."),
        ]
    )
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)

    response = agent_client.post(
        "/api/agent/chat",
        json={"message": "How many active customers do we have?"},
    )

    assert response.status_code == 200
    assert response.json()["response"] == "There are no CRM customers in that period."
    assert response.json()["tool_calls"][0]["name"] == "get_sales_analytics"
    assert response.json()["tool_calls"][0]["success"] is True


def test_chat_endpoint_rejects_missing_customer(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.agent_service.create_chat_model",
        lambda: pytest.fail("The model must not run for an unknown customer."),
    )
    response = agent_client.post(
        "/api/agent/chat",
        json={"message": "Summarize this customer", "customer_id": 999},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "customer_not_found"


def test_chat_endpoint_handles_missing_openai_configuration(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", None)

    response = agent_client.post(
        "/api/agent/chat",
        json={"message": "Find ABC Trading"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_not_configured"
    assert "API key" not in response.text


def test_chat_endpoint_rejects_malformed_request(agent_client: TestClient) -> None:
    response = agent_client.post("/api/agent/chat", json={"message": " "})
    assert response.status_code == 422


def test_chat_endpoint_safely_handles_model_failure(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenAgent:
        def invoke(self, _: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("provider response contains internal details")

    def build_broken_agent(
        _db: Session,
        *,
        model: Any,
        allow_actions: bool,
        tool_context: Any,
    ) -> BrokenAgent:
        _ = _db, model, allow_actions, tool_context
        return BrokenAgent()

    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: object())
    monkeypatch.setattr(
        "app.services.agent_action_service.build_sales_agent",
        build_broken_agent,
    )

    response = agent_client.post(
        "/api/agent/chat",
        json={"message": "Find ABC Trading"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "agent_error"
    assert "internal details" not in response.text


def test_gemini_model_factory_is_lazy_and_constructs_without_network(
    agent_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("test-key-not-used"))
    model = create_chat_model()
    assert model.model == settings.gemini_model

    with agent_sessions() as db:
        from app.agent.agent import build_sales_agent

        agent = build_sales_agent(
            db, model=model, tool_context=AgentToolContext(organization_id=1)
        )
        assert "tools" in agent.get_graph().nodes
        assert model.bind_tools([]) is not None
