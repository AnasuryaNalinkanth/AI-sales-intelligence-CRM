import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage
from sqlalchemy.orm import Session, sessionmaker

from app.agent.tools import AgentToolContext, build_read_only_tools
from app.core.config import settings
from app.models import (
    AgentConversationMessage,
    Company,
    Customer,
    HitlActionProposal,
    Meeting,
)
from app.services.agent_action_service import _NO_CHANGE_MESSAGE
from tests.conftest import authorize_test_client, legacy_organization_id, legacy_organization_id
from tests.test_agent_actions import ToolCallingFakeChatModel, _count, _tool_call


class RecordingModel(ToolCallingFakeChatModel):
    """Fake Gemini that records the conversation it was given on each call."""

    seen: list[list[BaseMessage]] = []

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Any:
        type(self).seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _model(monkeypatch: pytest.MonkeyPatch, *responses: AIMessage) -> type[RecordingModel]:
    RecordingModel.seen = []
    model = RecordingModel(responses=list(responses))
    monkeypatch.setattr("app.services.agent_service.create_chat_model", lambda: model)
    return RecordingModel


def _chat(
    client: TestClient,
    customer_id: int,
    message: str,
    conversation_id: str | None = None,
) -> Any:
    body: dict[str, Any] = {"message": message, "customer_id": customer_id}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return client.post("/api/agent/chat", json=body)


def _texts(messages: list[BaseMessage]) -> list[str]:
    return [str(message.content) for message in messages if message.type in {"human", "ai"}]


@pytest.fixture
def second_customer_id(agent_sessions: sessionmaker[Session]) -> int:
    with agent_sessions() as db:
        company = Company(organization_id=legacy_organization_id(db), company_name="Other Co")
        db.add(company)
        db.flush()
        customer = Customer(company=company, customer_name="Other Customer", status="active")
        db.add(customer)
        db.commit()
        return customer.customer_id


@pytest.fixture(autouse=True)
def _limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "agent_recursion_limit", 12)
    monkeypatch.setattr(settings, "agent_history_max_messages", 12)


def test_follow_up_question_uses_earlier_turn_and_survives_refresh(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, AIMessage(content="The primary contact is Alex Smith."))
    first = _chat(agent_client, customer_id, "Who is the primary contact?")
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]
    assert conversation_id

    # A page refresh loses browser state; the server restores the conversation.
    restored = agent_client.get(
        "/api/agent/conversations/latest", params={"customer_id": customer_id}
    ).json()
    assert restored["conversation_id"] == conversation_id
    assert [(m["role"], m["content"]) for m in restored["messages"]] == [
        ("user", "Who is the primary contact?"),
        ("assistant", "The primary contact is Alex Smith."),
    ]

    seen = _model(monkeypatch, AIMessage(content="Their email is alex@example.com."))
    second = _chat(agent_client, customer_id, "What is their email?", conversation_id)
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id
    texts = _texts(seen.seen[0])
    assert texts[:2] == ["Who is the primary contact?", "The primary contact is Alex Smith."]
    assert texts[2].endswith("User request: What is their email?")


def test_two_conversations_stay_isolated(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, AIMessage(content="Noted alpha."))
    alpha = _chat(agent_client, customer_id, "Topic alpha").json()["conversation_id"]
    _model(monkeypatch, AIMessage(content="Noted beta."))
    beta = _chat(agent_client, customer_id, "Topic beta").json()["conversation_id"]
    assert alpha != beta

    seen = _model(monkeypatch, AIMessage(content="Still alpha."))
    assert _chat(agent_client, customer_id, "Continue", alpha).status_code == 200
    texts = " ".join(_texts(seen.seen[0]))
    assert "Topic alpha" in texts and "Topic beta" not in texts


def test_customer_switch_and_other_users_cannot_reach_a_conversation(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    second_customer_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, AIMessage(content="ABC prefers Tuesdays."))
    conversation_id = _chat(agent_client, customer_id, "Note ABC's preference").json()[
        "conversation_id"
    ]

    # Reusing customer 1's conversation while customer 2 is selected is refused before the model runs.
    seen = _model(monkeypatch, AIMessage(content="unused"))
    switched = _chat(agent_client, second_customer_id, "And this one?", conversation_id)
    assert switched.status_code == 404
    assert switched.json()["error"]["code"] == "conversation_not_found"
    assert seen.seen == []
    assert agent_client.get(
        "/api/agent/conversations/latest", params={"customer_id": second_customer_id}
    ).json() is None

    # A fresh chat for customer 2 carries none of customer 1's history.
    seen = _model(monkeypatch, AIMessage(content="No history here."))
    assert _chat(agent_client, second_customer_id, "What do you know?").status_code == 200
    assert "ABC prefers Tuesdays." not in " ".join(_texts(seen.seen[0]))

    # Another user gets the same 404 for the ID and sees no history.
    authorize_test_client(agent_sessions, agent_client, email="other.rep@example.com")
    assert _chat(agent_client, customer_id, "Hi", conversation_id).status_code == 404
    assert agent_client.get(
        "/api/agent/conversations/latest", params={"customer_id": customer_id}
    ).json() is None


def test_invalid_and_unknown_conversation_ids_are_rejected(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _model(monkeypatch, AIMessage(content="unused"))
    customer_id = agent_records["customer_id"]
    assert _chat(agent_client, customer_id, "Hi", "not-a-uuid").status_code == 422
    unknown = _chat(agent_client, customer_id, "Hi", str(uuid4()))
    assert unknown.status_code == 404
    assert seen.seen == []


def test_history_sent_to_the_model_is_bounded(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    monkeypatch.setattr(settings, "agent_history_max_messages", 2)
    _model(monkeypatch, AIMessage(content="first reply"))
    conversation_id = _chat(agent_client, customer_id, "first").json()["conversation_id"]
    _model(monkeypatch, AIMessage(content="x" * 5000))
    _chat(agent_client, customer_id, "second", conversation_id)

    seen = _model(monkeypatch, AIMessage(content="third reply"))
    _chat(agent_client, customer_id, "third", conversation_id)
    texts = _texts(seen.seen[0])
    assert texts[0] == "second"
    assert texts[1] == "x" * 2000
    assert "first" not in texts


def test_unconfirmed_change_claim_is_replaced_and_not_stored(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_meetings = _count(agent_sessions, Meeting)
    _model(monkeypatch, AIMessage(content="I've scheduled the meeting for Friday at 3 PM."))
    response = _chat(agent_client, agent_records["customer_id"], "Book Friday 3 PM")

    assert response.status_code == 200
    assert response.json()["response"] == _NO_CHANGE_MESSAGE
    assert response.json()["pending_action"] is None
    assert _count(agent_sessions, Meeting) == before_meetings
    assert _count(agent_sessions, HitlActionProposal) == 0
    with agent_sessions() as db:
        stored = [row.content for row in db.query(AgentConversationMessage).all()]
    assert "I've scheduled the meeting for Friday at 3 PM." not in stored


def test_pending_proposal_is_never_reported_as_completed(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    before_meetings = _count(agent_sessions, Meeting)
    _model(
        monkeypatch,
        _tool_call(
            "create_meeting",
            {
                "customer_id": customer_id,
                "scheduled_date": "2030-10-08",
                "scheduled_time": "15:00",
                "scheduled_timezone": "UTC",
            },
        ),
        AIMessage(content="Done! The meeting has been saved."),
    )
    body = _chat(agent_client, customer_id, "Schedule a meeting at 15:00").json()
    assert body["pending_action"]["action"] == "create_meeting"
    assert body["response"].endswith("Please confirm.")
    assert "saved" not in body["response"]
    assert _count(agent_sessions, Meeting) == before_meetings


def test_conflict_reply_lists_only_backend_alternatives(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    busy = datetime(2030, 10, 14, 12, 0, tzinfo=timezone.utc)
    with agent_sessions() as db:
        db.add(Meeting(customer_id=customer_id, scheduled_at=busy.replace(tzinfo=None), duration=60, status="scheduled"))
        db.commit()
    _model(
        monkeypatch,
        _tool_call(
            "schedule_call",
            {"customer_id": customer_id, "scheduled_at": busy.isoformat(), "scheduled_timezone": "UTC", "duration": 30},
        ),
        AIMessage(content="That slot is taken. How about 03:17 or 23:59 instead?"),
    )
    body = _chat(agent_client, customer_id, "Call at noon").json()

    assert "03:17" not in body["response"] and "23:59" not in body["response"]
    assert body["response"].startswith("That time overlaps an existing scheduled meeting.")
    alternatives = body["schedule_alternatives"]
    assert alternatives
    for slot in alternatives:
        start = datetime.fromisoformat(slot["starts_at"])
        assert start >= busy.replace(hour=13) or start.timestamp() + 1800 <= busy.timestamp()
    assert _count(agent_sessions, HitlActionProposal) == 0


def test_read_tools_mark_missing_data_timing_and_repeat_calls(
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    second_customer_id: int,
) -> None:
    customer_id = agent_records["customer_id"]
    with agent_sessions() as db:
        db.add(
            Meeting(
                customer_id=customer_id,
                scheduled_at=datetime(2026, 9, 1, 9, 0),
                status="completed",
            )
        )
        db.commit()
    with agent_sessions() as db:
        context = AgentToolContext(organization_id=legacy_organization_id(db))
    tools = {
        tool.name: tool
        for tool in build_read_only_tools(context=context, session_factory=agent_sessions)
    }

    empty = json.loads(tools["get_customer_meetings"].invoke({"customer_id": second_customer_id}))
    assert empty["data"]["items"] == []
    assert empty["data"]["note"] == "No matching CRM records exist."

    meetings = json.loads(tools["get_customer_meetings"].invoke({"customer_id": customer_id}))
    timing = {item["scheduled_at"][:10]: item["timing"] for item in meetings["data"]["items"]}
    # The frozen clock is 2026-10-01 00:00 UTC.
    assert timing == {"2026-09-01": "past", "2026-10-01": "upcoming"}

    repeated = json.loads(tools["get_customer_meetings"].invoke({"customer_id": customer_id}))
    assert "data" not in repeated and "reuse that result" in repeated["note"]


def test_switched_customer_gets_no_other_customer_memory_and_is_told_so(
    agent_client: TestClient,
    agent_records: dict[str, int],
    second_customer_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reported scenario: a code given to customer A, then asked for from customer B.
    customer_a = agent_records["customer_id"]
    _model(monkeypatch, AIMessage(content="Noted."))
    conversation_a = _chat(
        agent_client, customer_a, "Remember this test code: ORBIT-482."
    ).json()["conversation_id"]

    seen = _model(monkeypatch, AIMessage(content="This conversation does not include a test code."))
    asked = _chat(agent_client, second_customer_id, "What test code did I give you earlier?")
    assert asked.status_code == 200
    assert asked.json()["conversation_id"] != conversation_a
    # Everything Gemini received, including the system prompt: no trace of customer A.
    sent = seen.seen[0]
    assert "ORBIT-482" not in " ".join(str(message.content) for message in sent)
    turns = _texts(sent)
    assert len(turns) == 1
    assert "Earlier messages in this conversation: none." in turns[0]

    # Customer B's own follow-up sees exactly B's earlier turn, and the count is stated.
    seen = _model(monkeypatch, AIMessage(content="Still none."))
    _chat(agent_client, second_customer_id, "Anything else?", asked.json()["conversation_id"])
    turns = _texts(seen.seen[0])
    assert turns[:2] == [
        "What test code did I give you earlier?",
        "This conversation does not include a test code.",
    ]
    assert "Earlier messages in this conversation: 2 (shown above)." in turns[2]

    # Customer A's history is still restored for A, and only for A.
    restored_a = agent_client.get(
        "/api/agent/conversations/latest", params={"customer_id": customer_a}
    ).json()
    assert restored_a["conversation_id"] == conversation_a
    assert restored_a["messages"][0]["content"] == "Remember this test code: ORBIT-482."
    restored_b = agent_client.get(
        "/api/agent/conversations/latest", params={"customer_id": second_customer_id}
    ).json()
    assert all("ORBIT-482" not in m["content"] for m in restored_b["messages"])


def _seed_conversation(
    sessions: sessionmaker[Session],
    customer_id: int,
    messages: list[tuple[str, str]],
) -> str:
    from app.models import AgentConversation, User

    with sessions() as db:
        owner = db.query(User).filter_by(email="sales.rep@example.com").one()
        conversation = AgentConversation(
            conversation_id=str(uuid4()), owner_user_id=owner.user_id, customer_id=customer_id
        )
        db.add(conversation)
        db.flush()
        for role, content in messages:
            db.add(
                AgentConversationMessage(
                    conversation_id=conversation.conversation_id, role=role, content=content
                )
            )
        db.commit()
        return conversation.conversation_id


def test_fabricated_code_in_stored_assistant_reply_is_not_accepted_as_memory(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    second_customer_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live database state: customer B's conversation holds an earlier invented reply.
    question = "What test code did I give you earlier?"
    conversation_id = _seed_conversation(
        agent_sessions,
        second_customer_id,
        [("user", question), ("assistant", "You gave me the test code ALPHA-9981.")],
    )
    seen = _model(
        monkeypatch,
        AIMessage(
            content="Based on our earlier messages in this conversation, "
            "you gave me the test code ALPHA-9981."
        ),
    )

    body = _chat(agent_client, second_customer_id, question, conversation_id).json()

    # The real request-building path sent exactly B's two stored messages, and said so.
    turns = _texts(seen.seen[0])
    assert turns[:2] == [question, "You gave me the test code ALPHA-9981."]
    assert "Earlier messages in this conversation: 2 (shown above)." in turns[2]
    # The user never gave that code, so the answer is replaced and the replacement is stored.
    assert body["response"] == "I can't find a test code in this conversation."
    with agent_sessions() as db:
        latest = (
            db.query(AgentConversationMessage)
            .order_by(AgentConversationMessage.message_id.desc())
            .first()
        )
        assert latest.content == "I can't find a test code in this conversation."


def test_recall_answer_is_kept_when_the_user_gave_the_value(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, AIMessage(content="Noted."))
    conversation_id = _chat(
        agent_client, customer_id, "Remember this test code: ORBIT-482."
    ).json()["conversation_id"]

    _model(monkeypatch, AIMessage(content="You gave me the test code ORBIT-482."))
    body = _chat(
        agent_client, customer_id, "What test code did I give you earlier?", conversation_id
    ).json()
    assert body["response"] == "You gave me the test code ORBIT-482."


def test_recall_answer_invented_on_a_new_conversation_is_replaced(
    agent_client: TestClient,
    second_customer_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _model(monkeypatch, AIMessage(content="You gave me the test code ALPHA-9981."))
    body = _chat(agent_client, second_customer_id, "What test code did I give you earlier?").json()
    assert "Earlier messages in this conversation: none." in _texts(seen.seen[0])[0]
    assert body["response"] == "I can't find a test code in this conversation."


RECALL = "What test code did I give you earlier?"
CANT_FIND = "I can't find a test code in this conversation."


@pytest.mark.parametrize(
    ("history", "answer", "expected"),
    [
        # The live case: two codes given by the user; recalling the latest is legitimate.
        (
            [
                ("user", "Remember this test code: ORBIT-482."),
                ("assistant", "Noted."),
                ("user", "Remember this test code: NEBULA-7316. Do not create records."),
                ("assistant", "Noted."),
            ],
            "Based on the earlier messages in this conversation, the test code you gave me is NEBULA-7316.",
            None,
        ),
        # Present only in an assistant reply: model output is not proof.
        (
            [("user", RECALL), ("assistant", "You gave me ALPHA-9981.")],
            "You gave me the test code ALPHA-9981.",
            CANT_FIND,
        ),
        # Gemini invents a different code than the one the user gave.
        ([("user", "Remember this test code: NEBULA-7316.")], "Your code is NEBULA-7317.", CANT_FIND),
        # A truncated code is not accepted as a substring of the real one.
        ([("user", "Remember this test code: NEBULA-7316.")], "Your code is NEBULA-731.", CANT_FIND),
        # Never provided: an honest answer stands, an invented one is replaced.
        ([("user", "Hello")], CANT_FIND, None),
        ([("user", "Hello")], "You gave me ZETA-12.", CANT_FIND),
    ],
)
def test_recall_guard_accepts_only_whole_values_the_user_wrote(
    history: list[tuple[str, str]],
    answer: str,
    expected: str | None,
) -> None:
    from app.services.agent_conversation_service import unverified_recall_reply

    messages = [{"role": role, "content": content} for role, content in history]
    assert unverified_recall_reply(RECALL, answer, messages) == expected


def test_recall_guard_does_not_treat_the_question_as_proof() -> None:
    from app.services.agent_conversation_service import unverified_recall_reply

    assert (
        unverified_recall_reply("Did I give you the test code ZETA-12?", "Yes, ZETA-12.", [])
        == CANT_FIND
    )


def test_code_given_only_to_another_customer_is_not_recalled(
    agent_client: TestClient,
    agent_records: dict[str, int],
    second_customer_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(monkeypatch, AIMessage(content="Noted."))
    _chat(agent_client, agent_records["customer_id"], "Remember this test code: NEBULA-7316.")

    # Even if Gemini produced customer A's code for customer B, it is not in B's messages.
    seen = _model(monkeypatch, AIMessage(content="You gave me the test code NEBULA-7316."))
    body = _chat(agent_client, second_customer_id, RECALL).json()
    assert "NEBULA-7316" not in " ".join(str(m.content) for m in seen.seen[0])
    assert body["response"] == CANT_FIND


# The frozen clock is 2026-10-01 00:00 UTC, so "today" is 2026-10-01.
def _book(customer_id: int, day: str, at: str | None) -> AIMessage:
    args: dict[str, Any] = {
        "customer_id": customer_id,
        "scheduled_date": day,
        "scheduled_timezone": "UTC",
    }
    if at is not None:
        args["scheduled_time"] = at
    return _tool_call("create_meeting", args, f"book-{day}-{at}")


def _chat_tz(client: TestClient, customer_id: int, message: str, conversation_id: str | None = None) -> Any:
    body: dict[str, Any] = {"message": message, "customer_id": customer_id, "timezone_name": "UTC"}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = client.post("/api/agent/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_ambiguous_time_today_asks_am_or_pm_before_any_proposal(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    # Gemini guessed PM; neither 05:45 nor 17:45 is inside 09:00-17:00 business hours.
    _model(monkeypatch, _book(customer_id, "2026-10-01", "17:45"))
    body = _chat_tz(agent_client, customer_id, "Book a meeting today at 5:45.")

    assert body["response"] == "Do you mean 5:45 AM or 5:45 PM?"
    assert body["pending_action"] is None
    assert "scheduled_time" not in body["clarification_task"]["operations"][0]["values"]
    assert _count(agent_sessions, HitlActionProposal) == 0

    # Stated explicitly, the same time is proposed with its full date, time and timezone.
    _model(monkeypatch, _book(customer_id, "2026-10-01", "17:45"))
    body = _chat_tz(agent_client, customer_id, "Book a meeting today at 5:45 PM.")
    assert body["response"] == (
        "I am ready to create meeting for 2026-10-01 17:45 UTC (UTC+00:00). Please confirm."
    )


def test_time_from_an_earlier_request_is_not_reused_then_follow_up_time_is(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    meetings_before = _count(agent_sessions, Meeting)
    _model(monkeypatch, _book(customer_id, "2026-10-01", "17:45"))
    first = _chat_tz(agent_client, customer_id, "Book a meeting for me today 5:45 pm")
    conversation_id = first["conversation_id"]
    confirmed = agent_client.post(f"/api/agent/actions/{first['pending_action']['action_id']}/confirm")
    assert confirmed.json()["status"] == "completed"

    # The reported bug: Gemini copies 17:45 from the earlier turn into "tomorrow".
    seen = _model(monkeypatch, _book(customer_id, "2026-10-02", "17:45"))
    second = _chat_tz(agent_client, customer_id, "Book a meeting for me tomorrow", conversation_id)
    assert "17:45" in " ".join(_texts(seen.seen[0]))  # It was available in history.
    assert second["response"] == "What time would you like to schedule the meeting?"
    assert second["pending_action"] is None
    values = second["clarification_task"]["operations"][0]["values"]
    assert values["scheduled_date"] == "2026-10-02" and "scheduled_time" not in values
    assert _count(agent_sessions, HitlActionProposal) == 1  # Only the confirmed first one.
    assert _count(agent_sessions, Meeting) == meetings_before + 1

    # The user answers in chat; the date comes from the earlier turn, the time from this one.
    _model(monkeypatch, _book(customer_id, "2026-10-02", "10:30"))
    third = _chat_tz(agent_client, customer_id, "At 10:30 AM please", conversation_id)
    assert third["response"] == (
        "I am ready to create meeting for 2026-10-02 10:30 UTC (UTC+00:00). Please confirm."
    )
    assert _count(agent_sessions, Meeting) == meetings_before + 1  # Still awaiting approval.


@pytest.mark.parametrize("model_time", [None, "09:00"])
def test_new_conversation_without_a_time_asks_for_one(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    model_time: str | None,
) -> None:
    # Whether Gemini omits the time or invents one, the user is asked.
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "2026-10-02", model_time))
    body = _chat_tz(agent_client, customer_id, "Book a meeting tomorrow.")
    assert body["response"] == "What time would you like to schedule the meeting?"
    assert body["pending_action"] is None
    assert _count(agent_sessions, HitlActionProposal) == 0


@pytest.mark.parametrize(
    ("message", "proposed", "expected"),
    [
        ("Book a meeting today at 5:45.", "17:45", (None, "Do you mean 5:45 AM or 5:45 PM?")),
        ("Book a meeting for me tomorrow", "17:45", (None, None)),
        ("Book a meeting at 5:45 PM", "17:45", ("17:45", None)),
        ("Book a meeting at 3:30", "03:30", ("15:30", None)),  # Only 15:30 is in business hours.
        ("Book a meeting at 9:30", "09:30", ("09:30", None)),
        ("Book a meeting at 17:45", "17:45", ("17:45", None)),
        ("Book a meeting at 05:45", "05:45", ("05:45", None)),  # Zero-padded: 24-hour.
        ("Book a meeting at 3pm", "15:00", ("15:00", None)),
        ("Book a meeting at noon", "12:00", ("12:00", None)),
        ("Schedule a meeting at 2026-10-20T16:00:00+04:00", "16:00", ("16:00", None)),
        ("Book a meeting on 2030-10-08", "15:00", (None, None)),  # A date is not a time.
        ("Book a meeting in 2 hours", "15:00", (None, None)),
        ("Book a meeting at 4pm", "17:45", (None, None)),  # Model time contradicts the message.
    ],
)
def test_meeting_time_must_be_stated_in_the_current_message(
    message: str,
    proposed: str,
    expected: tuple[str | None, str | None],
) -> None:
    from datetime import time

    from app.agent.tools.time_grounding import ground_meeting_time

    resolved, question = ground_meeting_time(message, time.fromisoformat(proposed))
    assert (resolved.strftime("%H:%M") if resolved else None, question) == expected


# Answers the model gives after reading CRM records in the same request.
CRM_GROUNDED_ANSWERS = [
    (
        "get_customer_meetings",
        "What was my last meeting with Contoso?",
        "Your last meeting with Contoso was on 12 October 2026 at 10:00.",
    ),
    (
        "get_customer_followups",
        "What is my next follow-up?",
        "Your next follow-up is due 14 Oct 2026.",
    ),
    (
        "get_customer_profile",
        "Do you remember the open enquiries for this customer?",
        "Enquiry #6 is open, valued at 25000.",
    ),
    (
        "get_customer_meetings",
        "Which meetings are scheduled with Contoso?",
        "Here are the meetings we have scheduled with Contoso: 14 Oct 10:00 (Demo).",
    ),
    (
        "get_customer_meetings",
        "When is the Contoso demo?",
        "Your demo with Contoso has been booked for 14 Oct at 10:00 (status: scheduled).",
    ),
]


@pytest.mark.parametrize(("tool", "question", "answer"), CRM_GROUNDED_ANSWERS)
def test_answers_from_crm_lookups_are_kept(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    question: str,
    answer: str,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(
        monkeypatch,
        _tool_call(tool, {"customer_id": customer_id}, "read"),
        AIMessage(content=answer),
    )
    body = _chat(agent_client, customer_id, question).json()

    assert [call["name"] for call in body["tool_calls"]] == [tool]
    assert body["tool_calls"][0]["success"] is True
    assert body["response"] == answer
    assert body["pending_action"] is None
    assert _count(agent_sessions, HitlActionProposal) == 0


def test_empty_answer_never_returns_an_earlier_reply_from_history(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    stale = "I am ready to create meeting for 2026-10-15 21:00 Asia/Dubai (UTC+04:00). Please confirm."
    _model(monkeypatch, AIMessage(content=stale))
    conversation_id = _chat(agent_client, customer_id, "Hello").json()["conversation_id"]

    # A truncated local model can end the run with empty content after a read tool.
    _model(
        monkeypatch,
        _tool_call("get_customer_profile", {"customer_id": customer_id}, "read"),
        AIMessage(content=""),
    )
    body = _chat(agent_client, customer_id, "Summarize this customer", conversation_id).json()

    assert body["response"] == "I could not complete that request."
    assert body["pending_action"] is None
    assert body["clarification_task"] is None
    assert _count(agent_sessions, HitlActionProposal) == 0
    with agent_sessions() as db:
        stored = [row.content for row in db.query(AgentConversationMessage).all()]
    assert stored.count(stale) == 1


@pytest.mark.parametrize(
    "answer",
    [
        "Here are the meetings we have scheduled with Contoso: 14 Oct 10:00 (Demo).",
        "Your demo with Contoso has been booked for 14 Oct at 10:00 (status: scheduled).",
    ],
)
def test_descriptions_of_existing_records_are_not_change_claims(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    _model(monkeypatch, AIMessage(content=answer))
    body = _chat(agent_client, agent_records["customer_id"], "Summarise the schedule.").json()
    assert body["response"] == answer


def test_personal_recall_without_a_crm_lookup_is_still_replaced(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(
        monkeypatch,
        AIMessage(content="Your last meeting with Contoso was on 12 October 2026 at 10:00."),
    )
    body = _chat(
        agent_client, agent_records["customer_id"], "What was my last meeting with Contoso?"
    ).json()
    assert body["tool_calls"] == []
    assert body["response"] == "I can't find that information in this conversation."


def test_failed_lookup_does_not_ground_a_recall_answer(
    agent_client: TestClient,
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(
        monkeypatch,
        _tool_call("get_customer_meetings", {"customer_id": 999999}, "read"),
        AIMessage(content="You gave me the test code ALPHA-9981."),
    )
    body = _chat(
        agent_client, agent_records["customer_id"], "What test code did I give you earlier?"
    ).json()
    assert body["tool_calls"][0]["success"] is False
    assert body["response"] == "I can't find a test code in this conversation."


@pytest.mark.parametrize(
    "claim",
    [
        "I've scheduled the meeting for Friday at 3 PM.",
        "I have booked a demo with Contoso for 14 Oct at 10:00.",
        "The follow-up was successfully created for tomorrow.",
        "Your meeting has been saved for Friday.",
    ],
)
def test_false_write_claim_after_a_lookup_is_still_replaced(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    claim: str,
) -> None:
    customer_id = agent_records["customer_id"]
    before_meetings = _count(agent_sessions, Meeting)
    _model(
        monkeypatch,
        _tool_call("get_customer_meetings", {"customer_id": customer_id}, "read"),
        AIMessage(content=claim),
    )
    body = _chat(agent_client, customer_id, "Book a demo with Contoso.").json()

    assert body["response"] == _NO_CHANGE_MESSAGE
    assert body["pending_action"] is None
    assert _count(agent_sessions, Meeting) == before_meetings
    assert _count(agent_sessions, HitlActionProposal) == 0
