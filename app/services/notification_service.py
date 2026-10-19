"""Notification job logic: creation, claiming (with leases), retry scheduling.

All functions that change state run in ONE short transaction and never hold a
database transaction open during an HTTP call.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    DeliveryAttempt,
    JobStatus,
    Member,
    NotificationJob,
    NotificationType,
    Registration,
    Workshop,
    utcnow,
)

CLAIMABLE = (JobStatus.PENDING.value, JobStatus.RETRYING.value)


# ------------------------------------------------------------ job creation
def confirmation_key(workshop_id: int, member_id: int) -> str:
    return f"{NotificationType.REGISTRATION_CONFIRMATION.value}:{workshop_id}:{member_id}"


def cancellation_key(workshop_id: int, member_id: int) -> str:
    return f"{NotificationType.WORKSHOP_CANCELLATION.value}:{workshop_id}:{member_id}"


def build_payload(member: Member, workshop: Workshop, message: str) -> dict[str, Any]:
    return {
        "recipient_member_id": member.id,
        "recipient_name": member.name,
        "recipient_email": member.email,
        "workshop_id": workshop.id,
        "workshop_title": workshop.title,
        "message": message,
    }


def create_confirmation_job(
    session: Session, workshop: Workshop, member: Member, registration: Registration, now: datetime | None = None
) -> NotificationJob:
    """Add the confirmation job to the CURRENT transaction (caller commits)."""
    now = now or utcnow()
    job = NotificationJob(
        notification_type=NotificationType.REGISTRATION_CONFIRMATION.value,
        member_id=member.id,
        workshop_id=workshop.id,
        registration_id=registration.id,
        idempotency_key=confirmation_key(workshop.id, member.id),
        payload=build_payload(
            member, workshop, f"Hi {member.name}, you are registered for '{workshop.title}'."
        ),
        status=JobStatus.PENDING.value,
        attempts=0,
        next_retry_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job


# ------------------------------------------------------------ retry policy
def compute_backoff_delay(attempt: int, settings: Settings, rng: random.Random | None = None) -> float:
    """Exponential backoff: base * 2**(attempt-1), capped, with optional jitter.

    attempt=1 -> 2s, 2 -> 4s, 3 -> 8s, 4 -> 16s ... (defaults), never above max.
    """
    delay = min(settings.retry_max_delay_seconds, settings.retry_base_delay_seconds * (2 ** (attempt - 1)))
    if settings.retry_jitter_ratio > 0:
        spread = delay * settings.retry_jitter_ratio
        delay += (rng or random).uniform(-spread, spread)
    return max(0.0, min(delay, settings.retry_max_delay_seconds))


# ----------------------------------------------------------- claiming jobs
@dataclass(frozen=True)
class ClaimedJob:
    """Plain snapshot of a claimed job (safe to use after the session is closed)."""

    id: int
    notification_type: str
    member_id: int
    workshop_id: int
    registration_id: int | None
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int  # includes the attempt that is about to be made


def _recover_expired_leases(session: Session, now: datetime, settings: Settings) -> int:
    """Inside a transaction: free jobs whose worker vanished (lease expired)."""
    expired = and_(NotificationJob.status == JobStatus.PROCESSING.value, NotificationJob.lease_expires_at <= now)
    no_more_attempts = session.execute(
        update(NotificationJob)
        .where(expired, NotificationJob.attempts >= settings.max_delivery_attempts)
        .values(
            status=JobStatus.PERMANENTLY_FAILED.value,
            last_error="Worker lease expired during the final delivery attempt",
            locked_by=None,
            lease_expires_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    requeued = session.execute(
        update(NotificationJob)
        .where(expired)
        .values(
            status=JobStatus.RETRYING.value,
            next_retry_at=now,
            last_error="Worker lease expired; job re-queued",
            locked_by=None,
            lease_expires_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    return int(no_more_attempts + requeued)


def recover_expired_jobs(session: Session, now: datetime, settings: Settings) -> int:
    """Public helper: recover jobs stuck in 'processing' after a worker crash."""
    with session.begin():
        return _recover_expired_leases(session, now, settings)


def claim_next_job(session: Session, worker_id: str, now: datetime, settings: Settings) -> ClaimedJob | None:
    """Atomically claim ONE eligible job for ``worker_id`` (or return None).

    Safety: the transaction takes SQLite's write lock (BEGIN IMMEDIATE), and the
    UPDATE repeats the eligibility conditions, so even a buggy caller could not
    claim a job that someone else already holds (rowcount would be 0).
    """
    with session.begin():
        _recover_expired_leases(session, now, settings)
        candidate_id = session.execute(
            select(NotificationJob.id)
            .where(NotificationJob.status.in_(CLAIMABLE), NotificationJob.next_retry_at <= now)
            .order_by(NotificationJob.next_retry_at, NotificationJob.id)
            .limit(1)
        ).scalar_one_or_none()
        if candidate_id is None:
            return None
        claimed = session.execute(
            update(NotificationJob)
            .where(
                NotificationJob.id == candidate_id,
                NotificationJob.status.in_(CLAIMABLE),
                NotificationJob.next_retry_at <= now,
            )
            .values(
                status=JobStatus.PROCESSING.value,
                attempts=NotificationJob.attempts + 1,
                locked_by=worker_id,
                lease_expires_at=now + timedelta(seconds=settings.lease_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed != 1:
            return None
        job = session.execute(select(NotificationJob).where(NotificationJob.id == candidate_id)).scalar_one()
        return ClaimedJob(
            id=job.id,
            notification_type=job.notification_type,
            member_id=job.member_id,
            workshop_id=job.workshop_id,
            registration_id=job.registration_id,
            idempotency_key=job.idempotency_key,
            payload=dict(job.payload),
            attempts=job.attempts,
        )


# ------------------------------------------------------ finishing an attempt
@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    outcome: str  # success | http_error | timeout | connection_error | request_error | internal_error
    retryable: bool = True
    http_status: int | None = None
    error: str | None = None


def finalize_attempt(
    session: Session,
    job: ClaimedJob,
    worker_id: str,
    result: DeliveryResult,
    started_at: datetime,
    finished_at: datetime,
    settings: Settings,
    rng: random.Random | None = None,
) -> str:
    """Record the attempt and move the job to its next state. Returns the new status.

    The job UPDATE only applies if this worker still owns the job
    (status=processing AND locked_by=worker). If the lease expired and another
    worker took over, this stale result is ignored and "lease_lost" is returned.
    """
    duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
    with session.begin():
        session.add(
            DeliveryAttempt(
                job_id=job.id,
                attempt_number=job.attempts,
                started_at=started_at,
                finished_at=finished_at,
                outcome=result.outcome,
                http_status=result.http_status,
                error_message=result.error,
                duration_ms=duration_ms,
            )
        )
        common = {"locked_by": None, "lease_expires_at": None, "updated_at": finished_at}
        if result.success:
            new_status = JobStatus.DELIVERED.value
            values = {**common, "status": new_status, "delivered_at": finished_at, "last_error": None}
        elif not result.retryable or job.attempts >= settings.max_delivery_attempts:
            new_status = JobStatus.PERMANENTLY_FAILED.value
            values = {**common, "status": new_status, "last_error": result.error}
        else:
            new_status = JobStatus.RETRYING.value
            delay = compute_backoff_delay(job.attempts, settings, rng)
            values = {
                **common,
                "status": new_status,
                "last_error": result.error,
                "next_retry_at": finished_at + timedelta(seconds=delay),
            }
        applied = session.execute(
            update(NotificationJob)
            .where(
                NotificationJob.id == job.id,
                NotificationJob.status == JobStatus.PROCESSING.value,
                NotificationJob.locked_by == worker_id,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount
    return new_status if applied == 1 else "lease_lost"


# ------------------------------------------------------------------ queries
def list_jobs(
    session: Session,
    status: str | None = None,
    member_id: int | None = None,
    workshop_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[NotificationJob]:
    stmt = select(NotificationJob).order_by(NotificationJob.id)
    if status:
        stmt = stmt.where(NotificationJob.status == status)
    if member_id:
        stmt = stmt.where(NotificationJob.member_id == member_id)
    if workshop_id:
        stmt = stmt.where(NotificationJob.workshop_id == workshop_id)
    with session.begin():
        return list(session.execute(stmt.limit(limit).offset(offset)).scalars())


def job_summary(session: Session) -> dict[str, int]:
    with session.begin():
        rows = session.execute(
            select(NotificationJob.status, func.count()).group_by(NotificationJob.status)
        ).all()
    return {status: count for status, count in rows}
