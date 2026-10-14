from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.company import CompanyResponse
from app.schemas.contact import ContactResponse
from app.schemas.customer import CustomerResponse
from app.schemas.sales_enquiry import (
    EnquiryPriority,
    EnquiryStatus,
    LeadSource,
    SalesEnquiryResponse,
)
from app.schemas.validators import (
    MAX_MONETARY_VALUE,
    MAX_TEXT_LENGTH,
    validate_monetary_decimal,
)

CustomerStatus = Literal["active", "inactive", "prospect"]
SalesStage = Literal["new", "qualified", "proposal", "negotiation", "won", "lost"]
class RegistrationInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class RegistrationCompany(RegistrationInput):
    company_name: str = Field(min_length=1, max_length=255)
    industry: str | None = Field(default=None, max_length=150)
    website: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    company_size: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)


class PrimaryContactInput(RegistrationInput):
    name: str = Field(min_length=1, max_length=255)
    job_title: str | None = Field(default=None, max_length=150)
    email: EmailStr | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)

    @field_validator("email", mode="before")
    @classmethod
    def blank_email_is_missing(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class InitialSalesEnquiryInput(RegistrationInput):
    product: str | None = Field(default=None, max_length=255)
    enquiry_text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    priority: EnquiryPriority = "normal"
    status: EnquiryStatus = "open"
    lead_source: LeadSource | None = None
    estimated_value: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=MAX_MONETARY_VALUE,
    )

    @field_validator("estimated_value", mode="after")
    @classmethod
    def check_monetary_value(cls, value: Decimal | None) -> Decimal | None:
        return validate_monetary_decimal(value)


class CustomerRegistrationCreate(RegistrationInput):
    customer_name: str = Field(min_length=1, max_length=255)
    status: CustomerStatus = "active"
    sales_stage: SalesStage = "new"
    company: RegistrationCompany
    primary_contact: PrimaryContactInput
    sales_enquiry: InitialSalesEnquiryInput


class CustomerRegistrationResponse(BaseModel):
    message: str
    customer: CustomerResponse
    company: CompanyResponse
    contact: ContactResponse
    sales_enquiry: SalesEnquiryResponse
