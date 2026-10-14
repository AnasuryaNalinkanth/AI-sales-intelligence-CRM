import logging
from typing import TypeAlias

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.services.email_verification_service import verify_email
from app.core.exceptions import APIError
from app.core.logging_utils import log_database_exception
from app.database.connection import atomic_transaction, rollback_failed_transaction
from app.models import Company, Contact, Customer, SalesEnquiry
from app.services.activity_validation import owned
from app.schemas.customer import CustomerUpdate
from app.schemas.customer_registration import CustomerRegistrationCreate

logger = logging.getLogger(__name__)
RegistrationRecords: TypeAlias = tuple[Customer, Company, Contact, SalesEnquiry]


def _normalized_name(value: str) -> str:
    return value.strip().casefold()


def _is_duplicate_customer_integrity_error(exc: IntegrityError) -> bool:
    detail = str(getattr(exc, "orig", exc)).lower()
    return (
        "uq_customers_company_customer_name" in detail
        or ("customers.company_id" in detail and "customers.customer_name" in detail)
    )

# MANUAL EDIT #2 FOR EMAIL VALIDATION: Added email validation for primary contact in create_customer function////FEATURE 1
def create_customer(
    db: Session,
    registration: CustomerRegistrationCreate,
    *,
    organization_id: int,
) -> RegistrationRecords:
    if registration.primary_contact.email is not None:
        if not verify_email(str(registration.primary_contact.email)):
            raise APIError(
                "Email is not valid.",
                status_code=422,
                code="invalid_email",
            )

    try:
        with atomic_transaction(db):
            normalized_company_name = _normalized_name(registration.company.company_name)
            # Only this organization's companies: a same-named company elsewhere is a different one.
            company = db.scalar(
                select(Company)
                .where(
                    Company.organization_id == organization_id,
                    func.lower(func.trim(Company.company_name)) == normalized_company_name,
                )
                .order_by(Company.company_id)
                .limit(1)
            )
            if company is None:
                company = Company(
                    organization_id=organization_id,
                    company_name=registration.company.company_name.strip(),
                    industry=registration.company.industry,
                    website=registration.company.website,
                    address=registration.company.address,
                    city=registration.company.city,
                    country=registration.company.country,
                    company_size=registration.company.company_size,
                    description=registration.company.description,
                )
                db.add(company)
                db.flush()

            normalized_customer_name = _normalized_name(registration.customer_name)
            duplicate_customer = db.scalar(
                select(Customer.customer_id)
                .where(
                    Customer.company_id == company.company_id,
                    func.lower(func.trim(Customer.customer_name)) == normalized_customer_name,
                )
                .limit(1)
            )
            if duplicate_customer is not None:
                raise APIError(
                    "A customer with this name is already registered under the company.",
                    status_code=409,
                    code="duplicate_customer",
                )

            customer = Customer(
                organization_id=organization_id,
                company=company,
                customer_name=registration.customer_name.strip(),
                status=registration.status,
                sales_stage=registration.sales_stage,
            )
            db.add(customer)
            db.flush()

            contact = Contact(
                organization_id=organization_id,
                customer=customer,
                name=registration.primary_contact.name,
                job_title=registration.primary_contact.job_title,
                email=(
                    str(registration.primary_contact.email)
                    if registration.primary_contact.email is not None
                    else None
                ),
                phone=registration.primary_contact.phone,
                is_primary=True,
            )
            db.add(contact)
            db.flush()

            sales_enquiry = SalesEnquiry(
                organization_id=organization_id,
                customer=customer,
                product=registration.sales_enquiry.product,
                enquiry_text=registration.sales_enquiry.enquiry_text,
                priority=registration.sales_enquiry.priority,
                status=registration.sales_enquiry.status,
                estimated_value=registration.sales_enquiry.estimated_value,
                lead_source=registration.sales_enquiry.lead_source,
                contact=contact,
            )
            db.add(sales_enquiry)
            db.flush()

            for record in (company, customer, contact, sales_enquiry):
                db.refresh(record)

        return customer, company, contact, sales_enquiry
    except APIError:
        raise
    except IntegrityError as exc:
        rollback_failed_transaction(db)
        log_database_exception(logger, "create_customer", exc, conflict=True)
        if _is_duplicate_customer_integrity_error(exc):
            raise APIError(
                "A customer with this name is already registered under the company.",
                status_code=409,
                code="duplicate_customer",
            ) from exc
        raise APIError(
            "Registration conflicts with existing CRM data.",
            status_code=409,
            code="registration_conflict",
        ) from exc
    except SQLAlchemyError as exc:
        rollback_failed_transaction(db)
        log_database_exception(logger, "create_customer", exc)
        raise APIError(
            "The database operation could not be completed.",
            status_code=500,
            code="database_error",
        ) from exc


def get_customer(db: Session, customer_id: int, *, organization_id: int) -> Customer:
    try:
        customer = db.scalar(
            select(Customer)
            .where(
                Customer.customer_id == customer_id,
                Customer.organization_id == organization_id,
            )
        )

        if customer is None:
            raise APIError(
                "Customer not found.",
                status_code=404,
                code="customer_not_found",
            )

        # Force each relationship individually so we can identify
        # which ORM load is failing.
        _ = customer.company
        _ = customer.contacts
        _ = customer.sales_enquiries

        return customer

    except APIError:
        raise

    except Exception as exc:
        logger.exception(
            "Customer ORM load failed [customer_id=%s error_type=%s]",
            customer_id,
            type(exc).__name__,
        )
        raise


def update_customer(
    db: Session,
    customer_id: int,
    data: CustomerUpdate,
    *,
    organization_id: int,
) -> Customer:
    try:
        with atomic_transaction(db):
            customer = owned(db, Customer, customer_id, organization_id=organization_id)
            if customer is None:
                raise APIError("Customer not found.", status_code=404, code="customer_not_found")

            updates = data.model_dump(exclude_unset=True)
            target_company_id = updates.get("company_id", customer.company_id)
            if "company_id" in updates:
                company = owned(db, Company, target_company_id, organization_id=organization_id)
                if company is None:
                    raise APIError("Company not found.", status_code=404, code="company_not_found")

            target_customer_name = updates.get("customer_name", customer.customer_name)
            if "customer_name" in updates or "company_id" in updates:
                normalized_name = _normalized_name(target_customer_name)
                duplicate_id = db.scalar(
                    select(Customer.customer_id)
                    .where(
                        Customer.customer_id != customer_id,
                        Customer.company_id == target_company_id,
                        func.lower(func.trim(Customer.customer_name)) == normalized_name,
                    )
                    .limit(1)
                )
                if duplicate_id is not None:
                    raise APIError(
                        "A customer with this name is already registered under the company.",
                        status_code=409,
                        code="duplicate_customer",
                    )

            for field, value in updates.items():
                setattr(customer, field, value)
            db.flush()
            db.refresh(customer)
        return customer
    except APIError:
        raise
    except IntegrityError as exc:
        rollback_failed_transaction(db)
        log_database_exception(logger, "update_customer", exc, conflict=True)
        if _is_duplicate_customer_integrity_error(exc):
            raise APIError(
                "A customer with this name is already registered under the company.",
                status_code=409,
                code="duplicate_customer",
            ) from exc
        raise APIError(
            "Customer update conflicts with existing CRM data.",
            status_code=409,
            code="customer_conflict",
        ) from exc
    except SQLAlchemyError as exc:
        rollback_failed_transaction(db)
        log_database_exception(logger, "update_customer", exc)
        raise APIError(
            "The database operation could not be completed.",
            status_code=500,
            code="database_error",
        ) from exc


def list_customers(
    db: Session,
    *,
    organization_id: int,
    page: int,
    page_size: int,
    search: str | None = None,
    customer_name: str | None = None,
    company_name: str | None = None,
) -> tuple[list[Customer], int]:
    filters = [Customer.organization_id == organization_id]
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                Customer.customer_name.ilike(pattern),
                Company.company_name.ilike(pattern),
            )
        )
    if customer_name and customer_name.strip():
        filters.append(Customer.customer_name.ilike(f"%{customer_name.strip()}%"))
    if company_name and company_name.strip():
        filters.append(Company.company_name.ilike(f"%{company_name.strip()}%"))

    try:
        count_statement = (
            select(func.count(Customer.customer_id))
            .join(Company, Customer.company_id == Company.company_id)
            .where(*filters)
        )
        total = db.scalar(count_statement) or 0
        statement = (
            select(Customer)
            .options(joinedload(Customer.company))
            .join(Company, Customer.company_id == Company.company_id)
            .where(*filters)
            .order_by(Customer.customer_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        customers = list(db.scalars(statement).all())
    except SQLAlchemyError as exc:
        rollback_failed_transaction(db)
        log_database_exception(logger, "list_customers", exc)
        raise APIError(
            "The database operation could not be completed.",
            status_code=500,
            code="database_error",
        ) from exc
    return customers, total
