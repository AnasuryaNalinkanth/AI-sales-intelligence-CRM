from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ActivityType = Literal["enquiry", "meeting", "call", "follow_up"]


class FindCustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    search: str = Field(min_length=1, max_length=255, description="Name or company search text.")


class CustomerIdInput(BaseModel):
    customer_id: int = Field(ge=1, description="Positive CRM customer ID.")


class CustomerActivityInput(CustomerIdInput):
    activity_type: ActivityType | None = Field(
        default=None,
        description="Optional activity type to include.",
    )
    start_date: date | None = Field(default=None, description="Inclusive ISO date lower bound.")
    end_date: date | None = Field(default=None, description="Inclusive ISO date upper bound.")

    @model_validator(mode="after")
    def date_range_is_valid(self) -> "CustomerActivityInput":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class FindSalesLeadsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    search: str | None = Field(
        default=None, max_length=255, description="Optional product, requirement, customer or company text."
    )
    qualification_status: Literal["ready", "not_ready"] | None = Field(
        default=None, description="Qualification of the lead; not an outcome."
    )
    outcome: Literal["open", "won", "lost"] | None = Field(
        default=None, description="open = not yet closed; won/lost = closed opportunity outcome."
    )
