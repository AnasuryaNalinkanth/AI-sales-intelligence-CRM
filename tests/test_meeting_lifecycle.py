"""Second meetings vs amendments, labelled details, open choices, and proposal lifecycle."""

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session, sessionmaker

from app.models import Contact, Customer, HitlActionProposal, HitlTask, Meeting
from app.services.agent_action_state import pending_actions
from tests.test_agent_actions import _count, _tool_call
from tests.test_agent_conversations import _model
from tests.test_meeting_amendments import DUBAI_1750, _amend, _proposals
from tests.test_meeting_drafts import DUBAI, _book, _chat, _draft, _user_id, _utc


def _chat_proposal(client: TestClient, customer_id: int, monkeypatch: pytest.MonkeyPatch) -> str:
    _model(monkeypatch, _book(customer_id, "p", scheduled_date="2026-10-15", scheduled_time="17:50"))
    return _chat(client, customer_id, "Schedule a meeting for 2026-10-15 5:50 PM.")["pending_action"]["action_id"]


def _draft_proposal(
    client: TestClient, sessions: sessionmaker[Session], customer_id: int, monkeypatch: pytest.MonkeyPatch
) -> str:
    task_id = _draft(
        sessions,
        user_id=_user_id(sessions),
        customer_id=customer_id,
        status="scheduled",
        scheduled_date="2026-10-15",
        scheduled_timezone=DUBAI,
    )
    _model(monkeypatch, AIMessage(content="Okay."))
    return _chat(client, customer_id, "5:50 PM", task_id)["pending_action"]["action_id"]


@pytest.fixture(params=["chat", "draft"])
def origin(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def pending(
    origin: str,
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    customer_id = agent_records["customer_id"]
    if origin == "chat":
        return _chat_proposal(agent_client, customer_id, monkeypatch)
    return _draft_proposal(agent_client, agent_sessions, customer_id, monkeypatch)


def _task_view(client: TestClient, sessions: sessionmaker[Session], action_id: str) -> dict[str, Any]:
    with sessions() as db:
        task_id = db.get(HitlActionProposal, action_id).task_id
    response = client.get(f"/api/agent/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


# --- Second meeting versus amendment -------------------------------------------------


def test_a_second_meeting_request_keeps_the_pending_proposal(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "n", scheduled_date="2026-10-19", scheduled_time="10:00"))
    body = _amend(agent_client, customer_id, "Also schedule another meeting on 2026-10-19 at 10:00 AM", pending)

    second = body["pending_action"]
    assert second["action_id"] != pending
    assert body["response"] == (
        "I am ready to create meeting for 2026-10-19 10:00 Asia/Dubai (UTC+04:00). Please confirm. "
        "Your earlier meeting proposal is still awaiting approval under Agent approvals."
    )
    with agent_sessions() as db:
        first = db.get(HitlActionProposal, pending)
        assert first.status == "pending" and _utc(first.parameters["scheduled_at"]) == DUBAI_1750
        assert db.get(HitlActionProposal, second["action_id"]).task_id != first.task_id
    assert sorted(status for _, status in _proposals(agent_sessions)) == ["pending", "pending"]


def test_an_explicit_change_moves_the_pending_meeting(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "c", scheduled_date="2026-10-16"))
    body = _amend(agent_client, customer_id, "Change it to 2026-10-16, same time", pending)

    payload = body["pending_action"]["payload"]
    assert body["pending_action"]["action_id"] != pending
    assert _utc(payload["scheduled_at"]) == DUBAI_1750 + timedelta(days=1)
    assert [status for _, status in _proposals(agent_sessions)] == ["cancelled", "pending"]


def test_a_new_time_without_a_clear_request_asks_before_moving_anything(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "c", scheduled_date="2026-10-16", scheduled_time="09:00"))
    body = _amend(agent_client, customer_id, "2026-10-16 at 9 am", pending)

    assert body["response"].startswith(
        "Your pending meeting is for 2026-10-15 17:50 Asia/Dubai (UTC+04:00). "
        "Do you want to move it to 2026-10-16 09:00 Asia/Dubai (UTC+04:00), or schedule a separate meeting?"
    )
    assert body["pending_action"]["action_id"] == pending
    assert _proposals(agent_sessions) == [(pending, "pending")]


def test_a_message_asking_for_both_is_clarified_without_running_the_model(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _model(monkeypatch, AIMessage(content="unused"))
    body = _amend(agent_client, agent_records["customer_id"], "Change it to another meeting on Friday", pending)

    assert body["response"].startswith("Do you want to change your pending meeting, or schedule a separate one?")
    assert body["pending_action"]["action_id"] == pending
    assert seen.seen == []
    assert _proposals(agent_sessions) == [(pending, "pending")]


# --- Labelled agenda and notes -------------------------------------------------------


def test_labelled_agenda_and_notes_are_captured_without_a_tool_call_and_kept(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, AIMessage(content="Noted."))
    first = _amend(
        agent_client, customer_id, "agenda: external penetration test; notes: call back at 3pm", pending
    )["pending_action"]
    assert (first["payload"]["agenda"], first["payload"]["notes"]) == (
        "external penetration test",
        "call back at 3pm",
    )
    # The time inside the notes is not read as a new meeting time.
    assert _utc(first["payload"]["scheduled_at"]) == DUBAI_1750

    _model(monkeypatch, AIMessage(content="Noted."))
    second = _amend(agent_client, customer_id, "make it 30 minutes", first["action_id"])["pending_action"]
    assert second["payload"]["duration"] == 30
    assert (second["payload"]["agenda"], second["payload"]["notes"]) == (
        "external penetration test",
        "call back at 3pm",
    )


def test_unlabelled_text_never_becomes_an_agenda(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(monkeypatch, AIMessage(content="It covers the security review."))
    body = _amend(agent_client, agent_records["customer_id"], "What is on the agenda today?", pending)

    assert body["pending_action"]["action_id"] == pending
    assert body["pending_action"]["payload"]["agenda"] is None


def test_labelled_agenda_fills_an_open_draft_without_a_tool_call(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    task_id = _draft(
        agent_sessions,
        user_id=_user_id(agent_sessions),
        customer_id=customer_id,
        status="scheduled",
        scheduled_date="2026-10-15",
        scheduled_timezone=DUBAI,
    )
    _model(monkeypatch, AIMessage(content="Okay."))
    body = _chat(agent_client, customer_id, "Agenda: scope review\nNotes: NDA signed at 3pm", task_id)

    assert body["response"] == "What time would you like to schedule the meeting?"
    with agent_sessions() as db:
        values = db.get(HitlTask, task_id).collected_data["operations"][0]["values"]
    assert (values["agenda"], values["notes"]) == ("scope review", "NDA signed at 3pm")
    assert "scheduled_time" not in values


# --- Ambiguous contact plus other details ----------------------------------------------


@pytest.fixture
def two_rohans(agent_sessions: sessionmaker[Session], agent_records: dict[str, int]) -> dict[str, int]:
    with agent_sessions() as db:
        customer = db.get(Customer, agent_records["customer_id"])
        cto = Contact(customer=customer, name="Rohan Kapoor", job_title="CTO")
        engineer = Contact(customer=customer, name="Rohan Kapoor", job_title="Network Engineer")
        db.add_all([cto, engineer])
        db.commit()
        return {"cto": cto.contact_id, "engineer": engineer.contact_id}


@pytest.mark.parametrize("model_calls_tool", [True, False])
def test_ambiguous_contact_keeps_other_details_and_resolves_next_turn(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    two_rohans: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
    model_calls_tool: bool,
) -> None:
    customer_id = agent_records["customer_id"]
    message = "Rohan Kapoor, 45 minutes, agenda: pen test"
    _model(
        monkeypatch,
        _book(customer_id, "a", contact_name="Rohan Kapoor", duration=45, agenda="pen test")
        if model_calls_tool
        else AIMessage(content="Which Rohan?"),
    )
    first = _amend(agent_client, customer_id, message, pending)

    revised = first["pending_action"]
    assert revised["action_id"] != pending
    assert (revised["payload"]["duration"], revised["payload"]["agenda"]) == (45, "pen test")
    assert revised["payload"]["contact_id"] is None  # Unchanged until the user chooses.
    assert 'More than one contact matches "Rohan Kapoor"' in first["response"]
    assert first["response"].endswith("You can approve this version now, or answer and I will revise it.")

    # "the CTO" settles it; nothing else needs repeating.
    _model(monkeypatch, AIMessage(content="Okay."))
    second = _amend(agent_client, customer_id, "The CTO", revised["action_id"])["pending_action"]
    assert second["payload"]["contact_id"] == two_rohans["cto"]
    assert (second["payload"]["duration"], second["payload"]["agenda"]) == (45, "pen test")
    assert _utc(second["payload"]["scheduled_at"]) == DUBAI_1750
    with agent_sessions() as db:
        task = db.get(HitlTask, db.get(HitlActionProposal, second["action_id"]).task_id)
        assert "pending_amendment" not in task.collected_data


def test_ambiguous_contact_in_a_draft_is_kept_and_chosen_by_id(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    two_rohans: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    task_id = _draft(
        agent_sessions,
        user_id=_user_id(agent_sessions),
        customer_id=customer_id,
        status="scheduled",
        scheduled_date="2026-10-15",
        scheduled_timezone=DUBAI,
    )
    _model(monkeypatch, AIMessage(content="Okay."))
    first = _chat(agent_client, customer_id, "5:50 PM with Rohan Kapoor, 45 minutes", task_id)
    assert first["pending_action"] is None and "More than one contact matches" in first["response"]
    with agent_sessions() as db:
        values = db.get(HitlTask, task_id).collected_data["operations"][0]["values"]
    assert (values["scheduled_time"], values["duration"], values["contact_name"]) == ("17:50", 45, "Rohan Kapoor")

    _model(monkeypatch, AIMessage(content="Okay."))
    chosen = _chat(agent_client, customer_id, f"#{two_rohans['engineer']}", task_id)
    payload = chosen["pending_action"]["payload"]
    assert (payload["contact_id"], payload["duration"]) == (two_rohans["engineer"], 45)


# --- Lifecycle: navigation, superseded vs cancelled, expiry --------------------------------


def test_navigating_away_and_back_leaves_the_proposal_pending_and_listed(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
) -> None:
    # Navigation sends no request; coming back reads the task list (restorePendingProposal).
    tasks = agent_client.get("/api/agent/tasks").json()
    listed = [
        operation
        for task in tasks
        if task["status"] == "awaiting_approval"
        for operation in task["operations"]
        if operation["status"] == "pending" and operation["action_id"] == pending
    ]
    assert len(listed) == 1
    assert listed[0]["values"]["customer_id"] == agent_records["customer_id"]
    assert _proposals(agent_sessions) == [(pending, "pending")]


def test_frontend_reset_never_cancels_a_pending_proposal() -> None:
    # Static guard: the frontend has no JS test runner in this project.
    source = Path("app/frontend/js/customer.js").read_text(encoding="utf-8")
    reset = re.search(r"function resetCustomerAiState\(\) \{(.*?)\n\}", source, re.DOTALL)
    assert reset is not None and "cancelAgentAction" not in reset.group(1)
    assert "cancelPending:" not in source and "{ cancelPending" not in source
    assert "await restorePendingProposal(customerId);" in source


# Runs the real restore functions from customer.js in Node against stubbed API calls.
_RESTORE_HARNESS = """
const scenario = JSON.parse(process.argv[1]);
let selectedCustomerId = scenario.selected;
let pendingAgentAction = scenario.pending ?? null;
let agentChatMessages = scenario.messages ?? [];
let agentChatLoading = Boolean(scenario.loading);
let agentConversationId = null;
const selectedCustomer = {};
let renders = 0;
const renderCustomer = () => { renders += 1; };
const getLatestConversation = async () => {
  if (scenario.conversationFails) throw new Error("offline");
  return scenario.conversation ?? null;
};
const getAgentTasks = async () => {
  if (scenario.switchTo !== undefined) selectedCustomerId = scenario.switchTo;
  if (scenario.tasksFail) throw new Error("offline");
  return scenario.tasks;
};
%s
%s
(async () => {
  for (let run = 0; run < (scenario.runs ?? 1); run += 1) await restoreAgentConversation(scenario.customer);
  console.log(JSON.stringify({
    pending: pendingAgentAction && pendingAgentAction.action_id,
    messages: agentChatMessages.length,
    conversationId: agentConversationId,
    renders,
  }));
})();
"""


def _operation(action_id: str, customer_id: int, expires_at: str = "2026-10-20") -> dict[str, Any]:
    return {
        "status": "awaiting_approval",
        "expires_at": expires_at,
        "operations": [
            {
                "status": "pending",
                "action_id": action_id,
                "action": "create_meeting",
                "values": {"customer_id": customer_id},
            }
        ],
    }


def _restore(**scenario: Any) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = Path("app/frontend/js/customer.js").read_text(encoding="utf-8")
    functions = [
        re.search(rf"async function {name}\(customerId\) \{{.*?\n\}}", source, re.DOTALL)
        for name in ("restoreAgentConversation", "restorePendingProposal")
    ]
    assert all(functions)
    script = _RESTORE_HARNESS % tuple(match.group(0) for match in functions)
    result = subprocess.run(
        [node, "-e", script, json.dumps({"customer": 7, "selected": 7, **scenario})],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


TASKS = [_operation("other-customer", 8, "2026-10-30"), _operation("older", 7), _operation("newest", 7, "2026-10-25")]
CONVERSATION = {"conversation_id": 3, "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize(
    "scenario",
    [
        {},  # no saved conversation
        {"conversation": CONVERSATION, "messages": [{"role": "user", "text": "typing"}]},
        {"conversation": CONVERSATION, "loading": True},
        {"conversationFails": True},
    ],
    ids=["no-conversation", "chat-in-progress", "chat-loading", "history-fails"],
)
def test_pending_proposal_is_restored_when_conversation_restore_is_skipped(scenario: dict[str, Any]) -> None:
    state = _restore(tasks=TASKS, **scenario)
    # Only this customer's newest proposal, never customer 8's.
    assert state["pending"] == "newest"
    assert state["conversationId"] is None
    assert state["messages"] == len(scenario.get("messages", []))


def test_pending_proposal_restore_also_runs_after_restoring_the_conversation() -> None:
    state = _restore(tasks=TASKS, conversation=CONVERSATION)
    assert (state["pending"], state["conversationId"], state["messages"]) == ("newest", 3, 1)


def test_pending_proposal_restore_is_customer_scoped_and_idempotent() -> None:
    assert _restore(tasks=[_operation("other-customer", 8)])["pending"] is None
    # The user switched customers while the task list loaded.
    assert _restore(tasks=TASKS, switchTo=8)["pending"] is None
    # A proposal already shown is kept, not replaced or rendered again.
    kept = _restore(tasks=TASKS, pending={"action_id": "current"})
    assert (kept["pending"], kept["renders"]) == ("current", 0)
    twice = _restore(tasks=TASKS, runs=2)
    assert (twice["pending"], twice["renders"]) == ("newest", 1)


def test_pending_proposal_restore_failure_leaves_the_page_working() -> None:
    state = _restore(tasksFail=True, conversation=CONVERSATION)
    assert (state["pending"], state["conversationId"]) == (None, 3)


def test_superseded_and_user_cancelled_versions_are_shown_differently(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer_id = agent_records["customer_id"]
    _model(monkeypatch, _book(customer_id, "a", duration=45))
    revised = _amend(agent_client, customer_id, "make it 45 minutes", pending)["pending_action"]["action_id"]

    view = _task_view(agent_client, agent_sessions, revised)
    assert [(op["action_id"], op["status"]) for op in view["operations"]] == [
        (pending, "superseded"),
        (revised, "pending"),
    ]
    assert view["status"] == "awaiting_approval"

    cancelled = agent_client.post(f"/api/agent/actions/{revised}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    view = _task_view(agent_client, agent_sessions, revised)
    assert [(op["action_id"], op["status"]) for op in view["operations"]] == [
        (pending, "superseded"),
        (revised, "cancelled"),
    ]
    assert view["status"] == "cancelled"


@pytest.mark.parametrize("already_swept", [False, True])
def test_expired_proposals_report_410_to_amendment_and_confirmation(
    agent_client: TestClient,
    agent_sessions: sessionmaker[Session],
    agent_records: dict[str, int],
    pending: str,
    monkeypatch: pytest.MonkeyPatch,
    already_swept: bool,
) -> None:
    with agent_sessions() as db:
        proposal = db.get(HitlActionProposal, pending)
        proposal.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        if already_swept:
            pending_actions.expire_overdue(db)  # The same sweep other requests run.
            assert db.get(HitlActionProposal, pending).status == "expired"
    seen = _model(monkeypatch, AIMessage(content="unused"))

    amended = _amend(agent_client, agent_records["customer_id"], "make it 30 minutes", pending, status=410)
    assert amended["error"]["code"] == "action_expired"
    confirmed = agent_client.post(f"/api/agent/actions/{pending}/confirm")
    assert confirmed.status_code == 410 and confirmed.json()["error"]["code"] == "action_expired"
    assert seen.seen == []
    with agent_sessions() as db:
        proposal = db.get(HitlActionProposal, pending)
        assert proposal.status == "expired"
        assert db.get(HitlTask, proposal.task_id).status == "expired"
    assert _count(agent_sessions, Meeting) == 1  # Only the fixture's meeting.
