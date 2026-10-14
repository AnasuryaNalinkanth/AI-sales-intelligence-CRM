from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import inspect
from sqlalchemy.orm import configure_mappers

from app.database.connection import Base
from app.models import (
    Call,
    Company,
    Contact,
    Customer,
    FollowUp,
    HitlActionAudit,
    HitlActionProposal,
    HitlApprovalDecision,
    HitlExecutionAudit,
    HitlPolicyAudit,
    HitlPolicyOverride,
    HitlTask,
    Meeting,
    SalesEnquiry,
    SchedulingLock,
    User,
)
from app.schemas import (
    CallCreate,
    CallResponse,
    CompanyCreate,
    CompanyResponse,
    ContactCreate,
    ContactResponse,
    CustomerCreate,
    CustomerResponse,
    FollowUpCreate,
    FollowUpResponse,
    MeetingCreate,
    MeetingResponse,
    SalesEnquiryCreate,
    SalesEnquiryResponse,
)


def test_all_models_register_expected_tables() -> None:
    expected_columns = {
        "organizations": {
            "organization_id", "name", "slug", "status", "created_at", "updated_at",
        },
        "users": {
            "user_id", "email", "full_name", "password_hash", "role",
            "is_active", "organization_id", "email_verified_at",
            "verification_grace_until", "created_at", "updated_at",
        },
        "companies": {
            "organization_id", "company_id", "company_name", "industry", "website", "address", "city",
            "country", "company_size", "description", "created_at", "updated_at",
        },
        "customers": {
            "organization_id", "customer_id", "company_id", "customer_name", "status", "sales_stage",
            "created_at", "updated_at",
        },
        "contacts": {
            "organization_id", "contact_id", "customer_id", "name", "job_title", "email", "phone",
            "is_primary", "created_at", "updated_at",
        },
        "sales_enquiries": {
            "organization_id", "enquiry_id", "customer_id", "product", "enquiry_text", "priority", "status",
            "estimated_value", "created_at", "updated_at", "contact_id", "assigned_user_id",
            "lead_source", "expected_close_date", "qualification_status", "qualification_reason",
            "outcome", "closed_at", "loss_reason",
        },
        "meetings": {
            "organization_id", "meeting_id", "customer_id", "contact_id", "enquiry_id", "scheduled_at",
            "scheduled_timezone", "duration", "status", "agenda", "notes", "summary",
            "created_at", "updated_at",
        },
        "calls": {
            "organization_id", "call_id", "customer_id", "contact_id", "enquiry_id", "call_type", "scheduled_at",
            "actual_time", "status", "outcome", "notes", "summary", "next_followup_date",
            "scheduled_timezone", "duration", "created_at", "updated_at",
        },
        "follow_ups": {
            "organization_id", "followup_id", "customer_id", "enquiry_id", "meeting_id", "call_id", "type",
            "due_date", "status", "description", "assigned_to", "completed_at", "created_at",
            "due_timezone", "duration", "updated_at",
        },
        "scheduling_locks": {
            "lock_id",
        },
        "hitl_tasks": {
            "task_id", "owner_user_id", "action_type", "collected_data",
            "cancelled_by_user_id", "cancelled_at", "status", "created_at",
            "updated_at", "expires_at",
        },
        "hitl_action_proposals": {
            "action_id", "task_id", "owner_user_id", "action_type", "parameters",
            "payload_fingerprint", "status", "approved_by_user_id", "approved_at",
            "created_at", "updated_at", "expires_at",
        },
        "hitl_approval_decisions": {
            "decision_id", "action_id", "decided_by_user_id", "decision", "decided_at",
        },
        "hitl_execution_audits": {
            "execution_id", "action_id", "initiated_by_user_id", "status", "result_record_id",
            "error_code", "result_message", "started_at", "completed_at",
        },
        "hitl_policy_overrides": {
            "organization_id", "action_type", "mode", "updated_by_user_id", "updated_at",
        },
        "hitl_policy_audits": {
            "organization_id", "audit_id", "action_type", "previous_mode", "new_mode", "changed_by_user_id",
            "changed_at",
        },
        "hitl_action_audits": {
            "audit_id", "action_id", "actor_user_id", "event_type", "details", "created_at",
        },
        "google_calendar_connections": {
            "user_id", "encrypted_refresh_token", "encrypted_access_token",
            "access_token_expires_at", "scope", "status", "reminder_minutes",
            "connected_at", "updated_at",
        },
        "agent_conversations": {
            "conversation_id", "owner_user_id", "customer_id", "created_at", "updated_at",
        },
        "agent_conversation_messages": {
            "message_id", "conversation_id", "role", "content", "created_at",
        },
        "calendar_event_links": {
            "link_id", "user_id", "activity_type", "activity_id", "google_event_id",
            "sync_status", "last_error", "attempts", "synced_at", "created_at", "updated_at",
        },
    }

    assert set(Base.metadata.tables) == set(expected_columns)
    for table_name, columns in expected_columns.items():
        assert set(Base.metadata.tables[table_name].columns.keys()) == columns
    assert all(model.__table__ in Base.metadata.sorted_tables for model in (
        User, Company, Customer, Contact, SalesEnquiry, Meeting, Call, FollowUp,
        HitlTask, HitlActionProposal, HitlApprovalDecision, HitlExecutionAudit,
        HitlPolicyOverride, HitlPolicyAudit, HitlActionAudit,
        SchedulingLock,
    ))


def test_foreign_keys_and_history_preserving_delete_rules() -> None:
    expected_foreign_keys = {
        "customers": {"company_id"},
        "contacts": {"customer_id"},
        "sales_enquiries": {"customer_id", "contact_id", "assigned_user_id"},
        "meetings": {"customer_id", "contact_id", "enquiry_id"},
        "calls": {"customer_id", "contact_id", "enquiry_id"},
        "follow_ups": {"customer_id", "enquiry_id", "meeting_id", "call_id"},
    }
    for table_name, columns in expected_foreign_keys.items():
        table = Base.metadata.tables[table_name]
        assert columns <= {foreign_key.parent.name for foreign_key in table.foreign_keys}

    customer_company_fk = next(iter(Base.metadata.tables["customers"].c.company_id.foreign_keys))
    assert customer_company_fk.ondelete == "RESTRICT"
    for table_name in ("contacts", "sales_enquiries", "meetings", "calls", "follow_ups"):
        customer_fk = next(
            foreign_key
            for foreign_key in Base.metadata.tables[table_name].foreign_keys
            if foreign_key.parent.name == "customer_id"
        )
        assert customer_fk.ondelete == "RESTRICT"
    for table_name in ("meetings", "calls", "follow_ups"):
        for foreign_key in Base.metadata.tables[table_name].foreign_keys:
            # organization_id is part of the composite tenant key to customers.
            if foreign_key.parent.name not in {"customer_id", "organization_id"}:
                assert foreign_key.ondelete == "SET NULL"


def test_relationships_are_configured_without_delete_cascade() -> None:
    configure_mappers()
    customer_relationships = inspect(Customer).relationships

    assert customer_relationships["company"].mapper.class_ is Company
    assert customer_relationships["contacts"].mapper.class_ is Contact
    assert customer_relationships["sales_enquiries"].mapper.class_ is SalesEnquiry
    assert customer_relationships["meetings"].mapper.class_ is Meeting
    assert customer_relationships["calls"].mapper.class_ is Call
    assert customer_relationships["followups"].mapper.class_ is FollowUp
    assert not customer_relationships["contacts"].cascade.delete
    assert not inspect(Company).relationships["customers"].cascade.delete
    assert inspect(Meeting).relationships["enquiry"].direction.name == "MANYTOONE"
    assert inspect(Call).relationships["contact"].direction.name == "MANYTOONE"


def test_create_schemas_validate_representative_data() -> None:
    now = datetime(2026, 10, 6, 10, 0, 0)
    assert CompanyCreate(company_name="Northwind").company_name == "Northwind"
    assert CustomerCreate(company_id=1, customer_name="Customer").company_id == 1
    assert ContactCreate(customer_id=1, name="A. Contact").is_primary is False
    enquiry = SalesEnquiryCreate(
        customer_id=1,
        enquiry_text="Interested in an annual plan",
        estimated_value=Decimal("1250.50"),
    )
    assert enquiry.estimated_value == Decimal("1250.50")
    assert (
        MeetingCreate(
            customer_id=1,
            scheduled_at=now.replace(tzinfo=timezone.utc),
        ).enquiry_id
        is None
    )
    assert CallCreate(customer_id=1, actual_time=now).status == "scheduled"
    followup = FollowUpCreate(customer_id=1, type="email", due_date=now)
    assert followup.meeting_id is None


def test_response_schemas_read_sqlalchemy_model_attributes() -> None:
    now = datetime(2026, 10, 6, 10, 0, 0)
    company = Company(company_id=1, company_name="Northwind", created_at=now, updated_at=now)
    customer = Customer(
        customer_id=1,
        company_id=1,
        customer_name="Customer",
        status="active",
        sales_stage="new",
        created_at=now,
        updated_at=now,
    )
    contact = Contact(
        contact_id=1,
        customer_id=1,
        name="A. Contact",
        is_primary=True,
        created_at=now,
        updated_at=now,
    )
    enquiry = SalesEnquiry(
        enquiry_id=1,
        customer_id=1,
        enquiry_text="Interested in an annual plan",
        priority="normal",
        status="open",
        qualification_status="not_ready",
        estimated_value=Decimal("1250.50"),
        created_at=now,
        updated_at=now,
    )
    meeting = Meeting(
        meeting_id=1,
        customer_id=1,
        scheduled_at=now,
        status="scheduled",
        created_at=now,
        updated_at=now,
    )
    call = Call(
        call_id=1,
        customer_id=1,
        status="scheduled",
        created_at=now,
        updated_at=now,
    )
    followup = FollowUp(
        followup_id=1,
        customer_id=1,
        type="email",
        due_date=now,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    responses = (
        CompanyResponse.model_validate(company),
        CustomerResponse.model_validate(customer),
        ContactResponse.model_validate(contact),
        SalesEnquiryResponse.model_validate(enquiry),
        MeetingResponse.model_validate(meeting),
        CallResponse.model_validate(call),
        FollowUpResponse.model_validate(followup),
    )
    assert len(responses) == 7
