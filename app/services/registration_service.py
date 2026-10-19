"""Workshop registration: all checks + registration + job creation in ONE transaction."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError
from app.models import Member, NotificationJob, Registration, Workshop, WorkshopStatus, utcnow
from app.services.notification_service import create_confirmation_job


def registration_counts(session: Session, workshop_ids: list[int]) -> dict[int, int]:
    """Must be called inside a transaction. Returns {workshop_id: registered_count}."""
    if not workshop_ids:
        return {}
    rows = session.execute(
        select(Registration.workshop_id, func.count(Registration.id))
        .where(Registration.workshop_id.in_(workshop_ids))
        .group_by(Registration.workshop_id)
    ).all()
    return {workshop_id: count for workshop_id, count in rows}


def register_member(
    session: Session, workshop_id: int, member_id: int, now: datetime | None = None
) -> tuple[Registration, NotificationJob]:
    """Register a member and queue the confirmation job atomically.

    Race safety: the transaction holds SQLite's write lock from its first
    statement (BEGIN IMMEDIATE), so the capacity count and the insert cannot be
    interleaved with another registration. The UNIQUE(workshop_id, member_id)
    constraint is a second, independent guard against duplicates.

    If anything fails (including job creation) the whole transaction rolls back:
    no registration without a job, no job without a registration.
    """
    now = now or utcnow()
    try:
        with session.begin():
            workshop = session.get(Workshop, workshop_id)
            if workshop is None:
                raise NotFoundError(f"Workshop {workshop_id} does not exist.", "workshop_not_found")
            member = session.get(Member, member_id)
            if member is None:
                raise NotFoundError(f"Member {member_id} does not exist.", "member_not_found")
            if workshop.status == WorkshopStatus.CANCELLED.value:
                raise ConflictError("Workshop has been cancelled; registration is closed.", "workshop_cancelled")
            already = session.scalar(
                select(Registration.id).where(
                    Registration.workshop_id == workshop_id, Registration.member_id == member_id
                )
            )
            if already is not None:
                raise ConflictError("Member is already registered for this workshop.", "already_registered")
            taken = session.scalar(
                select(func.count(Registration.id)).where(Registration.workshop_id == workshop_id)
            )
            if taken >= workshop.capacity:
                raise ConflictError("Workshop is full.", "workshop_full")

            registration = Registration(workshop_id=workshop_id, member_id=member_id, created_at=now)
            session.add(registration)
            session.flush()
            job = create_confirmation_job(session, workshop, member, registration, now)
    except IntegrityError as exc:  # unique-constraint safety net
        raise ConflictError("Member is already registered for this workshop.", "already_registered") from exc
    return registration, job
