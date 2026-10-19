"""Workshop Registration API (FastAPI).

Run:  uvicorn app.main:app --port 8000
Docs: /docs (Swagger UI), /redoc, /health
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import members, notifications, registrations, workshops
from app.database import get_db, init_db
from app.errors import AppError

DESCRIPTION = """
Workshop registration service with **durable notification jobs**.

* Registering a member (or cancelling a workshop) saves notification jobs in SQLite
  **in the same transaction** as the business change.
* A **separate worker process** delivers the jobs to the mock notification service
  over HTTP, with retries, exponential backoff and crash recovery.
* No real email/SMS is sent. Delivery is simulated by the mock notification service.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # create tables if they do not exist yet (idempotent)
    yield


app = FastAPI(title="Workshop Registration API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "code": exc.code})


@app.get("/health", tags=["Health"], summary="Health check")
def health(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001 - report any DB failure as unhealthy
        database = "unavailable"
    status = "ok" if database == "ok" else "degraded"
    return JSONResponse(
        status_code=200 if status == "ok" else 503,
        content={"status": status, "service": "workshop-registration-api", "database": database,
                 "time": datetime.now(timezone.utc).isoformat()},
    )


app.include_router(workshops.router)
app.include_router(members.router)
app.include_router(registrations.router)
app.include_router(notifications.router)
