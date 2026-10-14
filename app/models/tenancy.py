"""Write-time tenant guard: a CRM row must share the organization of every record it links to.

MySQL enforces the parent links (customer -> company, activity -> customer) with
composite foreign keys. The nullable links (contact, enquiry, meeting, call, assignee) cannot
be enforced that way, because ON DELETE SET NULL would also null organization_id,
so this listener checks all of them before every flush, on every database.
Services still scope every read and validate every reference; this is the backstop.
"""

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models.crm import Call, Company, Contact, Customer, FollowUp, Meeting, SalesEnquiry, User

# model -> (foreign key, relationship, referenced model); the first link is the parent.
_LINKS: dict[type, tuple[tuple[str, str, type], ...]] = {
    Customer: (("company_id", "company", Company),),
    Contact: (("customer_id", "customer", Customer),),
    SalesEnquiry: (
        ("customer_id", "customer", Customer),
        ("contact_id", "contact", Contact),
        ("assigned_user_id", "assigned_user", User),
    ),
    Meeting: (
        ("customer_id", "customer", Customer),
        ("contact_id", "contact", Contact),
        ("enquiry_id", "enquiry", SalesEnquiry),
    ),
    Call: (
        ("customer_id", "customer", Customer),
        ("contact_id", "contact", Contact),
        ("enquiry_id", "enquiry", SalesEnquiry),
    ),
    FollowUp: (
        ("customer_id", "customer", Customer),
        ("enquiry_id", "enquiry", SalesEnquiry),
        ("meeting_id", "meeting", Meeting),
        ("call_id", "call", Call),
    ),
}


class CrossOrganizationReference(ValueError):
    """A write would link records that belong to different organizations."""


def _target(session: Session, record: object, key: str, relationship: str, model: type):
    target = getattr(record, relationship)
    if target is None and getattr(record, key) is not None:
        target = session.get(model, getattr(record, key))
    # A parent added in the same flush may not have derived its organization yet.
    if target is not None and target.organization_id is None and type(target) in _LINKS:
        check_record(session, target)
    return target


def check_record(session: Session, record: object) -> None:
    links = _LINKS[type(record)]
    targets = [_target(session, record, *link) for link in links]
    parent = targets[0]
    if record.organization_id is None and parent is not None:
        record.organization_id = parent.organization_id
    for target in targets:
        if target is not None and target.organization_id != record.organization_id:
            raise CrossOrganizationReference(
                f"{type(record).__name__} cannot reference a {type(target).__name__} "
                "from another organization."
            )


def _changed(record: object) -> bool:
    state = inspect(record)
    names = ["organization_id"] + [name for link in _LINKS[type(record)] for name in link[:2]]
    return any(state.attrs[name].history.has_changes() for name in names)


@event.listens_for(Session, "before_flush")
def _enforce_tenant_links(session: Session, flush_context, instances) -> None:
    for record in list(session.new):
        if type(record) in _LINKS:
            check_record(session, record)
    for record in list(session.dirty):
        if type(record) in _LINKS and _changed(record):
            check_record(session, record)
