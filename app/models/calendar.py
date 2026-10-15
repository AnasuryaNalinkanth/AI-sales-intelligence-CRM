from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class GoogleCalendarConnection(Base):
    """One user's Google Calendar authorization. Tokens are Fernet-encrypted."""

    __tablename__ = "google_calendar_connections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('connected', 'reauthorization_required')",
            name="ck_google_calendar_connections_status",
        ),
        CheckConstraint(
            "reminder_minutes BETWEEN 0 AND 1440",
            name="ck_google_calendar_connections_reminder_minutes",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="connected")
    reminder_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CalendarEventLink(Base):
    """The Google event created for one CRM activity in one user's calendar."""

    __tablename__ = "calendar_event_links"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "activity_type",
            "activity_id",
            name="uq_calendar_event_links_user_activity",
        ),
        CheckConstraint(
            "activity_type IN ('meeting', 'call', 'followup')",
            name="ck_calendar_event_links_activity_type",
        ),
        CheckConstraint(
            "sync_status IN ('synced', 'failed', 'deleted')",
            name="ck_calendar_event_links_sync_status",
        ),
    )

    link_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    activity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    activity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    google_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
