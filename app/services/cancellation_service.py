"""Workshop cancellation: status change + one cancellation job per registered member."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.errors import NotFoundError
from app.models import (
    JobStatus,
    Member,
    NotificationJob,
    NotificationType,
    Registration,
    Workshop,
    WorkshopStatus,
    utcnow,
)
from app.services.notification_service import build_payload, cancellation_key


def cancel_workshop(
    session: Session, workshop_id: int, now: datetime | None = None
) -> tuple[Workshop, bool, int, int]:
    """Cancel a workshop. Returns (workshop, already_cancelled, jobs_created, registered_count).

    Everything happens in one transaction: either the workshop is cancelled AND
    all jobs exist, or nothing changed. The call is idempotent: the job's
    idempotency key is UNIQUE and we use INSERT ... ON CONFLICT DO NOTHING, so
    retrying the endpoint can never create a second job for the same member.
    """
    now = now or utcnow()
    with session.begin():
        workshop = session.get(Workshop, workshop_id)
        if workshop is None:
            raise NotFoundError(f"Workshop {workshop_id} does not exist.", "workshop_not_found")

        already_cancelled = workshop.status == WorkshopStatus.CANCELLED.value
        if not already_cancelled:
            workshop.status = WorkshopStatus.CANCELLED.value
            workshop.cancelled_at = now

        recipients = session.execute(
            select(Member)
            .join(Registration, Registration.member_id == Member.id)
            .where(Registration.workshop_id == workshop_id)
            .order_by(Member.id)
        ).scalars().all()

        created = 0
        for member in recipients:
            result = session.execute(
                sqlite_insert(NotificationJob)
                .values(
                    notification_type=NotificationType.WORKSHOP_CANCELLATION.value,
                    member_id=member.id,
                    workshop_id=workshop.id,
                    registration_id=None,
                    idempotency_key=cancellation_key(workshop.id, member.id),
                    payload=build_payload(
                        member,
                        workshop,
                        f"Hi {member.name}, the workshop '{workshop.title}' has been cancelled.",
                    ),
                    status=JobStatus.PENDING.value,
                    attempts=0,
                    next_retry_at=now,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            created += int(result.rowcount or 0)
        return workshop, already_cancelled, created, len(recipients)
