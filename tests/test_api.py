import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import APIError, api_error_handler
from app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("path", "title"),
    [
        ("/", "Customer relationships, sales activity and approvals"),
        ("/login", "Sign in to SalesDesk"),
        ("/customer-registration", "Register a customer"),
        ("/customer", "Customer intelligence"),
    ],
)
def test_frontend_pages_are_served(path: str, title: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert title in response.text
    assert "text/html" in response.headers["content-type"]


def test_frontend_assets_are_served() -> None:
    response = client.get("/static/js/api.js")

    assert response.status_code == 200
    assert "export function createCustomer" in response.text
    assert "export function generateCustomerSummary" in response.text
    assert "export function generateMeetingBrief" in response.text
    assert "/agent/customer-summary/" in response.text
    assert "/agent/meeting-brief/" in response.text


def test_frontend_files_are_revalidated_so_updates_never_mix_versions() -> None:
    for path in ("/", "/customer", "/static/js/common.js", "/static/css/style.css"):
        response = client.get(path)
        assert response.headers["cache-control"] == "no-cache", path
        assert response.headers["etag"], path

    revalidated = client.get("/static/js/common.js", headers={"If-None-Match": client.get("/static/js/common.js").headers["etag"]})
    assert revalidated.status_code == 304
    assert "cache-control" not in client.get("/api/auth/me").headers


def test_customer_intelligence_js_wires_ai_endpoints() -> None:
    customer_js = client.get("/static/js/customer.js")
    assert customer_js.status_code == 200
    assert "generateCustomerSummary" in customer_js.text
    assert "generateMeetingBrief" in customer_js.text
    assert "Generate AI Summary" in customer_js.text
    assert "Prepare Me" in customer_js.text
    assert "aiSummaryError" in customer_js.text
    assert "Generating customer summary" in customer_js.text

    page = client.get("/customer")
    assert page.status_code == 200
    assert "meeting-brief-dialog" in page.text


def test_customer_intelligence_loads_first_page_on_startup() -> None:
    customer_js = client.get("/static/js/customer.js").text.replace("\r\n", "\n")
    startup = customer_js[customer_js.index("mountAuthControls();\nif (requireAuth())"):]

    # The first page loads unconditionally, not only for ?customer_id= links.
    assert startup.index("const initialLoad = loadCustomers(1);") < startup.index(
        "if (linkedCustomerId"
    )
    assert startup.count("loadCustomers(") == 1
    # Overlapping loads (startup vs. a quick search) must not render stale rows.
    assert "if (requestId === customerListRequest) renderCustomerList(data);" in customer_js


def test_customer_page_puts_collapsed_approvals_below_the_workspace() -> None:
    page = client.get("/customer").text
    # Customer search, list and detail come first; the approvals panel follows, collapsed.
    assert page.index('class="intelligence-layout"') < page.index('class="hitl-panel')
    assert 'id="toggle-hitl"' in page and 'aria-expanded="false"' in page
    assert '<div id="hitl-content" class="hitl-content" hidden>' in page


def test_customer_chat_keeps_scroll_position_and_grows_the_prompt() -> None:
    customer_js = client.get("/static/js/customer.js").text.replace("\r\n", "\n")
    render = customer_js[customer_js.index("function renderCustomer(customer)"):]
    render = render[: render.index("\n}\n")]
    # The rebuilt chat log is put back where the reader was, not at scrollTop 0.
    assert render.startswith("function renderCustomer(customer) {\n  const chatView = captureChatView(")
    assert render.rstrip().endswith("restoreChatView(chatView);")
    assert 'event.key === "Enter" && !event.shiftKey && !event.isComposing' in customer_js
    assert "function autosizeChatInput(input)" in customer_js


def test_customer_chat_renders_schedule_alternatives_through_task_recheck() -> None:
    customer_js = client.get("/static/js/customer.js").text.replace("\r\n", "\n")

    assert "response.schedule_alternatives" in customer_js
    chooser = customer_js[customer_js.index("async function chooseAgentScheduleAlternative"):]
    chooser = chooser[: chooser.index("\n}\n")]
    assert "submitScheduleChoice(values)" in chooser
    assert 'error.code === "schedule_conflict"' in chooser


def test_customer_chat_offers_new_activity_for_past_times_via_task_flow() -> None:
    customer_js = client.get("/static/js/customer.js").text.replace("\r\n", "\n")

    assert 'agentScheduleReason === "past"' in customer_js
    assert "`Schedule a new ${label}`" in customer_js
    requester = customer_js[customer_js.index("async function requestNewScheduleTimes"):]
    requester = requester[: requester.index("\n}\n")]
    # No times are invented client-side: they come from the server's schedule_in_past response.
    assert "submitScheduleChoice({})" in requester
    assert 'error.code === "schedule_in_past"' in requester


def test_chat_schedule_buttons_follow_task_lifecycle_instead_of_stale_state() -> None:
    customer_js = client.get("/static/js/customer.js").text.replace("\r\n", "\n")
    submit = customer_js[customer_js.index("async function submitScheduleChoice"):]
    submit = submit[: submit.index("\n}\n")]

    # The task is re-read before any input is sent, and inputs go only to an open task.
    assert submit.index("resumeAgentTask(draft.taskId)") < submit.index("submitAgentTaskInput(")
    assert 'task?.status === "collecting_information"' in submit
    # A submitted task is returned as-is (no duplicate proposal); a dead one is replaced.
    assert "SUBMITTED_TASK_STATUSES.has(task.status)) return task;" in submit
    assert "createAgentTask(" in submit


def test_chat_shows_compact_confirmation_instead_of_task_management_ui() -> None:
    customer_js = client.get("/static/js/customer.js").text.replace("\r\n", "\n")
    chat = customer_js[customer_js.index("function appendAgentChat"):]
    chat = chat[: chat.index("\n}\n")]
    card = customer_js[customer_js.index("function appendPendingActionCard"):]
    card = card[: card.index("\n}\n")]

    assert "renderHitlTask(" not in chat
    assert "openHitlPanel" in chat
    assert "agent-action-details" not in card
    assert '"Reject"' not in card
    assert '"Confirm"' in card and '"Cancel"' in card


@pytest.mark.parametrize(
    ("path", "module"),
    [
        ("/api/customers/test", "customers"),
        ("/api/enquiries/test", "enquiries"),
        ("/api/contacts/test", "contacts"),
        ("/api/meetings/test", "meetings"),
        ("/api/calls/test", "calls"),
        ("/api/followups/test", "followups"),
        ("/api/analytics/test", "analytics"),
        ("/api/agent/test", "agent"),
    ],
)
def test_module_endpoint(agent_client: TestClient, path: str, module: str) -> None:
    response = agent_client.get(path)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "module": module}


def test_cors_allows_configured_frontend_origin() -> None:
    response = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5500",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5500"


def test_application_errors_return_consistent_json() -> None:
    test_app = FastAPI()
    test_app.add_exception_handler(APIError, api_error_handler)

    @test_app.get("/failure")
    def raise_application_error() -> None:
        raise APIError("Expected failure", status_code=409, code="conflict")

    response = TestClient(test_app).get("/failure")

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "conflict", "message": "Expected failure"}
    }


def test_database_dependency_is_configured_without_connecting() -> None:
    from app.database.connection import Base, SessionLocal, engine, get_db

    assert Base is not None
    assert SessionLocal is not None
    assert engine is not None
    assert engine.pool.checkedout() == 0
    db = next(get_db())
    try:
        assert db.is_active
    finally:
        db.close()


def test_dashboard_needs_attention_panel_renders_the_api_response_read_only() -> None:
    from typing import get_args

    from app.schemas.intelligence import AttentionReasonCode

    html = client.get("/").text
    for marker in (
        'id="attention"',
        'id="attention-status" class="dashboard-message" role="status"',
        'id="attention-error" class="dashboard-message dashboard-error" role="alert" hidden',
        'id="attention-list"',
        'id="attention-retry"',
        'id="attention-rules"',
    ):
        assert marker in html
    # The panel sits between the headline counts and the schedule.
    assert html.index('class="glance"') < html.index('id="attention"') < html.index('class="dashboard-section schedule-section')

    js = client.get("/static/js/dashboard.js").text.replace("\r\n", "\n")
    block = js[js.index("// ---------- Needs attention"):js.index("if (getAuthToken())")]
    assert 'getAnalytics("attention", { limit: ATTENTION_LIMIT })' in block
    assert "void loadAttention();" in js
    # Every reason the API can return has a text label, so meaning never depends on colour.
    for code in get_args(AttentionReasonCode):
        assert f"  {code}: {{ label:" in block, code
    assert "known?.label ?? humanize(reason.code)" in block
    assert 'element("span", "attention-reason-detail", reason.detail)' in block
    assert "/customer?customer_id=${encodeURIComponent(item.customer_id)}" in block
    assert "error?.status === 401" in block
    assert "No open sales leads need attention right now." in block
    # Read-only: no writes, no other endpoints, no example data.
    for forbidden in ("method:", "moveWorkspaceActivity", "fetch(", "POST"):
        assert forbidden not in block, forbidden

    css = client.get("/static/css/dashboard.css").text
    for code in ("overdue_followup", "no_recent_activity", "no_next_step"):
        assert f'.attention-reason[data-reason="{code}"]' in css


def test_dashboard_separates_customer_level_work_and_scopes_the_overdue_headline() -> None:
    html = client.get("/").text
    tile = html[html.index('class="glance-item glance-alert"'):]
    tile = tile[:tile.index("</a>")]
    # The headline keeps its analytics value; the note states what that value counts.
    assert 'data-glance="overdue_followups"' in tile
    assert "All customers, enquiry and customer-level; due before today (UTC)" in tile

    js = client.get("/static/js/dashboard.js").text.replace("\r\n", "\n")
    block = js[js.index("function renderCustomerLevelContext"):js.index("function renderAttention(")]
    assert "Number(item.customer_level_overdue_followup_count) || 0" in block
    assert "if (overdue > 0)" in block
    assert "item.customer_level_oldest_overdue_due_at" in block
    assert "item.customer_level_next_step_at" in block
    assert "item.customer_level_last_activity_at" in block
    assert "Customer-level · not linked to this sales lead" in block
    assert "if (!rows.length) return null;" in block
    # Customer-level rows never reuse the enquiry reason markup or codes.
    assert "attention-reason" not in block and "dataset.reason" not in block
    for forbidden in ("method:", "fetch(", "getAnalytics"):
        assert forbidden not in block, forbidden


def test_dashboard_suggestions_only_prepare_task_drafts_or_link() -> None:
    import re

    js = client.get("/static/js/dashboard.js").text.replace("\r\n", "\n")
    block = js[js.index("// ---------- Needs attention"):js.index("if (getAuthToken())")]
    api_exports = set(re.findall(r"export (?:async )?function (\w+)", client.get("/static/js/api.js").text))
    used = {name for name in api_exports if re.search(rf"\b{name}\(", block)}
    # Reading the queue and preparing a task draft are the panel's only API calls.
    assert used == {"getAnalytics", "createAgentTask"}
    suggestions = block[block.index("let preparingTask"):block.index("// Same UTC calendar date")]
    assert "await createAgentTask([{ action: action.kind, values }]);" in suggestions
    assert "const values = { ...action.prefill };" in suggestions
    assert "if (action.timezone_field) values[action.timezone_field] = timezoneName;" in suggestions
    # Duplicate-click protection, failure recovery and the shared expired-session message.
    assert "if (preparingTask) return;" in suggestions
    assert "setSuggestionButtonsDisabled(true);" in suggestions and "setSuggestionButtonsDisabled(false);" in suggestions
    assert 'error?.status === 401' in suggestions
    assert "#approvals" in suggestions and "/customer?customer_id=${encodeURIComponent(action.prefill.customer_id)}" in suggestions
    # Wording describes preparing a draft, never completing the action.
    assert "Prepare task: " in suggestions and "Nothing changes in the CRM until you submit it." in suggestions
    assert "Opens the customer record; no task is created." in suggestions
    for forbidden in ("updateFollowup", "moveWorkspaceActivity", "completeFollowup", "apply_enrichment", "fetch("):
        assert forbidden not in block, forbidden


def test_approvals_deep_link_scrolls_again_after_the_page_has_rendered() -> None:
    page = client.get("/customer").text
    assert '<h2 id="hitl-heading" tabindex="-1">' in page

    customer_js = client.get("/static/js/customer.js").text.replace("\r\n", "\n")
    startup = customer_js[customer_js.index("mountAuthControls();\nif (requireAuth())"):]
    assert "const hitlReady = initializeHitl();" in startup
    assert 'const linkedToApprovals = window.location.hash === "#approvals";' in startup
    # The linked customer's full render (not just the list) is awaited.
    assert "customerReady = initialLoad.then(() => selectCustomer(linkedCustomerId));" in startup
    # Only deep links re-scroll, once, after the task list and the customer have settled.
    assert startup.count("followApprovalsLinkAfter(") == 1
    assert "if (linkedToApprovals) followApprovalsLinkAfter([hitlReady, customerReady]);" in startup

    follow = customer_js[customer_js.index("function followApprovalsLinkAfter"):customer_js.index("const SUBMITTED_TASK_STATUSES")]
    assert "Promise.allSettled(loads)" in follow
    assert 'if (userMoved || window.location.hash !== "#approvals") return;' in follow
    for event in ("wheel", "touchmove", "keydown", "pointerdown"):
        assert f'"{event}"' in follow
    assert "removeEventListener" in follow
    assert 'focus({ preventScroll: true })' in follow
    for forbidden in ("setTimeout", "setInterval", "requestAnimationFrame"):
        assert forbidden not in follow, forbidden
    # openHitlPanel goes through the reduced-motion-aware scroll helper.
    helper = customer_js[customer_js.index("function scrollToElement"):customer_js.index("function followApprovalsLinkAfter")]
    assert 'reduceMotion ? "auto" : "smooth"' in helper
    assert "scrollToElement(hitlContent.closest(\".hitl-panel\"));" in helper
