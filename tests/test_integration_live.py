"""Main API database + REAL worker HTTP client + REAL mock service (uvicorn in a thread).

These tests prove the two applications talk to each other over real HTTP, including real
connection errors and real client-side timeouts.
"""
from dataclasses import replace

import httpx

from app.worker.notification_worker import NotificationWorker
from tests.conftest import free_port
from tests.helpers import get_attempts, get_job


def live_worker(settings, session_factory, clock, url, timeout=2.0):
    cfg = replace(settings, notification_service_url=url)
    http = httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0))
    return NotificationWorker(
        session_factory,
        cfg,
        http_client=http,
        clock=clock,
        worker_id="live-worker",
    ), http


def register_one(api, email="live@example.com"):
    workshop, member = api.workshop(), api.member(email=email)
    return api.register_ok(workshop["id"], member["id"])["confirmation_notification_id"], workshop, member


def test_end_to_end_delivery_over_real_http(api, settings, session_factory, clock, live_mock):
    job_id, workshop, member = register_one(api)
    worker, http = live_worker(settings, session_factory, clock, live_mock.notifications_url)
    with http:
        assert worker.process_once() == 1
    assert get_job(session_factory, job_id).status == "delivered"
    records = httpx.get(f"{live_mock.url}/deliveries").json()
    assert len(records) == 1
    assert records[0]["notification_id"] == f"registration_confirmation:{workshop['id']}:{member['id']}"
    assert records[0]["recipient_email"] == "live@example.com"


def test_simulated_failures_are_retried_until_delivery(api, settings, session_factory, clock, live_mock):
    job_id, *_ = register_one(api)
    httpx.post(f"{live_mock.url}/admin/fail-next", params={"count": 2, "status_code": 500}).raise_for_status()
    worker, http = live_worker(settings, session_factory, clock, live_mock.notifications_url)
    with http:
        for wait in (2, 4, 0):
            worker.process_once()
            clock.advance(wait)
    job = get_job(session_factory, job_id)
    assert job.status == "delivered" and job.attempts == 3
    assert [a.outcome for a in get_attempts(session_factory, job_id)] == ["http_error", "http_error", "success"]
    assert len(httpx.get(f"{live_mock.url}/deliveries").json()) == 1


def test_real_client_timeout_then_idempotent_redelivery(api, settings, session_factory, clock, live_mock):
    """Receiver accepted the notification but answered too slowly. The retry must not duplicate it."""
    job_id, *_ = register_one(api)
    httpx.post(f"{live_mock.url}/admin/timeout-next",
               params={"count": 1, "seconds": 1.0, "record_before_timeout": True}).raise_for_status()
    worker, http = live_worker(settings, session_factory, clock, live_mock.notifications_url, timeout=0.3)
    with http:
        worker.process_once()
        first = get_attempts(session_factory, job_id)[0]
        assert first.outcome == "timeout"
        assert get_job(session_factory, job_id).status == "retrying"
        assert len(httpx.get(f"{live_mock.url}/deliveries").json()) == 1  # receiver HAD accepted it

        clock.advance(2)
        worker.process_once()
    assert get_job(session_factory, job_id).status == "delivered"
    records = httpx.get(f"{live_mock.url}/deliveries").json()
    assert len(records) == 1 and records[0]["duplicate_count"] == 1  # idempotent: still ONE record


def test_connection_refused_when_service_is_down(api, settings, session_factory, clock):
    job_id, *_ = register_one(api)
    dead_url = f"http://127.0.0.1:{free_port()}/notifications"  # nothing is listening here
    worker, http = live_worker(settings, session_factory, clock, dead_url)
    with http:
        worker.process_once()
    job = get_job(session_factory, job_id)
    print("JOB STATUS:", job.status)
    print("LAST ERROR:", repr(job.last_error))
    assert job.status == "retrying" and "Connection error" in job.last_error
    assert get_attempts(session_factory, job_id)[0].outcome == "connection_error"
