"""MOCK notification service.

*** This service only SIMULATES notification delivery. ***
*** It never sends a real email or SMS.                  ***

Run:  uvicorn mock_service.main:app --port 8001
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mock_service.database import build_engine, init_db, make_session_factory
from mock_service.models import MockDelivery

DESCRIPTION = """
**This is a MOCK service. It simulates notification delivery and does NOT send real email or SMS.**

* `POST /notifications` accepts a delivery request (idempotent on `notification_id`).
* `PUT /admin/config`, `POST /admin/fail-next`, `POST /admin/timeout-next`, `POST /admin/reset`
  control simulated failures and timeouts.
* `GET /deliveries` shows what was "delivered" (stored persistently in SQLite).
"""


# ---------------------------------------------------------------- behaviour
class BehaviorConfig(BaseModel):
    mode: Literal["normal", "error", "timeout"] = Field(default="normal", description="How to answer delivery requests.")
    remaining: int | None = Field(default=None, ge=1, description="Apply the mode to the next N requests, then return to normal. null = until reset.")
    error_status: int = Field(default=500, ge=400, le=599, description="HTTP status returned in 'error' mode.")
    timeout_seconds: float = Field(default=30.0, gt=0, le=300, description="How long to stall in 'timeout' mode.")
    record_before_timeout: bool = Field(default=False, description="Timeout mode only: store the delivery BEFORE stalling (receiver accepted it but the sender never sees the answer).")
    fail_recipients: list[str] = Field(default_factory=list, description="Emails whose deliveries always fail with error_status.")


@dataclass(frozen=True)
class Decision:
    kind: Literal["ok", "error", "timeout"]
    status: int = 500
    seconds: float = 0.0
    record_first: bool = False


class Behavior:
    """Thread-safe, in-memory failure-injection settings (reset on restart)."""

    def __init__(self, config: BehaviorConfig | None = None) -> None:
        self._lock = threading.Lock()
        self._config = config or BehaviorConfig()

    @classmethod
    def from_env(cls) -> "Behavior":
        env = os.environ.get
        remaining = env("MOCK_REMAINING")
        return cls(BehaviorConfig(
            mode=env("MOCK_MODE", "normal"),  # type: ignore[arg-type]
            remaining=int(remaining) if remaining else None,
            error_status=int(env("MOCK_ERROR_STATUS", "500")),
            timeout_seconds=float(env("MOCK_TIMEOUT_SECONDS", "30")),
        ))

    def snapshot(self) -> BehaviorConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def configure(self, config: BehaviorConfig) -> None:
        with self._lock:
            self._config = config.model_copy(deep=True)

    def reset(self) -> None:
        self.configure(BehaviorConfig())

    def decide(self, recipient_email: str) -> Decision:
        with self._lock:
            c = self._config
            if recipient_email.lower() in {e.lower() for e in c.fail_recipients}:
                return Decision("error", status=c.error_status)
            if c.mode == "normal":
                return Decision("ok")
            decision = (
                Decision("error", status=c.error_status)
                if c.mode == "error"
                else Decision("timeout", seconds=c.timeout_seconds, record_first=c.record_before_timeout)
            )
            if c.remaining is not None:
                left = c.remaining - 1
                self._config = c.model_copy(update={"remaining": left if left > 0 else None,
                                                    "mode": c.mode if left > 0 else "normal"})
            return decision


# ------------------------------------------------------------------ schemas
class DeliveryRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "notification_id": "registration_confirmation:1:1",
        "notification_type": "registration_confirmation",
        "recipient_member_id": 1, "recipient_name": "Asha Rao", "recipient_email": "asha@example.com",
        "workshop_id": 1, "workshop_title": "Intro to FastAPI", "message": "You are registered."}})

    notification_id: str = Field(min_length=1, max_length=200, description="Idempotency key: same id => stored once.")
    notification_type: str = Field(min_length=1, max_length=40)
    recipient_member_id: int
    recipient_name: str
    recipient_email: str
    workshop_id: int
    workshop_title: str
    message: str


class DeliveryResponse(BaseModel):
    status: Literal["delivered", "duplicate_ignored"]
    notification_id: str
    delivery_id: int
    duplicate_count: int
    note: str = "MOCK delivery only. No real email or SMS was sent."


class DeliveryRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    notification_id: str
    notification_type: str
    recipient_member_id: int
    recipient_name: str
    recipient_email: str
    workshop_id: int
    workshop_title: str
    message: str
    received_at: datetime
    duplicate_count: int


# ---------------------------------------------------------------------- app
def _setup(app: FastAPI, database_url: str) -> None:
    engine = build_engine(database_url)
    init_db(engine)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)


def create_app(database_url: str | None = None, behavior: Behavior | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "session_factory", None) is None:
            _setup(
                app,
                os.environ.get(
                    "MOCK_DATABASE_URL",
                    "sqlite:///./data/mock_notifications.db",
                ),
            )
        try:
            yield
        finally:
            engine = getattr(app.state, "engine", None)
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title="MOCK Notification Service",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.session_factory = None
    app.state.behavior = behavior or Behavior.from_env()

    if database_url:
        _setup(app, database_url)

    def store_delivery(request: Request, body: DeliveryRequest) -> tuple[MockDelivery, bool]:
        """Idempotent insert. Returns (row, created)."""
        with request.app.state.session_factory() as session, session.begin():
            inserted = session.execute(
                sqlite_insert(MockDelivery).values(
                    **body.model_dump(), received_at=datetime.now(timezone.utc), duplicate_count=0
                ).on_conflict_do_nothing(index_elements=["notification_id"])
            ).rowcount
            if not inserted:
                session.execute(update(MockDelivery).where(MockDelivery.notification_id == body.notification_id)
                                .values(duplicate_count=MockDelivery.duplicate_count + 1))
            row = session.execute(select(MockDelivery).where(MockDelivery.notification_id == body.notification_id)).scalar_one()
            return row, bool(inserted)

    @app.get("/health", tags=["Health"], summary="Health check")
    def health() -> dict:
        return {"status": "ok", "service": "mock-notification-service", "mock": True}

    @app.post("/notifications", response_model=DeliveryResponse, tags=["Delivery"],
              summary="Accept a (simulated) notification delivery",
              responses={200: {"model": DeliveryResponse, "description": "Duplicate notification_id: ignored, nothing stored twice."},
                         201: {"model": DeliveryResponse, "description": "New delivery stored."},
                         500: {"description": "Simulated failure."}, 504: {"description": "Simulated timeout."}})
    def deliver(body: DeliveryRequest, request: Request):
        decision = request.app.state.behavior.decide(body.recipient_email)
        if decision.kind == "error":
            raise HTTPException(status_code=decision.status, detail="Simulated failure (mock service)")
        if decision.kind == "timeout":
            if decision.record_first:
                store_delivery(request, body)  # receiver accepted it, but the sender will never see the answer
            time.sleep(decision.seconds)
            raise HTTPException(status_code=504, detail="Simulated timeout (mock service)")
        row, created = store_delivery(request, body)
        from fastapi.responses import JSONResponse

        payload = DeliveryResponse(status="delivered" if created else "duplicate_ignored",
                                   notification_id=row.notification_id, delivery_id=row.id,
                                   duplicate_count=row.duplicate_count)
        return JSONResponse(status_code=201 if created else 200, content=payload.model_dump())

    @app.get("/deliveries", response_model=list[DeliveryRecord], tags=["Inspect"], summary="List stored mock deliveries")
    def list_deliveries(request: Request, recipient_email: str | None = Query(default=None),
                        workshop_id: int | None = Query(default=None),
                        notification_type: str | None = Query(default=None)) -> list[MockDelivery]:
        stmt = select(MockDelivery).order_by(MockDelivery.id)
        if recipient_email:
            stmt = stmt.where(MockDelivery.recipient_email == recipient_email.lower())
        if workshop_id:
            stmt = stmt.where(MockDelivery.workshop_id == workshop_id)
        if notification_type:
            stmt = stmt.where(MockDelivery.notification_type == notification_type)
        with request.app.state.session_factory() as session, session.begin():
            return list(session.execute(stmt).scalars())

    @app.get("/admin/config", response_model=BehaviorConfig, tags=["Failure simulation"], summary="Show current simulated behaviour")
    def get_config(request: Request) -> BehaviorConfig:
        return request.app.state.behavior.snapshot()

    @app.put("/admin/config", response_model=BehaviorConfig, tags=["Failure simulation"], summary="Set simulated behaviour")
    def set_config(config: BehaviorConfig, request: Request) -> BehaviorConfig:
        request.app.state.behavior.configure(config)
        return request.app.state.behavior.snapshot()

    @app.post("/admin/fail-next", response_model=BehaviorConfig, tags=["Failure simulation"],
              summary="Fail the next N requests with an HTTP error")
    def fail_next(request: Request, count: int = Query(default=1, ge=1, le=1000),
                  status_code: int = Query(default=500, ge=400, le=599)) -> BehaviorConfig:
        request.app.state.behavior.configure(BehaviorConfig(mode="error", remaining=count, error_status=status_code))
        return request.app.state.behavior.snapshot()

    @app.post("/admin/timeout-next", response_model=BehaviorConfig, tags=["Failure simulation"],
              summary="Time out (stall) the next N requests")
    def timeout_next(request: Request, count: int = Query(default=1, ge=1, le=1000),
                     seconds: float = Query(default=30.0, gt=0, le=300),
                     record_before_timeout: bool = Query(default=False)) -> BehaviorConfig:
        request.app.state.behavior.configure(BehaviorConfig(
            mode="timeout", remaining=count, timeout_seconds=seconds, record_before_timeout=record_before_timeout))
        return request.app.state.behavior.snapshot()

    @app.post("/admin/reset", response_model=BehaviorConfig, tags=["Failure simulation"],
              summary="Restore normal delivery")
    def reset(request: Request, clear_deliveries: bool = Query(default=False, description="Also delete stored delivery records.")) -> BehaviorConfig:
        request.app.state.behavior.reset()
        if clear_deliveries:
            with request.app.state.session_factory() as session, session.begin():
                session.execute(delete(MockDelivery))
        return request.app.state.behavior.snapshot()

    return app


app = create_app()
