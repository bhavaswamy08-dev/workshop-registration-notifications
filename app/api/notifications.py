from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import NotFoundError
from app.models import JobStatus, NotificationJob
from app.schemas import DeliveryAttemptOut, ErrorResponse, NotificationJobDetail, NotificationJobOut, NotificationSummary
from app.services import notification_service

router = APIRouter(prefix="/notifications", tags=["Notification jobs"])


@router.get("", response_model=list[NotificationJobOut], summary="List notification jobs",
            description="Inspect the durable job queue. Use this to watch jobs move pending -> delivered.")
def list_notifications(
    status: JobStatus | None = Query(default=None),
    member_id: int | None = Query(default=None),
    workshop_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[NotificationJob]:
    return notification_service.list_jobs(db, status.value if status else None, member_id, workshop_id, limit, offset)


@router.get("/summary", response_model=NotificationSummary, summary="Count jobs by status")
def notifications_summary(db: Session = Depends(get_db)) -> NotificationSummary:
    by_status = notification_service.job_summary(db)
    return NotificationSummary(total=sum(by_status.values()), by_status=by_status)


@router.get("/{job_id}", response_model=NotificationJobDetail, summary="View one job with all its delivery attempts",
            responses={404: {"model": ErrorResponse}})
def get_notification(job_id: int, db: Session = Depends(get_db)) -> NotificationJobDetail:
    with db.begin():
        job = db.get(NotificationJob, job_id)
        if job is None:
            raise NotFoundError(f"Notification job {job_id} does not exist.", "notification_not_found")
        base = NotificationJobOut.model_validate(job).model_dump()
        attempts = [DeliveryAttemptOut.model_validate(a) for a in job.attempts_log]
        detail = NotificationJobDetail(**base, delivery_attempts=attempts)
    return detail
