import logging

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import APIError
from app.core.logging_utils import log_database_exception
from app.database.connection import atomic_transaction, rollback_failed_transaction
from app.models import Customer, SalesEnquiry
from app.schemas.customer import CustomerUpdate
from app.schemas.sales_enquiry import SalesEnquiryUpdate
from app.services.activity_validation import owned
from app.services.enquiry_service import ensure_lead_open

logger = logging.getLogger(__name__)


def apply_enrichment(
    db: Session,
    *,
    customer_id: int,
    organization_id: int,
    enquiry_id: int | None = None,
    customer_updates: dict[str, object] | None = None,
    enquiry_updates: dict[str, object] | None = None,
    defer_commit: bool = False,
) -> tuple[Customer | None, SalesEnquiry | None]:
    customer_updates = customer_updates or {}
    enquiry_updates = enquiry_updates or {}

    if not customer_updates and not enquiry_updates:
        raise APIError(
            "No CRM changes were proposed.",
            status_code=422,
            code="empty_enrichment",
        )

    try:
        with atomic_transaction(db, commit_existing=not defer_commit):
            customer = owned(db, Customer, customer_id, organization_id=organization_id)

            if customer is None:
                raise APIError(
                    "Customer not found.",
                    status_code=404,
                    code="customer_not_found",
                )

            enquiry = None

            if enquiry_updates:
                if enquiry_id is None:
                    raise APIError(
                        "Enquiry ID is required for enquiry enrichment.",
                        status_code=422,
                        code="enquiry_id_required",
                    )

                enquiry = owned(
                    db, SalesEnquiry, enquiry_id, organization_id=organization_id, for_update=True
                )

                if enquiry is None:
                    raise APIError(
                        "Sales enquiry not found.",
                        status_code=404,
                        code="enquiry_not_found",
                    )

                if enquiry.customer_id != customer_id:
                    raise APIError(
                        "Sales enquiry does not belong to this customer.",
                        status_code=400,
                        code="enquiry_customer_mismatch",
                    )
                ensure_lead_open(enquiry)

            if customer_updates:
                validated_customer = CustomerUpdate.model_validate(
                    customer_updates
                )

                for field, value in validated_customer.model_dump(
                    exclude_unset=True
                ).items():
                    setattr(customer, field, value)

            if enquiry_updates and enquiry is not None:
                validated_enquiry = SalesEnquiryUpdate.model_validate(
                    enquiry_updates
                )

                for field, value in validated_enquiry.model_dump(
                    exclude_unset=True
                ).items():
                    setattr(enquiry, field, value)

            db.flush()

            db.refresh(customer)

            if enquiry is not None:
                db.refresh(enquiry)

        return customer, enquiry

    except APIError:
        raise

    except IntegrityError as exc:
        rollback_failed_transaction(db)
        log_database_exception(
            logger,
            "apply_enrichment",
            exc,
            conflict=True,
        )
        raise APIError(
            "CRM enrichment conflicts with existing data.",
            status_code=409,
            code="enrichment_conflict",
        ) from exc

    except SQLAlchemyError as exc:
        rollback_failed_transaction(db)
        log_database_exception(
            logger,
            "apply_enrichment",
            exc,
        )
        raise APIError(
            "The database operation could not be completed.",
            status_code=500,
            code="database_error",
        ) from exc