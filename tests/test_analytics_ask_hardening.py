"""Regression tests for the Phase 2 review fixes: deadlines, released connections, structured-output
error classification on the real LangChain parsing path, and safe logging. No network calls:
provider transports are patched, or point at a local in-process server."""

import asyncio
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
import ollama
from pydantic import SecretStr
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.database.connection import get_db
from app.main import app
from app.services import analytics_query_service

ASK = "/api/analytics/ask"
QUESTION_MARKER = "secret-question-marker Jane Doe"
OUTPUT_MARKER = "PII-ECHO-MARKER"
DASHBOARDS = ("overview", "customers", "enquiries", "activities", "pipeline", "followups")


def _ollama_replies(
    monkeypatch: pytest.MonkeyPatch, content: str | None = None, error: Exception | None = None
) -> None:
    """Patch ChatOllama's transport (sync and async) so LangChain's own parsing still runs."""
    part = {
        "model": "test", "created_at": "2026-10-15T00:00:00Z",
        "message": {"role": "assistant", "content": content}, "done": True, "done_reason": "stop",
    }

    async def astream(self, messages, stop=None, **kwargs):
        if error:
            raise error
        yield part

    def stream(self, messages, stop=None, **kwargs):
        if error:
            raise error
        yield part

    monkeypatch.setattr(ChatOllama, "_acreate_chat_stream", astream)
    monkeypatch.setattr(ChatOllama, "_create_chat_stream", stream)
    monkeypatch.setattr(settings, "ai_provider", "ollama")


def _gemini_replies(
    monkeypatch: pytest.MonkeyPatch, content: str | None = None, error: Exception | None = None
) -> None:
    def result() -> ChatResult:
        if error:
            raise error
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    async def agenerate(self, *args, **kwargs):
        return result()

    monkeypatch.setattr(ChatGoogleGenerativeAI, "_generate", lambda self, *a, **k: result())
    monkeypatch.setattr(ChatGoogleGenerativeAI, "_agenerate", agenerate)
    monkeypatch.setattr(settings, "ai_provider", "gemini")
    # Never the developer's real key; the transport is patched anyway.
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("test-only-key"))


PROVIDERS = {"ollama": _ollama_replies, "gemini": _gemini_replies}
MALFORMED = [
    '{"operation": "drop_table"}',
    '{"operation": "lead_win_rate", "organization_id": 2}',
    "not json",
    # Before the fix these were classified as provider quota errors (503).
    '{"operation": "too many requests"}',
    f'{{"operation": "unsupported", "echo": "{OUTPUT_MARKER} rate limit resource_exhausted"}}',
]


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("content", MALFORMED)
def test_malformed_output_on_the_real_parsing_path_is_malformed_and_not_logged(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    provider: str,
    content: str,
) -> None:
    PROVIDERS[provider](monkeypatch, content)
    caplog.set_level(logging.DEBUG)

    response = agent_client.post(ASK, json={"question": QUESTION_MARKER})

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "malformed_ai_output"
    assert OUTPUT_MARKER not in caplog.text
    assert "secret-question-marker" not in caplog.text
    assert "Structured AI output failed validation" in caplog.text


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_valid_output_on_the_real_parsing_path_is_answered(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    PROVIDERS[provider](monkeypatch, '{"operation": "new_customers", "start_date": "2026-01-01"}')

    response = agent_client.post(ASK, json={"question": "Roughly, New customers this year?"})

    assert response.status_code == 200, response.text
    assert response.json()["interpretation"]["operation"] == "new_customers"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ollama.ResponseError("model is overloaded", 429), 503, "llm_quota_exceeded"),
        (httpx.ConnectError("connection refused"), 502, "llm_provider_error"),
        (ollama.ResponseError("model not found", 404), 502, "llm_provider_error"),
    ],
)
def test_genuine_provider_errors_keep_their_classification(
    agent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    code: str,
) -> None:
    _ollama_replies(monkeypatch, error=error)

    response = agent_client.post(ASK, json={"question": "Roughly, New customers this year?"})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    for endpoint in DASHBOARDS:
        assert agent_client.get(f"/api/analytics/{endpoint}").status_code == 200


def test_gemini_quota_error_is_still_a_quota_error(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ResourceExhausted(Exception):
        pass

    _gemini_replies(monkeypatch, error=ResourceExhausted("429 RESOURCE_EXHAUSTED"))

    response = agent_client.post(ASK, json={"question": "Roughly, New customers this year?"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_quota_exceeded"


@pytest.mark.parametrize(
    ("content", "status", "code"),
    [
        ('{"summary": "too many requests"}', 502, "malformed_ai_output"),
        ("not json", 502, "malformed_ai_output"),
        (
            '{"summary": "Active buyer.", "key_points": [], "customer_concerns": [], '
            '"recommended_next_action": "Send the proposal."}',
            200,
            None,
        ),
    ],
)
def test_customer_summary_shares_the_classification_on_the_sync_path(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    status: int,
    code: str | None,
) -> None:
    _ollama_replies(monkeypatch, content)

    response = agent_client.post(f"/api/agent/customer-summary/{agent_records['customer_id']}")

    assert response.status_code == status, response.text
    if code:
        assert response.json()["error"]["code"] == code


class SlowModel:
    def __init__(self) -> None:
        self.cancelled = False

    async def ainvoke(self, messages: Any) -> AIMessage:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return AIMessage(content='{"operation": "new_customers"}')


def test_overall_deadline_returns_504_and_cancels_the_model_call(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = SlowModel()
    monkeypatch.setattr(analytics_query_service, "create_chat_model", lambda **_: model)
    monkeypatch.setattr(settings, "analytics_ask_timeout_seconds", 0.2)

    started = time.perf_counter()
    response = agent_client.post(ASK, json={"question": "Roughly, New customers this year?"})

    assert time.perf_counter() - started < 5
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "llm_timeout"
    assert model.cancelled
    for endpoint in DASHBOARDS:
        assert agent_client.get(f"/api/analytics/{endpoint}").status_code == 200


class _EndlessOllama(BaseHTTPRequestHandler):
    """Streams Ollama chat chunks forever, like a model that never stops thinking."""

    disconnected = threading.Event()
    stop = threading.Event()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        chunk = {
            "model": "test", "created_at": "2026-10-15T00:00:00Z",
            "message": {"role": "assistant", "content": " "}, "done": False,
        }
        line = json.dumps(chunk).encode() + b"\n"
        try:
            while not self.stop.is_set():
                self.wfile.write(line)
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            type(self).disconnected.set()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def endless_ollama() -> Generator[str, None, None]:
    _EndlessOllama.disconnected = threading.Event()
    _EndlessOllama.stop = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EndlessOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    _EndlessOllama.stop.set()
    server.shutdown()
    server.server_close()


def test_deadline_closes_a_streaming_ollama_connection(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch, endless_ollama: str
) -> None:
    # Every chunk arrives well inside the 120 s per-read timeout, so only the overall
    # deadline can stop this request.
    monkeypatch.setattr(settings, "ai_provider", "ollama")
    monkeypatch.setattr(settings, "ollama_base_url", endless_ollama)
    monkeypatch.setattr(settings, "analytics_ask_timeout_seconds", 1.0)

    response = agent_client.post(ASK, json={"question": "Roughly, New customers this year?"})

    assert response.status_code == 504
    assert _EndlessOllama.disconnected.wait(5), "the provider connection was left open"


def test_no_transaction_is_held_during_inference_and_execution_leaves_the_event_loop(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[Session] = []

    def tracking_db() -> Generator[Session, None, None]:
        with agent_sessions() as db:
            sessions.append(db)
            yield db

    app.dependency_overrides[get_db] = tracking_db
    in_transaction: list[bool] = []

    class ProbeModel:
        async def ainvoke(self, messages: Any) -> AIMessage:
            in_transaction.append(sessions[0].in_transaction())
            return AIMessage(content='{"operation": "customer_totals"}')

    off_loop: list[bool] = []
    real_execute = analytics_query_service._execute

    def probe_execute(*args: Any):
        try:
            asyncio.get_running_loop()
            off_loop.append(False)
        except RuntimeError:
            off_loop.append(True)
        return real_execute(*args)

    monkeypatch.setattr(analytics_query_service, "create_chat_model", lambda **_: ProbeModel())
    monkeypatch.setattr(analytics_query_service, "_execute", probe_execute)

    response = agent_client.post(ASK, json={"question": "How many customers?"})

    assert response.status_code == 200, response.text
    assert in_transaction == [False]
    assert off_loop == [True]
    # The session reconnected and stayed scoped to the caller's organization.
    assert response.json()["result"]["items"][0] == {
        "label": "total_customers", "count": 1, "value": None,
    }
