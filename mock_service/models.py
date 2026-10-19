"""Mock service table: one row per UNIQUE notification_id that was 'delivered'."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from mock_service.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class MockDelivery(Base):
    __tablename__ = "mock_deliveries"
    # Idempotency: the same notification_id can only be stored once.
    __table_args__ = (UniqueConstraint("notification_id", name="uq_mock_deliveries_notification_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[str] = mapped_column(String(200), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient_member_id: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_name: Mapped[str] = mapped_column(String(100), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    workshop_id: Mapped[int] = mapped_column(Integer, nullable=False)
    workshop_title: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
