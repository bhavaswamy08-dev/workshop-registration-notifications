from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import NotFoundError
from app.models import Workshop, WorkshopStatus
from app.schemas import CancellationOut, ErrorResponse, WorkshopCreate, WorkshopOut
from app.services.cancellation_service import cancel_workshop
from app.services.registration_service import registration_counts

router = APIRouter(prefix="/workshops", tags=["Workshops"])


def to_out(workshop: Workshop, registered: int) -> WorkshopOut:
    out = WorkshopOut.model_validate(workshop)
    out.registered_count = registered
    out.seats_remaining = max(0, workshop.capacity - registered)
    return out


@router.post("", response_model=WorkshopOut, status_code=201, summary="Create a workshop",
             responses={422: {"description": "Validation error (e.g. date in the past, capacity < 1)."}})
def create_workshop(payload: WorkshopCreate, db: Session = Depends(get_db)) -> WorkshopOut:
    with db.begin():
        workshop = Workshop(**payload.model_dump())
        db.add(workshop)
        db.flush()
    return to_out(workshop, 0)


@router.get("", response_model=list[WorkshopOut], summary="List workshops")
def list_workshops(status: WorkshopStatus | None = Query(default=None, description="Filter by status"),
                   db: Session = Depends(get_db)) -> list[WorkshopOut]:
    with db.begin():
        stmt = select(Workshop).order_by(Workshop.id)
        if status:
            stmt = stmt.where(Workshop.status == status.value)
        workshops = db.execute(stmt).scalars().all()
        counts = registration_counts(db, [w.id for w in workshops])
    return [to_out(w, counts.get(w.id, 0)) for w in workshops]


@router.get("/{workshop_id}", response_model=WorkshopOut, summary="View a workshop",
            responses={404: {"model": ErrorResponse}})
def get_workshop(workshop_id: int, db: Session = Depends(get_db)) -> WorkshopOut:
    with db.begin():
        workshop = db.get(Workshop, workshop_id)
        if workshop is None:
            raise NotFoundError(f"Workshop {workshop_id} does not exist.", "workshop_not_found")
        counts = registration_counts(db, [workshop.id])
    return to_out(workshop, counts.get(workshop.id, 0))


@router.post("/{workshop_id}/cancel", response_model=CancellationOut, summary="Cancel a workshop",
             description=("Marks the workshop as cancelled and queues ONE cancellation notification job per "
                          "registered member, all in a single transaction. Returns immediately; the worker "
                          "delivers the notifications later. Calling it again is safe (idempotent): no "
                          "duplicate jobs are created."),
             responses={404: {"model": ErrorResponse}})
def cancel(workshop_id: int, db: Session = Depends(get_db)) -> CancellationOut:
    workshop, already, created, registered = cancel_workshop(db, workshop_id)
    message = (
        "Workshop was already cancelled; no new notifications were created."
        if already
        else f"Workshop cancelled. {created} cancellation notification(s) queued."
    )
    return CancellationOut(
        workshop=to_out(workshop, registered),
        already_cancelled=already,
        notifications_created=created,
        message=message,
    )
