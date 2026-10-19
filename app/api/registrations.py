from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import NotFoundError
from app.models import JobStatus, Registration, Workshop
from app.schemas import ErrorResponse, RegistrationCreate, RegistrationOut, RegistrationRecord
from app.services.registration_service import register_member

router = APIRouter(tags=["Registrations"])


@router.post(
    "/registrations",
    response_model=RegistrationOut,
    status_code=201,
    summary="Register a member for a workshop",
    description=(
        "Saves the registration AND a confirmation notification job in one database transaction, then "
        "returns immediately. The notification is delivered later by the background worker."
    ),
    responses={
        404: {"model": ErrorResponse, "description": "workshop_not_found / member_not_found"},
        409: {"model": ErrorResponse, "description": "already_registered / workshop_full / workshop_cancelled"},
    },
)
def create_registration(payload: RegistrationCreate, db: Session = Depends(get_db)) -> RegistrationOut:
    registration, job = register_member(db, payload.workshop_id, payload.member_id)
    return RegistrationOut(
        id=registration.id,
        workshop_id=registration.workshop_id,
        member_id=registration.member_id,
        created_at=registration.created_at,
        message="Registration successful. A confirmation notification has been queued.",
        confirmation_notification_id=job.id,
        confirmation_status=JobStatus(job.status),
    )


@router.get("/registrations", response_model=list[RegistrationRecord], summary="List registrations")
def list_registrations(workshop_id: int | None = Query(default=None), member_id: int | None = Query(default=None),
                       db: Session = Depends(get_db)) -> list[Registration]:
    stmt = select(Registration).order_by(Registration.id)
    if workshop_id:
        stmt = stmt.where(Registration.workshop_id == workshop_id)
    if member_id:
        stmt = stmt.where(Registration.member_id == member_id)
    with db.begin():
        return list(db.execute(stmt).scalars())


@router.get("/registrations/{registration_id}", response_model=RegistrationRecord, summary="View a registration",
            responses={404: {"model": ErrorResponse}})
def get_registration(registration_id: int, db: Session = Depends(get_db)) -> Registration:
    with db.begin():
        registration = db.get(Registration, registration_id)
        if registration is None:
            raise NotFoundError(f"Registration {registration_id} does not exist.", "registration_not_found")
    return registration


@router.get("/workshops/{workshop_id}/registrations", response_model=list[RegistrationRecord],
            summary="List registrations of one workshop", responses={404: {"model": ErrorResponse}})
def list_workshop_registrations(workshop_id: int, db: Session = Depends(get_db)) -> list[Registration]:
    with db.begin():
        if db.get(Workshop, workshop_id) is None:
            raise NotFoundError(f"Workshop {workshop_id} does not exist.", "workshop_not_found")
        return list(db.execute(
            select(Registration).where(Registration.workshop_id == workshop_id).order_by(Registration.id)
        ).scalars())
