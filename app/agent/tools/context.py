from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import APIError


@dataclass
class AgentToolContext:
    # Set only from the authenticated user, never from model or client input; every
    # tool scopes its reads and writes to this organization.
    organization_id: int
    locked_customer_id: int | None = None
    timezone_name: str | None = None
    # The user's current chat message only, never earlier turns.
    request_message: str | None = None
    customer_ids: set[int] = field(default_factory=set)
    contacts_by_customer: dict[int, set[int]] = field(default_factory=dict)
    enquiries_by_customer: dict[int, set[int]] = field(default_factory=dict)
    meetings_by_customer: dict[int, set[int]] = field(default_factory=dict)
    calls_by_customer: dict[int, set[int]] = field(default_factory=dict)
    followups_by_customer: dict[int, set[int]] = field(default_factory=dict)
    # Last availability conflict found by a scheduling tool in this request.
    schedule_conflict: dict[str, Any] | None = None
    # Read-tool calls already answered in this request, keyed by name and arguments.
    retrieved_calls: set[str] = field(default_factory=set)
    # Values of the user's validated create_meeting draft being completed in this request.
    meeting_draft: dict[str, Any] | None = None

    def can_access_customer(self, customer_id: int) -> bool:
        return self.locked_customer_id is None or self.locked_customer_id == customer_id

    def ensure_customer_access(self, customer_id: int) -> None:
        if not self.can_access_customer(customer_id):
            raise APIError(
                "This conversation is scoped to a different customer.",
                status_code=403,
                code="customer_scope_violation",
            )

    def remember_customer(self, customer_id: int) -> None:
        if not self.can_access_customer(customer_id):
            return
        self.customer_ids.add(customer_id)

    def remember_contact(self, customer_id: int, contact_id: int) -> None:
        if not self.can_access_customer(customer_id):
            return
        self.customer_ids.add(customer_id)
        self.contacts_by_customer.setdefault(customer_id, set()).add(contact_id)

    def remember_enquiry(self, customer_id: int, enquiry_id: int) -> None:
        if not self.can_access_customer(customer_id):
            return
        self.customer_ids.add(customer_id)
        self.enquiries_by_customer.setdefault(customer_id, set()).add(enquiry_id)

    def remember_meeting(self, customer_id: int, meeting_id: int) -> None:
        if not self.can_access_customer(customer_id):
            return
        self.customer_ids.add(customer_id)
        self.meetings_by_customer.setdefault(customer_id, set()).add(meeting_id)

    def remember_call(self, customer_id: int, call_id: int) -> None:
        if not self.can_access_customer(customer_id):
            return
        self.customer_ids.add(customer_id)
        self.calls_by_customer.setdefault(customer_id, set()).add(call_id)

    def remember_followup(self, customer_id: int, followup_id: int) -> None:
        if not self.can_access_customer(customer_id):
            return
        self.customer_ids.add(customer_id)
        self.followups_by_customer.setdefault(customer_id, set()).add(followup_id)

    def has_customer(self, customer_id: int) -> bool:
        return self.can_access_customer(customer_id) and customer_id in self.customer_ids

    def has_contact(self, customer_id: int, contact_id: int) -> bool:
        return self.has_customer(customer_id) and contact_id in self.contacts_by_customer.get(
            customer_id, set()
        )

    def has_enquiry(self, customer_id: int, enquiry_id: int) -> bool:
        return self.has_customer(customer_id) and enquiry_id in self.enquiries_by_customer.get(
            customer_id, set()
        )

    def has_meeting(self, customer_id: int, meeting_id: int) -> bool:
        return self.has_customer(customer_id) and meeting_id in self.meetings_by_customer.get(
            customer_id, set()
        )

    def has_call(self, customer_id: int, call_id: int) -> bool:
        return self.has_customer(customer_id) and call_id in self.calls_by_customer.get(
            customer_id, set()
        )

    def has_followup(self, customer_id: int, followup_id: int) -> bool:
        return self.has_customer(customer_id) and followup_id in self.followups_by_customer.get(
            customer_id, set()
        )

    def remember_profile(self, profile: dict[str, object]) -> None:
        raw_customer_id = profile.get("customer_id")
        if not isinstance(raw_customer_id, int):
            customer = profile.get("customer")
            if isinstance(customer, dict):
                raw_customer_id = customer.get("customer_id")
        if not isinstance(raw_customer_id, int):
            return
        customer_id = raw_customer_id
        if not self.can_access_customer(customer_id):
            return
        self.remember_customer(customer_id)

        contacts = profile.get("contacts")
        if isinstance(contacts, list):
            for item in contacts:
                if isinstance(item, dict) and isinstance(item.get("contact_id"), int):
                    self.remember_contact(customer_id, item["contact_id"])

        enquiries = profile.get("sales_enquiries")
        if isinstance(enquiries, list):
            for item in enquiries:
                if isinstance(item, dict) and isinstance(item.get("enquiry_id"), int):
                    self.remember_enquiry(customer_id, item["enquiry_id"])

