from typing import Any

import pytest
from langchain_core.tools import tool
from pydantic import ValidationError

from app.agent.langchain_compat import create_compatible_agent
from app.agent.model import create_chat_model
from app.core.config import Settings, settings
from app.core.exceptions import APIError


def test_gemini_remains_default_provider() -> None:
    assert Settings.model_fields["ai_provider"].default == "gemini"
    assert Settings.model_fields["ollama_base_url"].default == "http://localhost:11434"
    assert Settings.model_fields["ollama_model"].default == "qwen3.5:4b"
    assert Settings.model_fields["ollama_num_ctx"].default == 16384


def test_settings_normalize_and_reject_providers() -> None:
    assert Settings(_env_file=None, ai_provider="  OLLAMA ").ai_provider == "ollama"
    with pytest.raises(ValidationError, match="ai_provider"):
        Settings(_env_file=None, ai_provider="openai")
    with pytest.raises(ValidationError, match="ollama_timeout_seconds"):
        Settings(_env_file=None, ollama_timeout_seconds=0)
    with pytest.raises(ValidationError, match="ollama_num_ctx"):
        Settings(_env_file=None, ollama_num_ctx=0)


def test_unsupported_provider_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_provider", "openai")
    with pytest.raises(APIError) as exc_info:
        create_chat_model()
    assert exc_info.value.code == "llm_not_configured"
    assert exc_info.value.status_code == 503


def test_gemini_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", None)
    with pytest.raises(APIError) as exc_info:
        create_chat_model()
    assert exc_info.value.code == "llm_not_configured"


def test_ollama_provider_constructor_args_without_gemini_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_provider", "ollama")
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama.test:11434")
    monkeypatch.setattr(settings, "ollama_model", "qwen-test")
    monkeypatch.setattr(settings, "ollama_timeout_seconds", 45.0)
    monkeypatch.setattr(settings, "ollama_num_ctx", 8192)
    captured_kwargs: dict[str, Any] = {}

    class DummyChatOllama:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)

    monkeypatch.setattr("langchain_ollama.ChatOllama", DummyChatOllama)
    create_chat_model()

    assert captured_kwargs == {
        "model": "qwen-test",
        "base_url": "http://ollama.test:11434",
        "num_ctx": 8192,
        "reasoning": None,
        "num_predict": None,
        "client_kwargs": {"timeout": 45.0},
    }


def test_ollama_model_runs_agent_tool_loop_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_provider", "ollama")
    model = create_chat_model()
    requests: list[dict[str, Any]] = []
    replies = [
        {"tool_calls": [{"function": {"name": "count_customers", "arguments": {"status": "active"}}}]},
        {"content": "You have 3 active customers."},
    ]

    def fake_chat(**params: Any):
        requests.append(params)
        message = {"role": "assistant", "content": "", **replies[len(requests) - 1]}
        response = {"model": "qwen-test", "message": message, "done": True, "done_reason": "stop"}
        return iter([response]) if params.get("stream") else response

    monkeypatch.setattr(model._client, "chat", fake_chat)

    @tool
    def count_customers(status: str) -> int:
        """Count CRM customers with a status."""
        assert status == "active"
        return 3

    agent = create_compatible_agent(model=model, tools=[count_customers], system_prompt="CRM")
    result = agent.invoke({"messages": [("user", "How many active customers?")]})

    assert requests[0]["tools"][0]["function"]["name"] == "count_customers"
    assert result["messages"][1].tool_calls[0]["args"] == {"status": "active"}
    assert result["messages"][2].content == "3"
    assert result["messages"][-1].content == "You have 3 active customers."
