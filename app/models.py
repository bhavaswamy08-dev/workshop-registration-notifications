"""SQLAlchemy models (5 tables) for the registration service."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores UTC as naive datetimes in SQLite and always returns aware UTC values."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class WorkshopStatus(str, Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"


class NotificationType(str, Enum):
    REGISTRATION_CONFIRMATION = "registration_confirmation"
    WORKSHOP_CANCELLATION = "workshop_cancellation"


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRYING = "retrying"
    DELIVERED = "delivered"
    PERMANENTLY_FAILED = "permanently_failed"


def _in_list(column: str, values: type[Enum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class Workshop(Base):
    __tablename__ = "workshops"
    __table_args__ = (
        CheckConstraint("capacity > 0", name="ck_workshops_capacity_positive"),
        CheckConstraint(_in_list("status", WorkshopStatus), name="ck_workshops_status"),
        Index("ix_workshops_status_starts_at", "status", "starts_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=WorkshopStatus.SCHEDULED.value)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    registrations: Mapped[list["Registration"]] = relationship(back_populates="workshop")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    registrations: Mapped[list["Registration"]] = relationship(back_populates="member")


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (
        # The database itself guarantees "one registration per member per workshop",
        # even if two requests race each other.
        UniqueConstraint("workshop_id", "member_id", name="uq_registration_workshop_member"),
        Index("ix_registrations_workshop_id", "workshop_id"),
        Index("ix_registrations_member_id", "member_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workshop_id: Mapped[int] = mapped_column(ForeignKey("workshops.id"), nullable=False)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    workshop: Mapped[Workshop] = relationship(back_populates="registrations")
    member: Mapped[Member] = relationship(back_populates="registrations")


class NotificationJob(Base):
    """A durable to-do item: "deliver this notification to this member"."""

    __tablename__ = "notification_jobs"
    __table_args__ = (
        # One job per business event, e.g. "registration_confirmation:<workshop>:<member>".
        UniqueConstraint("idempotency_key", name="uq_notification_jobs_idempotency_key"),
        CheckConstraint(_in_list("status", JobStatus), name="ck_notification_jobs_status"),
        CheckConstraint(_in_list("notification_type", NotificationType), name="ck_notification_jobs_type"),
        CheckConstraint("attempts >= 0", name="ck_notification_jobs_attempts"),
        Index("ix_notification_jobs_status_next_retry", "status", "next_retry_at"),
        Index("ix_notification_jobs_status_lease", "status", "lease_expires_at"),
        Index("ix_notification_jobs_member_id", "member_id"),
        Index("ix_notification_jobs_workshop_id", "workshop_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_type: Mapped[str] = mapped_column(String(40), nullable=False)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    workshop_id: Mapped[int] = mapped_column(ForeignKey("workshops.id"), nullable=False)
    registration_id: Mapped[int | None] = mapped_column(ForeignKey("registrations.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=JobStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Lease fields: who holds the job and until when (crash recovery).
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    attempts_log: Mapped[list["DeliveryAttempt"]] = relationship(
        back_populates="job", order_by="DeliveryAttempt.attempt_number"
    )


class DeliveryAttempt(Base):
    """Audit log: one row for every HTTP delivery attempt of a job."""

    __tablename__ = "notification_delivery_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_attempt_job_number"),
        Index("ix_delivery_attempts_job_id", "job_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("notification_jobs.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)  # success|http_error|timeout|connection_error|...
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    job: Mapped[NotificationJob] = relationship(back_populates="attempts_log")
