from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.connection import Base
from app.models.organization import Organization


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="sales", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.organization_id",
            ondelete="RESTRICT",
            name="fk_users_organization_id_organizations",
        ),
        nullable=False,
        index=True,
    )
    # Stored only; verification is not enforced until Phase 2.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship()


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        # Referenced by customers' composite foreign key.
        UniqueConstraint("organization_id", "company_id", name="uq_companies_organization_company"),
        UniqueConstraint(
            "organization_id", "company_name", name="uq_companies_organization_company_name"
        ),
    )

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.organization_id",
            ondelete="RESTRICT",
            name="fk_companies_organization_id_organizations",
        ),
        nullable=False,
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    industry: Mapped[str | None] = mapped_column(String(150))
    website: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(120))
    company_size: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customers: Mapped[list["Customer"]] = relationship(
        back_populates="company", foreign_keys="Customer.company_id"
    )


class SchedulingLock(Base):
    """One row per organization; lock_id equals the organization_id it serializes."""

    __tablename__ = "scheduling_locks"

    lock_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_company_customer_name", "company_id", "customer_name"),
        UniqueConstraint(
            "company_id",
            "customer_name",
            name="uq_customers_company_customer_name",
        ),
        UniqueConstraint(
            "organization_id", "customer_id", name="uq_customers_organization_customer"
        ),
        Index("ix_customers_organization_company", "organization_id", "company_id"),
        ForeignKeyConstraint(
            ["organization_id", "company_id"],
            ["companies.organization_id", "companies.company_id"],
            ondelete="RESTRICT",
            name="fk_customers_company_organization",
        ),
    )

    customer_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.company_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    sales_stage: Mapped[str] = mapped_column(String(50), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="customers", foreign_keys=[company_id])
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="customer", foreign_keys="Contact.customer_id"
    )
    sales_enquiries: Mapped[list["SalesEnquiry"]] = relationship(
        back_populates="customer", foreign_keys="SalesEnquiry.customer_id"
    )
    meetings: Mapped[list["Meeting"]] = relationship(
        back_populates="customer", foreign_keys="Meeting.customer_id"
    )
    calls: Mapped[list["Call"]] = relationship(
        back_populates="customer", foreign_keys="Call.customer_id"
    )
    followups: Mapped[list["FollowUp"]] = relationship(
        back_populates="customer", foreign_keys="FollowUp.customer_id"
    )


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_customer_id_name", "customer_id", "name"),
        Index("ix_contacts_organization_customer", "organization_id", "customer_id"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.customer_id"],
            ondelete="RESTRICT",
            name="fk_contacts_customer_organization",
        ),
    )

    contact_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title: Mapped[str | None] = mapped_column(String(150))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[Customer] = relationship(back_populates="contacts", foreign_keys=[customer_id])
    meetings: Mapped[list["Meeting"]] = relationship(back_populates="contact")
    calls: Mapped[list["Call"]] = relationship(back_populates="contact")


class SalesEnquiry(Base):
    __tablename__ = "sales_enquiries"
    __table_args__ = (
        Index("ix_sales_enquiries_customer_created_at", "customer_id", "created_at"),
        CheckConstraint(
            "priority IN ('low', 'medium', 'normal', 'high', 'urgent')",
            name="ck_sales_enquiries_priority",
        ),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'converted', 'closed', 'lost')",
            name="ck_sales_enquiries_status",
        ),
        CheckConstraint(
            "qualification_status IN ('ready', 'not_ready')",
            name="ck_sales_enquiries_qualification",
        ),
        CheckConstraint(
            "lead_source IN ('website', 'referral', 'campaign', 'manual', 'import', 'other')",
            name="ck_sales_enquiries_lead_source",
        ),
        # Outcome is separate from qualification: a closed lead always has its timestamp,
        # and only a lost one carries a loss reason.
        CheckConstraint(
            "(outcome IS NULL AND closed_at IS NULL AND loss_reason IS NULL)"
            " OR (outcome = 'won' AND closed_at IS NOT NULL AND loss_reason IS NULL)"
            " OR (outcome = 'lost' AND closed_at IS NOT NULL AND loss_reason IS NOT NULL)",
            name="ck_sales_enquiries_outcome",
        ),
        Index("ix_sales_enquiries_organization_customer", "organization_id", "customer_id"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.customer_id"],
            ondelete="RESTRICT",
            name="fk_sales_enquiries_customer_organization",
        ),
    )

    enquiry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product: Mapped[str | None] = mapped_column(String(255))
    enquiry_text: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(50), nullable=False, default="normal", index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="open", index=True)
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "contacts.contact_id",
            ondelete="SET NULL",
            name="fk_sales_enquiries_contact_id_contacts",
        ),
        index=True,
    )
    assigned_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.user_id",
            ondelete="SET NULL",
            name="fk_sales_enquiries_assigned_user_id_users",
        ),
        index=True,
    )
    lead_source: Mapped[str | None] = mapped_column(String(50))
    expected_close_date: Mapped[date | None] = mapped_column(Date)
    qualification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_ready", server_default="not_ready", index=True
    )
    qualification_reason: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(10), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    loss_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[Customer] = relationship(
        back_populates="sales_enquiries", foreign_keys=[customer_id]
    )
    contact: Mapped[Contact | None] = relationship(foreign_keys=[contact_id])
    assigned_user: Mapped[User | None] = relationship(foreign_keys=[assigned_user_id])
    meetings: Mapped[list["Meeting"]] = relationship(back_populates="enquiry")
    calls: Mapped[list["Call"]] = relationship(back_populates="enquiry")
    followups: Mapped[list["FollowUp"]] = relationship(back_populates="enquiry")


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        Index("ix_meetings_customer_scheduled_at", "customer_id", "scheduled_at"),
        Index("ix_meetings_organization_customer", "organization_id", "customer_id"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.customer_id"],
            ondelete="RESTRICT",
            name="fk_meetings_customer_organization",
        ),
    )

    meeting_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.contact_id", ondelete="SET NULL"), index=True
    )
    enquiry_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_enquiries.enquiry_id", ondelete="SET NULL"), index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    scheduled_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    duration: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="scheduled", index=True)
    agenda: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[Customer] = relationship(
        back_populates="meetings", foreign_keys=[customer_id]
    )
    contact: Mapped[Contact | None] = relationship(back_populates="meetings")
    enquiry: Mapped[SalesEnquiry | None] = relationship(back_populates="meetings")
    followups: Mapped[list["FollowUp"]] = relationship(back_populates="meeting")


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        Index("ix_calls_customer_scheduled_at", "customer_id", "scheduled_at"),
        CheckConstraint(
            "status IN ('scheduled', 'attempted', 'completed', 'failed', 'cancelled', 'missed')",
            name="ck_calls_status",
        ),
        Index("ix_calls_organization_customer", "organization_id", "customer_id"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.customer_id"],
            ondelete="RESTRICT",
            name="fk_calls_customer_organization",
        ),
    )

    call_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.contact_id", ondelete="SET NULL"), index=True
    )
    enquiry_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_enquiries.enquiry_id", ondelete="SET NULL"), index=True
    )
    call_type: Mapped[str | None] = mapped_column(String(50))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    actual_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    scheduled_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    duration: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="scheduled", index=True)
    outcome: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    next_followup_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[Customer] = relationship(
        back_populates="calls", foreign_keys=[customer_id]
    )
    contact: Mapped[Contact | None] = relationship(back_populates="calls")
    enquiry: Mapped[SalesEnquiry | None] = relationship(back_populates="calls")
    followups: Mapped[list["FollowUp"]] = relationship(back_populates="call")


class FollowUp(Base):
    __tablename__ = "follow_ups"
    __table_args__ = (
        Index("ix_follow_ups_customer_due_date", "customer_id", "due_date"),
        Index("ix_follow_ups_organization_customer", "organization_id", "customer_id"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.customer_id"],
            ondelete="RESTRICT",
            name="fk_follow_ups_customer_organization",
        ),
    )

    followup_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    enquiry_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_enquiries.enquiry_id", ondelete="SET NULL"), index=True
    )
    meeting_id: Mapped[int | None] = mapped_column(
        ForeignKey("meetings.meeting_id", ondelete="SET NULL"), index=True
    )
    call_id: Mapped[int | None] = mapped_column(
        ForeignKey("calls.call_id", ondelete="SET NULL"), index=True
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    due_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    duration: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending", index=True)
    description: Mapped[str | None] = mapped_column(Text)
    assigned_to: Mapped[str | None] = mapped_column(String(255))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[Customer] = relationship(
        back_populates="followups", foreign_keys=[customer_id]
    )
    enquiry: Mapped[SalesEnquiry | None] = relationship(back_populates="followups")
    meeting: Mapped[Meeting | None] = relationship(back_populates="followups")
    call: Mapped[Call | None] = relationship(back_populates="followups")
