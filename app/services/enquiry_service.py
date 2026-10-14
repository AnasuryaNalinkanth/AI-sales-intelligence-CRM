"""Sales leads (stored as sales enquiries): capture, qualification, and opportunity closure."""

import logging
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import APIError
from app.core.logging_utils import log_database_exception
from app.database.connection import atomic_transaction, rollback_failed_transaction
from app.models import Company, Contact, Customer, SalesEnquiry, User
from app.schemas.follow_up import FollowUpCreate
from app.schemas.sales_enquiry import (
    LeadQualificationUpdate,
    OpportunityClose,
    OpportunityOutcome,
    QualificationStatus,
    SalesEnquiryCreate,
    SalesEnquiryUpdate,
)
from app.services import followup_service
from app.services.activity_validation import (
    owned,
    require_customer,
    validate_customer_references,
    validate_enquiry_reassignment,
)
from app.services.scheduling_service import lock_workspace_schedule

logger = logging.getLogger(__name__)

# Closing moves the pipeline stage to the matching legacy terminal value, so open-lead
# counts, dashboards, and insights that read the stage stay consistent.
OUTCOME_STAGE: dict[str, str] = {"won": "converted", "lost": "lost"}


def ensure_lead_open(enquiry: SalesEnquiry) -> None:
    """Closed opportunities are history: ordinary edits must not change or reopen them."""
    if enquiry.outcome is not None:
        raise APIError(
            f"This opportunity is closed as {enquiry.outcome.title()} and cannot be changed.",
            409,
            "opportunity_closed",
        )


def _validate_lead_links(
    db: Session,
    *,
    organization_id: int,
    customer_id: int,
    contact_id: int | None,
    assigned_user_id: int | None,
) -> None:
    validate_customer_references(
        db, customer_id, organization_id=organization_id, contact_id=contact_id
    )
    if assigned_user_id is not None:
        assignee = db.scalar(
            select(User).where(
                User.user_id == assigned_user_id,
                User.organization_id == organization_id,
                User.is_active.is_(True),
            )
        )
        if assignee is None:
            raise APIError(
                "The assigned sales representative was not found.",
                404,
                "assignee_not_found",
            )


def _write_error(operation: str, exc: SQLAlchemyError) -> APIError:
    conflict = isinstance(exc, IntegrityError)
    log_database_exception(logger, operation, exc, conflict=conflict)
    if conflict:
        return APIError("Sales lead references conflict with CRM data.", 409, "enquiry_conflict")
    return APIError("The database operation could not be completed.", 500, "database_error")


def create_enquiry(
    db: Session, data: SalesEnquiryCreate, *, organization_id: int
) -> SalesEnquiry:
    try:
        with atomic_transaction(db):
            _validate_lead_links(
                db,
                organization_id=organization_id,
                customer_id=data.customer_id,
                contact_id=data.contact_id,
                assigned_user_id=data.assigned_user_id,
            )
            enquiry = SalesEnquiry(**data.model_dump(), organization_id=organization_id)
            db.add(enquiry)
            db.flush()
            db.refresh(enquiry)
        return enquiry
    except APIError:
        raise
    except SQLAlchemyError as exc:
        raise _write_error("create_enquiry", exc) from exc


def get_enquiry(db: Session, enquiry_id: int, *, organization_id: int) -> SalesEnquiry:
    try:
        enquiry = owned(db, SalesEnquiry, enquiry_id, organization_id=organization_id)
    except SQLAlchemyError as exc:
        log_database_exception(logger, "get_enquiry", exc)
        raise APIError(
            "The database operation could not be completed.",
            500,
            "database_error",
        ) from exc
    if enquiry is None:
        raise APIError("Sales lead not found.", 404, "enquiry_not_found")
    return enquiry


def update_enquiry(
    db: Session, enquiry_id: int, data: SalesEnquiryUpdate, *, organization_id: int
) -> SalesEnquiry:
    try:
        with atomic_transaction(db):
            if data.customer_id is not None:
                lock_workspace_schedule(db, organization_id)
            enquiry = owned(
                db, SalesEnquiry, enquiry_id, organization_id=organization_id, for_update=True
            )
            if enquiry is None:
                raise APIError("Sales lead not found.", 404, "enquiry_not_found")
            ensure_lead_open(enquiry)
            updates = data.model_dump(exclude_unset=True)
            target_customer_id = updates.get("customer_id", enquiry.customer_id)
            _validate_lead_links(
                db,
                organization_id=organization_id,
                customer_id=target_customer_id,
                contact_id=updates.get("contact_id", enquiry.contact_id),
                assigned_user_id=updates.get("assigned_user_id"),
            )
            if target_customer_id != enquiry.customer_id:
                validate_enquiry_reassignment(db, enquiry.enquiry_id)
            for field, value in updates.items():
                setattr(enquiry, field, value)
            db.flush()
            db.refresh(enquiry)
        return enquiry
    except APIError:
        raise
    except SQLAlchemyError as exc:
        raise _write_error("update_enquiry", exc) from exc


def _lock_open_lead(
    db: Session, enquiry_id: int, *, organization_id: int, customer_id: int | None
) -> SalesEnquiry:
    enquiry = owned(db, SalesEnquiry, enquiry_id, organization_id=organization_id, for_update=True)
    if enquiry is None or (customer_id is not None and enquiry.customer_id != customer_id):
        raise APIError("Sales lead not found.", 404, "enquiry_not_found")
    ensure_lead_open(enquiry)
    return enquiry


def update_qualification(
    db: Session,
    enquiry_id: int,
    data: LeadQualificationUpdate,
    *,
    organization_id: int,
    customer_id: int | None = None,
    defer_commit: bool = False,
) -> SalesEnquiry:
    """Record a human qualification decision; Not Ready may schedule the next follow-up."""
    try:
        with atomic_transaction(db, commit_existing=not defer_commit):
            if data.next_followup is not None:
                # Same lock order as other schedule writers: workspace first, then the lead.
                lock_workspace_schedule(db, organization_id)
            enquiry = _lock_open_lead(
                db, enquiry_id, organization_id=organization_id, customer_id=customer_id
            )
            enquiry.qualification_status = data.qualification_status
            enquiry.qualification_reason = data.qualification_reason
            if data.next_followup is not None:
                followup_service.create_followup(
                    db,
                    FollowUpCreate(
                        customer_id=enquiry.customer_id,
                        enquiry_id=enquiry.enquiry_id,
                        **data.next_followup.model_dump(),
                    ),
                    organization_id=organization_id,
                    defer_commit=True,
                )
            db.flush()
            db.refresh(enquiry)
        return enquiry
    except APIError:
        raise
    except SQLAlchemyError as exc:
        raise _write_error("update_qualification", exc) from exc


def close_opportunity(
    db: Session,
    enquiry_id: int,
    data: OpportunityClose,
    *,
    organization_id: int,
    customer_id: int | None = None,
    defer_commit: bool = False,
) -> SalesEnquiry:
    """Close an active opportunity (an open, Ready lead) as Won or Lost. There is no reopen."""
    try:
        with atomic_transaction(db, commit_existing=not defer_commit):
            enquiry = _lock_open_lead(
                db, enquiry_id, organization_id=organization_id, customer_id=customer_id
            )
            if enquiry.qualification_status != "ready":
                raise APIError(
                    "Only a lead qualified as Ready can be closed as Won or Lost.",
                    409,
                    "opportunity_not_active",
                )
            enquiry.outcome = data.outcome
            enquiry.loss_reason = data.loss_reason if data.outcome == "lost" else None
            enquiry.closed_at = datetime.now(timezone.utc)
            enquiry.status = OUTCOME_STAGE[data.outcome]
            db.flush()
            db.refresh(enquiry)
        return enquiry
    except APIError:
        raise
    except SQLAlchemyError as exc:
        raise _write_error("close_opportunity", exc) from exc


def list_customer_enquiries(
    db: Session, customer_id: int, *, organization_id: int
) -> list[SalesEnquiry]:
    try:
        require_customer(db, customer_id, organization_id=organization_id)
        return list(
            db.scalars(
                select(SalesEnquiry)
                .where(
                    SalesEnquiry.customer_id == customer_id,
                    SalesEnquiry.organization_id == organization_id,
                )
                .order_by(SalesEnquiry.created_at.desc(), SalesEnquiry.enquiry_id.desc())
            ).all()
        )
    except APIError:
        raise
    except SQLAlchemyError as exc:
        log_database_exception(logger, "list_customer_enquiries", exc)
        raise APIError(
            "The database operation could not be completed.",
            500,
            "database_error",
        ) from exc


def list_leads(
    db: Session,
    *,
    organization_id: int,
    page: int,
    page_size: int,
    search: str | None = None,
    qualification_status: QualificationStatus | None = None,
    outcome: OpportunityOutcome | Literal["open"] | None = None,
    customer_id: int | None = None,
    assigned_user_id: int | None = None,
) -> tuple[list[dict[str, object]], int]:
    """One page of leads with their customer, company, contact, and assignee names."""
    filters = [SalesEnquiry.organization_id == organization_id]
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                SalesEnquiry.product.ilike(pattern),
                SalesEnquiry.enquiry_text.ilike(pattern),
                Customer.customer_name.ilike(pattern),
                Company.company_name.ilike(pattern),
            )
        )
    if qualification_status is not None:
        filters.append(SalesEnquiry.qualification_status == qualification_status)
    if outcome == "open":
        filters.append(SalesEnquiry.outcome.is_(None))
    elif outcome is not None:
        filters.append(SalesEnquiry.outcome == outcome)
    if customer_id is not None:
        filters.append(SalesEnquiry.customer_id == customer_id)
    if assigned_user_id is not None:
        filters.append(SalesEnquiry.assigned_user_id == assigned_user_id)

    def scoped(statement):
        return (
            statement.join(Customer, SalesEnquiry.customer_id == Customer.customer_id)
            .join(Company, Customer.company_id == Company.company_id)
            .where(*filters)
        )

    try:
        total = db.scalar(scoped(select(func.count(SalesEnquiry.enquiry_id)))) or 0
        rows = db.execute(
            scoped(
                select(
                    SalesEnquiry,
                    Customer.customer_name,
                    Company.company_name,
                    Contact.name,
                    User.full_name,
                ).select_from(SalesEnquiry)
            )
            .outerjoin(Contact, SalesEnquiry.contact_id == Contact.contact_id)
            .outerjoin(User, SalesEnquiry.assigned_user_id == User.user_id)
            .order_by(SalesEnquiry.updated_at.desc(), SalesEnquiry.enquiry_id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    except SQLAlchemyError as exc:
        rollback_failed_transaction(db)
        log_database_exception(logger, "list_leads", exc)
        raise APIError(
            "The database operation could not be completed.", 500, "database_error"
        ) from exc
    return [
        {
            "enquiry": enquiry,
            "customer_name": customer_name,
            "company_name": company_name,
            "contact_name": contact_name,
            "assigned_user_name": assigned_user_name,
        }
        for enquiry, customer_name, company_name, contact_name, assigned_user_name in rows
    ], total


def list_assignees(db: Session, *, organization_id: int) -> list[User]:
    try:
        return list(
            db.scalars(
                select(User)
                .where(User.organization_id == organization_id, User.is_active.is_(True))
                .order_by(User.full_name, User.user_id)
            ).all()
        )
    except SQLAlchemyError as exc:
        rollback_failed_transaction(db)
        log_database_exception(logger, "list_assignees", exc)
        raise APIError(
            "The database operation could not be completed.", 500, "database_error"
        ) from exc
