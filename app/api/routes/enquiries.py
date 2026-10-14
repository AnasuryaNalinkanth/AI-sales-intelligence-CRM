"""Sales lead routes. The /enquiries paths are kept so existing integrations keep working."""

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.database.connection import get_db
from app.models import User
from app.schemas.sales_enquiry import (
    LeadAssignee,
    LeadQualificationUpdate,
    OpportunityClose,
    QualificationStatus,
    SalesEnquiryResponse,
    SalesEnquiryUpdate,
    SalesLeadCreate,
    SalesLeadListItem,
    SalesLeadListResponse,
)
from app.services import enquiry_service

router = APIRouter(prefix="/enquiries", tags=["sales leads"])
customer_router = APIRouter(prefix="/customers", tags=["customer sales leads"])


@router.get("/test")
def test_enquiries() -> dict[str, str]:
    return {"status": "ok", "module": "enquiries"}


@router.get(
    "",
    response_model=SalesLeadListResponse,
    summary="List sales leads",
)
def list_leads(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    qualification_status: QualificationStatus | None = Query(default=None),
    outcome: Literal["open", "won", "lost"] | None = Query(
        default=None, description="open = not yet closed; won/lost = closed opportunities."
    ),
    customer_id: int | None = Query(default=None, ge=1),
    assigned_user_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SalesLeadListResponse:
    rows, total = enquiry_service.list_leads(
        db,
        organization_id=current_user.organization_id,
        page=page,
        page_size=page_size,
        search=search,
        qualification_status=qualification_status,
        outcome=outcome,
        customer_id=customer_id,
        assigned_user_id=assigned_user_id,
    )
    return SalesLeadListResponse(
        items=[
            SalesLeadListItem.model_validate(
                {
                    **SalesEnquiryResponse.model_validate(row.pop("enquiry")).model_dump(),
                    **row,
                }
            )
            for row in rows
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/assignees",
    response_model=list[LeadAssignee],
    summary="List sales representatives a lead can be assigned to",
)
def list_assignees(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LeadAssignee]:
    return [
        LeadAssignee.model_validate(user)
        for user in enquiry_service.list_assignees(
            db, organization_id=current_user.organization_id
        )
    ]


@router.post(
    "",
    response_model=SalesEnquiryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a sales lead",
)
def create_enquiry(
    data: SalesLeadCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SalesEnquiryResponse:
    return SalesEnquiryResponse.model_validate(
        enquiry_service.create_enquiry(db, data, organization_id=current_user.organization_id)
    )


@router.get(
    "/{enquiry_id}",
    response_model=SalesEnquiryResponse,
    summary="Get a sales lead",
)
def get_enquiry(
    enquiry_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SalesEnquiryResponse:
    return SalesEnquiryResponse.model_validate(
        enquiry_service.get_enquiry(db, enquiry_id, organization_id=current_user.organization_id)
    )


@router.put(
    "/{enquiry_id}",
    response_model=SalesEnquiryResponse,
    summary="Update a sales lead",
)
@router.patch(
    "/{enquiry_id}",
    response_model=SalesEnquiryResponse,
    summary="Partially update a sales lead (closed opportunities are read-only)",
)
def update_enquiry(
    data: SalesEnquiryUpdate,
    enquiry_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SalesEnquiryResponse:
    return SalesEnquiryResponse.model_validate(
        enquiry_service.update_enquiry(
            db, enquiry_id, data, organization_id=current_user.organization_id
        )
    )


@router.post(
    "/{enquiry_id}/qualification",
    response_model=SalesEnquiryResponse,
    summary="Set a sales lead's qualification (Ready / Not Ready)",
)
def update_qualification(
    data: LeadQualificationUpdate,
    enquiry_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SalesEnquiryResponse:
    return SalesEnquiryResponse.model_validate(
        enquiry_service.update_qualification(
            db, enquiry_id, data, organization_id=current_user.organization_id
        )
    )


@router.post(
    "/{enquiry_id}/close",
    response_model=SalesEnquiryResponse,
    summary="Close an active opportunity as Won or Lost",
)
def close_opportunity(
    data: OpportunityClose,
    enquiry_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SalesEnquiryResponse:
    return SalesEnquiryResponse.model_validate(
        enquiry_service.close_opportunity(
            db, enquiry_id, data, organization_id=current_user.organization_id
        )
    )


@customer_router.get(
    "/{customer_id}/enquiries",
    response_model=list[SalesEnquiryResponse],
    summary="List a customer's sales leads",
)
def list_customer_enquiries(
    customer_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[SalesEnquiryResponse]:
    return [
        SalesEnquiryResponse.model_validate(item)
        for item in enquiry_service.list_customer_enquiries(
            db, customer_id, organization_id=current_user.organization_id
        )
    ]
