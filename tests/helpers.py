"""Test helpers: fake clock, scripted fake notification service, small DB readers."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.models import DeliveryAttempt, NotificationJob


def future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class FakeClock:
    """Controllable time for the worker. Starts 1 hour 'in the future' so that jobs
    created with the real clock are immediately eligible; tests then advance it."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc) + timedelta(hours=1)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeNotificationService:
    """In-process stand-in for the HTTP notification service (used via httpx.MockTransport).

    script: list consumed one entry per request. Entries:
      ("ok",) | ("status", 500) | ("timeout",) | ("connect_error",)
    When the script is empty every request succeeds.
    The receiver is idempotent: `delivered` is keyed by notification_id.
    """

    def __init__(self, script: list[tuple] | None = None, fail_emails: set[str] | None = None) -> None:
        self.script = list(script or [])
        self.fail_emails = set(fail_emails or ())
        self.requests: list[dict] = []
        self.delivered: dict[str, dict] = {}
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self._lock:
            self.requests.append(body)
            if body["recipient_email"] in self.fail_emails:
                return httpx.Response(500, json={"detail": "simulated failure"})
            action = self.script.pop(0) if self.script else ("ok",)
            kind = action[0]
            if kind == "status":
                return httpx.Response(action[1], json={"detail": "simulated"})
            if kind == "timeout":
                raise httpx.ReadTimeout("simulated timeout", request=request)
            if kind == "connect_error":
                raise httpx.ConnectError("simulated connection refused", request=request)
            self.delivered.setdefault(body["notification_id"], body)
            return httpx.Response(201, json={"status": "delivered"})


def get_job(session_factory, job_id: int) -> NotificationJob:
    with session_factory() as session, session.begin():
        return session.get(NotificationJob, job_id)


def get_attempts(session_factory, job_id: int) -> list[DeliveryAttempt]:
    with session_factory() as session, session.begin():
        return list(session.execute(
            select(DeliveryAttempt).where(DeliveryAttempt.job_id == job_id).order_by(DeliveryAttempt.attempt_number)
        ).scalars())


def all_jobs(session_factory, **filters) -> list[NotificationJob]:
    with session_factory() as session, session.begin():
        stmt = select(NotificationJob).order_by(NotificationJob.id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(NotificationJob, key) == value)
        return list(session.execute(stmt).scalars())
