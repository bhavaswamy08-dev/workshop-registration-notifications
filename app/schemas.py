"""Pydantic request/response schemas."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import JobStatus, NotificationType, WorkshopStatus

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")


class ErrorResponse(BaseModel):
    detail: str = Field(examples=["Workshop is full."])
    code: str = Field(examples=["workshop_full"])


# ----------------------------------------------------------------- workshops
class WorkshopCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=3, max_length=120, examples=["Intro to FastAPI"])
    description: str = Field(default="", max_length=2000, examples=["Hands-on REST API workshop"])
    starts_at: datetime = Field(examples=["2030-01-15T10:00:00Z"], description="Must be in the future. Naive values are treated as UTC.")
    capacity: int = Field(ge=1, le=10000, examples=[30])

    @field_validator("starts_at")
    @classmethod
    def _must_be_future(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if value <= datetime.now(timezone.utc):
            raise ValueError("starts_at must be in the future")
        return value


class WorkshopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str
    starts_at: datetime
    capacity: int
    status: WorkshopStatus
    created_at: datetime
    cancelled_at: datetime | None = None
    registered_count: int = 0
    seats_remaining: int = 0


class CancellationOut(BaseModel):
    workshop: WorkshopOut
    already_cancelled: bool
    notifications_created: int = Field(description="New cancellation jobs created by THIS call (0 on a retry).")
    message: str


# ------------------------------------------------------------------- members
class MemberCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100, examples=["Asha Rao"])
    email: str = Field(max_length=254, examples=["asha@example.com"], description="Identifier only. No real email is ever sent.")

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("invalid email address")
        return value


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    created_at: datetime


# ------------------------------------------------------------- registrations
class RegistrationCreate(BaseModel):
    workshop_id: int = Field(gt=0, examples=[1])
    member_id: int = Field(gt=0, examples=[1])


class RegistrationRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workshop_id: int
    member_id: int
    created_at: datetime


class RegistrationOut(RegistrationRecord):
    message: str
    confirmation_notification_id: int
    confirmation_status: JobStatus


# -------------------------------------------------------------- notifications
class DeliveryAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    attempt_number: int
    started_at: datetime
    finished_at: datetime
    outcome: str
    http_status: int | None
    error_message: str | None
    duration_ms: int


class NotificationJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    notification_type: NotificationType
    member_id: int
    workshop_id: int
    registration_id: int | None
    idempotency_key: str
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    next_retry_at: datetime
    created_at: datetime
    updated_at: datetime
    last_error: str | None
    delivered_at: datetime | None
    locked_by: str | None
    lease_expires_at: datetime | None


class NotificationJobDetail(NotificationJobOut):
    delivery_attempts: list[DeliveryAttemptOut] = Field(default_factory=list)


class NotificationSummary(BaseModel):
    total: int
    by_status: dict[str, int]
