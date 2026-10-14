"""Proactive sales intelligence: detection rules, scoring, API, isolation, agent and UI."""

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import Engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.tools import AgentToolContext, build_read_only_tools
from app.agent.tools.analytics_tools import build_analytics_tools
from app.core.exceptions import APIError
from app.models import (
    Call,
    Company,
    Customer,
    FollowUp,
    Meeting,
    Organization,
    SalesEnquiry,
)
from app.services.proactive_insights_service import RULES, get_proactive_insights
from tests.conftest import FROZEN_SCHEDULING_NOW, _FrozenSchedulingClock, legacy_organization_id
from tests.test_agent_actions import _chat_model, _tool_call

NOW = FROZEN_SCHEDULING_NOW  # 2026-10-01 00:00 UTC


def _at(month: int, day: int) -> datetime:
    return datetime(2026, month, day, 9, 0, tzinfo=timezone.utc)


def _customer(
    db: Session,
    name: str,
    *,
    organization_id: int | None = None,
    stage: str = "new",
    status: str = "active",
    created: datetime = _at(5, 1),
) -> Customer:
    company = Company(
        organization_id=organization_id or legacy_organization_id(db), company_name=f"{name} Ltd"
    )
    customer = Customer(
        company=company, customer_name=name, status=status, sales_stage=stage,
        created_at=created, updated_at=created,
    )
    db.add_all([company, customer])
    db.flush()
    return customer


def _enquiry(
    db: Session,
    customer: Customer,
    *,
    created: datetime,
    priority: str = "normal",
    value: int | None = None,
    status: str = "open",
) -> SalesEnquiry:
    enquiry = SalesEnquiry(
        customer=customer, product="CRM", enquiry_text="Needs a CRM", priority=priority,
        status=status, estimated_value=value, created_at=created, updated_at=created,
    )
    db.add(enquiry)
    db.flush()
    return enquiry


def _followup(
    db: Session, customer: Customer, enquiry: SalesEnquiry | None, due: datetime, status: str = "pending",
    completed_at: datetime | None = None,
) -> FollowUp:
    followup = FollowUp(
        customer=customer, enquiry=enquiry, type="email", due_date=due, status=status,
        completed_at=completed_at,
    )
    db.add(followup)
    db.flush()
    return followup


def _insights(db: Session, **filters: Any):
    return get_proactive_insights(
        db, organization_id=legacy_organization_id(db), now=NOW, **filters
    )


def _by_id(result) -> dict[str, Any]:
    return {item.recommendation_id: item for item in result.items}


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.analytics.datetime", _FrozenSchedulingClock)
    monkeypatch.setattr("app.agent.tools.analytics_tools.datetime", _FrozenSchedulingClock)


# --- detection rules ----------------------------------------------------------------


def test_overdue_followups_exclude_completed_and_future_ones(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Overdue Co")
        enquiry = _enquiry(db, customer, created=_at(9, 25))
        overdue = _followup(db, customer, enquiry, _at(9, 20))
        _followup(db, customer, enquiry, _at(9, 10), status="completed", completed_at=_at(9, 11))
        _followup(db, customer, enquiry, _at(10, 5))
        db.commit()
        item = _by_id(_insights(db))[f"enquiry-{enquiry.enquiry_id}"]

    assert item.category == "overdue_followup"
    assert item.followup_id == overdue.followup_id
    overdue_facts = [e for e in item.evidence if e.record_type == "followup"]
    assert [e.record_id for e in overdue_facts] == [overdue.followup_id]
    assert "10 day(s) ago" in overdue_facts[0].detail  # 2026-09-20 09:00 -> 2026-10-01 00:00


def test_neglected_leads_use_recorded_activity_and_label_missing_data(
    agent_sessions: sessionmaker[Session],
) -> None:
    with agent_sessions() as db:
        recent = _customer(db, "Recent Co")
        recent_enquiry = _enquiry(db, recent, created=_at(6, 1))
        db.add(Meeting(customer=recent, enquiry=recent_enquiry, scheduled_at=_at(9, 25), status="completed"))
        db.add(Meeting(customer=recent, enquiry=recent_enquiry, scheduled_at=_at(10, 20), status="scheduled"))
        stale = _customer(db, "Stale Co")
        stale_enquiry = _enquiry(db, stale, created=_at(6, 1))
        db.add(Call(customer=stale, enquiry=stale_enquiry, actual_time=_at(8, 1), status="completed"))
        silent = _customer(db, "Silent Co")
        silent_enquiry = _enquiry(db, silent, created=_at(8, 1))
        # A lead with no open enquiry is judged on all of its activity.
        lead = _customer(db, "Lead Co", stage="qualified", created=_at(7, 1))
        won = _customer(db, "Won Co", stage="won", created=_at(1, 1))
        db.commit()
        items = _by_id(_insights(db))

    assert f"enquiry-{recent_enquiry.enquiry_id}" not in items
    verified = items[f"enquiry-{stale_enquiry.enquiry_id}"]
    assert verified.category == "neglected_lead"  # Stage "new": never "stalled".
    assert any(e.kind == "fact" and "Last counted activity was on 2026-08-01" in e.detail for e in verified.evidence)
    unverified = items[f"enquiry-{silent_enquiry.enquiry_id}"]
    assert unverified.category == "neglected_lead"
    missing = [e for e in unverified.evidence if e.kind == "missing_data" and "measured from its creation" in e.detail]
    assert missing and "does not mean the lead is lost" in missing[0].detail
    assert items[f"customer-{lead.customer_id}"].category == "neglected_lead"
    assert f"customer-{won.customer_id}" not in items


def test_stalled_deals_need_age_stage_inactivity_and_no_next_step(
    agent_sessions: sessionmaker[Session],
) -> None:
    with agent_sessions() as db:
        stalled = _customer(db, "Stalled Co", stage="proposal")
        old = _enquiry(db, stalled, created=_at(6, 1))
        db.add(Call(customer=stalled, enquiry=old, actual_time=_at(8, 1), status="completed"))
        young_customer = _customer(db, "Young Co", stage="proposal")
        young = _enquiry(db, young_customer, created=_at(9, 5))  # 26 days: inactive, too young
        booked_customer = _customer(db, "Booked Co", stage="proposal")
        booked = _enquiry(db, booked_customer, created=_at(6, 1))
        db.add(Call(customer=booked_customer, enquiry=booked, scheduled_at=_at(10, 20), status="scheduled"))
        db.commit()
        items = _by_id(_insights(db))

    item = items[f"enquiry-{old.enquiry_id}"]
    assert item.category == "stalled_deal"
    estimate = [e for e in item.evidence if e.kind == "estimate"]
    assert estimate and "time in the current stage is unknown" in estimate[0].detail
    assert items[f"enquiry-{young.enquiry_id}"].category == "neglected_lead"
    # A scheduled next step means it is not stalled; it is still inactive, so neglected.
    assert items[f"enquiry-{booked.enquiry_id}"].category == "neglected_lead"


def test_upcoming_meetings_and_high_priority_without_next_step_need_attention(
    agent_sessions: sessionmaker[Session],
) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Meeting Co")
        enquiry = _enquiry(db, customer, created=_at(9, 28))
        db.add(Meeting(customer=customer, enquiry=enquiry, scheduled_at=_at(10, 1), status="scheduled", agenda="Demo"))
        urgent_customer = _customer(db, "Urgent Co")
        urgent = _enquiry(db, urgent_customer, created=_at(9, 20), priority="urgent")
        db.commit()
        items = _by_id(_insights(db))

    meeting = items[f"enquiry-{enquiry.enquiry_id}"]
    assert meeting.category == "needs_attention" and meeting.meeting_id is not None
    assert "meeting brief" in meeting.recommended_next_action
    assert items[f"enquiry-{urgent.enquiry_id}"].category == "needs_attention"


# --- scoring ------------------------------------------------------------------------


def test_scores_follow_the_documented_rules(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Score Co", stage="negotiation")
        enquiry = _enquiry(db, customer, created=_at(9, 25), priority="urgent", value=5000)
        _followup(db, customer, enquiry, datetime(2026, 9, 24, tzinfo=timezone.utc))  # 7 days
        db.commit()
        item = _by_id(_insights(db))[f"enquiry-{enquiry.enquiry_id}"]

    # overdue 7/14 = 0.5 -> urgency 100 * 0.5 * 0.5 = 25; priority, stage, value all 1 -> impact 100.
    assert item.score_components["overdue"] == 0.5
    assert (item.urgency_score, item.impact_score) == (25, 100)
    # 0.6 * 25 + 0.4 * 100 = 55, plus 5 for a second signal (no next step).
    assert item.priority_score == 60
    assert (item.urgency, item.potential_impact) == ("low", "high")
    assert item.reason.startswith("Strongest factor: Sales lead priority is urgent")


def test_scores_are_clamped_and_multiple_signals_rank_first(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Max Co", stage="negotiation")
        enquiry = _enquiry(db, customer, created=_at(6, 1), priority="urgent", value=100)
        _followup(db, customer, enquiry, _at(9, 1))
        db.add(Meeting(customer=customer, enquiry=enquiry, scheduled_at=datetime(2026, 10, 2, tzinfo=timezone.utc), status="scheduled"))
        mild = _customer(db, "Mild Co")
        mild_enquiry = _enquiry(db, mild, created=_at(9, 5), priority="low")
        db.commit()
        result = _insights(db)

    top = result.items[0]
    assert top.enquiry_id == enquiry.enquiry_id
    assert top.urgency_score == 90  # 50 overdue + 30 inactivity + 20 * 0.5 imminence
    assert top.priority_score == 100  # 54 + 40 + 10 bonus = 104, clamped
    assert result.items[-1].enquiry_id == mild_enquiry.enquiry_id
    assert [i.priority_score for i in result.items] == sorted((i.priority_score for i in result.items), reverse=True)


def test_unknown_values_and_missing_timestamps_are_not_invented(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Unknown Co")
        enquiry = _enquiry(db, customer, created=_at(8, 1), value=None)
        # Neither has a time, so neither counts as activity.
        db.add(Call(customer=customer, enquiry=enquiry, status="completed"))
        _followup(db, customer, enquiry, _at(10, 20), status="completed", completed_at=None)
        db.commit()
        item = _by_id(_insights(db))[f"enquiry-{enquiry.enquiry_id}"]

    assert item.score_components["value"] == 0
    assert any(e.kind == "missing_data" and "No estimated value" in e.detail for e in item.evidence)
    assert any(e.kind == "missing_data" and "measured from its creation" in e.detail for e in item.evidence)
    assert "Estimated value is" not in json.dumps(item.model_dump(mode="json"))


def test_one_recommendation_per_issue_with_combined_evidence(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Busy Co")
        enquiry = _enquiry(db, customer, created=_at(6, 1))
        first = _followup(db, customer, enquiry, _at(9, 1))
        second = _followup(db, customer, enquiry, _at(9, 15))
        loose = _followup(db, customer, None, _at(9, 20))  # Customer-level: its own issue.
        closed = _enquiry(db, customer, created=_at(5, 1), status="closed")
        orphan = _followup(db, customer, closed, _at(9, 21))  # Its enquiry is closed.
        db.add(Meeting(customer=customer, enquiry=enquiry, scheduled_at=_at(10, 2), status="scheduled"))
        db.commit()
        result = _insights(db)

    ids = [item.recommendation_id for item in result.items]
    assert len(ids) == len(set(ids)) == 2
    by_id = _by_id(result)
    enquiry_item = by_id[f"enquiry-{enquiry.enquiry_id}"]
    assert enquiry_item.followup_id == first.followup_id  # The oldest.
    assert [l.record_id for l in enquiry_item.links if l.record_type == "followup"] == [first.followup_id, second.followup_id]
    assert any("4 overdue follow-ups in total" in e.detail for e in enquiry_item.evidence)
    assert enquiry_item.meeting_id is not None
    customer_item = by_id[f"customer-{customer.customer_id}"]
    assert customer_item.enquiry_id is None
    assert {l.record_id for l in customer_item.links if l.record_type == "followup"} == {loose.followup_id, orphan.followup_id}
    assert any("no longer open" in e.detail for e in customer_item.evidence)


def test_links_point_at_the_records_own_customer_page(agent_sessions: sessionmaker[Session]) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Link Co")
        enquiry = _enquiry(db, customer, created=_at(6, 1))
        followup = _followup(db, customer, enquiry, _at(9, 1))
        db.commit()
        item = _by_id(_insights(db))[f"enquiry-{enquiry.enquiry_id}"]

    cid = customer.customer_id
    assert [link.href for link in item.links] == [
        f"/customer?customer_id={cid}",
        f"/customer?customer_id={cid}#enquiry-{enquiry.enquiry_id}",
        f"/customer?customer_id={cid}#followup-{followup.followup_id}",
    ]
    assert item.customer_id == cid and item.enquiry_id == enquiry.enquiry_id
    assert item.suggested_actions[0].prefill == {"customer_id": cid}


def test_query_count_does_not_grow_with_records(
    agent_engine: Engine, agent_sessions: sessionmaker[Session]
) -> None:
    statements: list[str] = []

    def run() -> int:
        statements.clear()
        listener = lambda *args: statements.append(args[2])  # noqa: E731
        event.listen(agent_engine, "before_cursor_execute", listener)
        try:
            with agent_sessions() as db:
                get_proactive_insights(db, organization_id=1, now=NOW)
        finally:
            event.remove(agent_engine, "before_cursor_execute", listener)
        return len(statements)

    def seed(count: int) -> None:
        with agent_sessions() as db:
            for index in range(count):
                customer = _customer(db, f"Load {count}-{index}")
                enquiry = _enquiry(db, customer, created=_at(6, 1))
                _followup(db, customer, enquiry, _at(9, 1))
            db.commit()

    seed(1)
    small = run()
    seed(20)
    assert run() == small == 7


# --- API ------------------------------------------------------------------------------


def test_empty_organization_returns_an_explicit_empty_result(agent_client: TestClient, frozen_now) -> None:
    body = agent_client.get("/api/analytics/proactive-insights").json()
    assert body["total"] == 0 and body["items"] == []
    assert body["counts_by_category"] == dict.fromkeys(
        ["overdue_followup", "stalled_deal", "neglected_lead", "needs_attention"], 0
    )
    assert body["rules"]["stale_days"] == RULES["stale_days"]


def test_filters_pagination_and_validation(
    agent_client: TestClient, agent_sessions: sessionmaker[Session], frozen_now
) -> None:
    with agent_sessions() as db:
        for index in range(3):
            customer = _customer(db, f"Page {index}")
            enquiry = _enquiry(db, customer, created=_at(6, 1))
            _followup(db, customer, enquiry, _at(9, 1))
        quiet = _customer(db, "Quiet Co")
        _enquiry(db, quiet, created=_at(6, 1))
        db.commit()

    everything = agent_client.get("/api/analytics/proactive-insights").json()
    assert everything["total"] == 4
    page = agent_client.get("/api/analytics/proactive-insights", params={"page": 2, "page_size": 2}).json()
    assert [i["recommendation_id"] for i in page["items"]] == [i["recommendation_id"] for i in everything["items"][2:4]]
    overdue = agent_client.get("/api/analytics/proactive-insights", params={"category": "overdue_followup"}).json()
    assert overdue["total"] == 3 and overdue["counts_by_category"] == everything["counts_by_category"]
    top = everything["items"][0]["priority_score"]
    high = agent_client.get("/api/analytics/proactive-insights", params={"min_score": top}).json()
    assert high["total"] >= 1 and all(i["priority_score"] >= top for i in high["items"])
    urgency = everything["items"][0]["urgency"]
    by_urgency = agent_client.get("/api/analytics/proactive-insights", params={"urgency": urgency}).json()
    assert all(i["urgency"] == urgency for i in by_urgency["items"])
    for params in ({"category": "lost"}, {"urgency": "extreme"}, {"min_score": 101}, {"page_size": 101}, {"page": 0}):
        assert agent_client.get("/api/analytics/proactive-insights", params=params).status_code == 422


def test_database_failure_is_an_error_not_an_empty_result(
    agent_client: TestClient, monkeypatch: pytest.MonkeyPatch, frozen_now
) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(Session, "execute", broken)
    response = agent_client.get("/api/analytics/proactive-insights")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "database_error"
    assert "connection lost" not in response.text


def test_requires_authentication() -> None:
    from app.main import app

    with TestClient(app) as anonymous:
        assert anonymous.get("/api/analytics/proactive-insights").status_code == 401


# --- organization isolation -----------------------------------------------------------


@pytest.fixture
def rival(agent_sessions: sessionmaker[Session]) -> dict[str, int]:
    with agent_sessions() as db:
        organization = Organization(name="Rival Corp", slug="rival-corp")
        db.add(organization)
        db.flush()
        customer = _customer(db, "Rival Secret", organization_id=organization.organization_id)
        enquiry = _enquiry(db, customer, created=_at(6, 1), priority="urgent")
        followup = _followup(db, customer, enquiry, _at(9, 1))
        db.commit()
        return {
            "organization_id": organization.organization_id,
            "customer_id": customer.customer_id,
            "enquiry_id": enquiry.enquiry_id,
            "followup_id": followup.followup_id,
        }


def test_recommendations_and_links_never_cross_organizations(
    agent_client: TestClient, agent_sessions: sessionmaker[Session], rival: dict[str, int], frozen_now
) -> None:
    with agent_sessions() as db:
        own = _customer(db, "Own Co")
        own_enquiry = _enquiry(db, own, created=_at(6, 1))
        _followup(db, own, own_enquiry, _at(9, 1))
        db.commit()

    body = agent_client.get("/api/analytics/proactive-insights").json()
    assert [i["customer_id"] for i in body["items"]] == [own.customer_id]
    assert "Rival" not in json.dumps(body)
    assert sum(body["counts_by_category"].values()) == 1
    foreign = agent_client.get(
        "/api/analytics/proactive-insights", params={"customer_id": rival["customer_id"]}
    ).json()
    assert foreign["total"] == 0 and foreign["items"] == []
    # Every link stays inside the caller's organization, and the linked page loads.
    for link in body["items"][0]["links"]:
        customer_id = int(re.search(r"customer_id=(\d+)", link["href"]).group(1))
        assert customer_id == own.customer_id
        assert agent_client.get(f"/api/customers/{customer_id}/overview").status_code == 200
    assert agent_client.get(f"/api/customers/{rival['customer_id']}/overview").status_code == 404


def test_service_and_tools_fail_closed_without_organization() -> None:
    with pytest.raises(TypeError):
        get_proactive_insights(None, now=NOW)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="tenant context"):
        build_analytics_tools(None)


# --- agent --------------------------------------------------------------------------------


def _tool(db: Session, context: AgentToolContext):
    return {tool.name: tool for tool in build_read_only_tools(db, context)}["get_proactive_insights"]


def test_agent_tool_returns_the_services_recommendations_for_its_organization(
    agent_sessions: sessionmaker[Session], rival: dict[str, int], frozen_now
) -> None:
    with agent_sessions() as db:
        own = _customer(db, "Agent Co")
        own_enquiry = _enquiry(db, own, created=_at(6, 1))
        followup = _followup(db, own, own_enquiry, _at(9, 1))
        db.commit()
        context = AgentToolContext(organization_id=legacy_organization_id(db))
        data = json.loads(_tool(db, context).invoke({"category": "overdue_followup"}))["data"]
        expected = _insights(db, category="overdue_followup")

    assert [i["recommendation_id"] for i in data["items"]] == [i.recommendation_id for i in expected.items]
    item = data["items"][0]
    assert item["followup_id"] == followup.followup_id
    assert any(l["href"].endswith(f"#followup-{followup.followup_id}") for l in item["links"])
    assert "Rival" not in json.dumps(data)
    # Recommended records count as retrieved, so an approved action may reference them.
    assert context.has_followup(own.customer_id, followup.followup_id)
    assert not context.has_customer(rival["customer_id"])


def test_agent_tool_is_limited_to_the_conversations_customer(
    agent_sessions: sessionmaker[Session], frozen_now
) -> None:
    with agent_sessions() as db:
        first = _customer(db, "First Co")
        _followup(db, first, _enquiry(db, first, created=_at(6, 1)), _at(9, 1))
        second = _customer(db, "Second Co")
        _followup(db, second, _enquiry(db, second, created=_at(6, 1)), _at(9, 1))
        db.commit()
        context = AgentToolContext(organization_id=legacy_organization_id(db), locked_customer_id=first.customer_id)
        tool = _tool(db, context)
        scoped = json.loads(tool.invoke({}))["data"]
        refused = json.loads(tool.invoke({"customer_id": second.customer_id}))

    assert {i["customer_id"] for i in scoped["items"]} == {first.customer_id}
    assert refused["error"]["code"] == "customer_scope_violation"


def test_chat_answers_from_the_insights_tool(
    agent_client: TestClient, agent_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, frozen_now
) -> None:
    with agent_sessions() as db:
        customer = _customer(db, "Focus Co")
        enquiry = _enquiry(db, customer, created=_at(6, 1))
        _followup(db, customer, enquiry, _at(9, 1))
        db.commit()
    from app.services import proactive_insights_service

    served: list[Any] = []
    real = proactive_insights_service.get_proactive_insights

    def spy(*args: Any, **kwargs: Any) -> Any:
        served.append((kwargs["organization_id"], real(*args, **kwargs)))
        return served[-1][1]

    monkeypatch.setattr(proactive_insights_service, "get_proactive_insights", spy)
    _chat_model(
        monkeypatch,
        _tool_call("get_proactive_insights", {}),
        AIMessage(content=f"Focus on enquiry #{enquiry.enquiry_id}: its follow-up is overdue."),
    )
    response = agent_client.post("/api/agent/chat", json={"message": "What should I focus on today?"})

    assert response.status_code == 200, response.text
    calls = response.json()["tool_calls"]
    assert [(c["name"], c["success"]) for c in calls] == [("get_proactive_insights", True)]
    # The answer came from the service, run for the signed-in user's organization.
    with agent_sessions() as db:
        assert served[0][0] == legacy_organization_id(db)
    assert [i.enquiry_id for i in served[0][1].items] == [enquiry.enquiry_id]


def test_insights_tool_is_read_only_and_described_in_the_prompt() -> None:
    from app.agent.prompts import SYSTEM_PROMPT

    assert "get_proactive_insights" in SYSTEM_PROMPT
    assert "never rank, score or flag records yourself" in SYSTEM_PROMPT
    assert "missing_data" in SYSTEM_PROMPT


# --- frontend -----------------------------------------------------------------------------


def test_analytics_page_renders_recommendations_with_safe_links() -> None:
    from app.main import app

    with TestClient(app) as client:
        html = client.get("/analytics").text
        js = client.get("/static/js/analytics.js").text.replace("\r\n", "\n")
        customer_js = client.get("/static/js/customer.js").text.replace("\r\n", "\n")
        css = client.get("/static/css/analytics.css").text

    for element_id in ("insights", "insights-counts", "insights-filters", "insights-status", "insights-list", "insights-refresh"):
        assert f'id="{element_id}"' in html
    assert 'getAnalytics("proactive-insights"' in js
    assert "Unable to load recommendations" in js and "Nothing needs attention right now" in js
    # Links are rendered only when they open a record on its own customer's page.
    assert "function safeRecordHref" in js and "if (!href) continue;" in js
    for record in ("enquiry", "followup", "meeting"):
        assert f"card.id = `{record}-${{{record}.{record}_id}}`" in customer_js
    assert "The linked record was not found for this customer." in customer_js
    assert '.insight-card[data-urgency="high"]' in css
