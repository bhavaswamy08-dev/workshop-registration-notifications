import random
from datetime import timedelta
from dataclasses import replace

import pytest

from app.config import Settings
from app.services.notification_service import compute_backoff_delay
from tests.helpers import FakeNotificationService, get_attempts, get_job


def setup_registration(api, **workshop_kwargs):
    workshop, member = api.workshop(**workshop_kwargs), api.member()
    registration = api.register_ok(workshop["id"], member["id"])
    return workshop, member, registration["confirmation_notification_id"]


# ---------------------------------------------------------------- happy path
def test_confirmation_notification_is_delivered(api, client, session_factory, make_worker):
    workshop, member, job_id = setup_registration(api)
    service = FakeNotificationService()
    worker = make_worker(service)

    assert worker.process_once() == 1

    job = get_job(session_factory, job_id)
    assert job.status == "delivered" and job.attempts == 1 and job.delivered_at is not None
    assert job.last_error is None and job.locked_by is None and job.lease_expires_at is None
    key = f"registration_confirmation:{workshop['id']}:{member['id']}"
    assert key in service.delivered
    sent = service.delivered[key]
    assert sent["recipient_email"] == member["email"] and sent["notification_type"] == "registration_confirmation"
    attempts = get_attempts(session_factory, job_id)
    assert [(a.attempt_number, a.outcome, a.http_status) for a in attempts] == [(1, "success", 201)]

    detail = client.get(f"/notifications/{job_id}").json()
    assert detail["status"] == "delivered" and len(detail["delivery_attempts"]) == 1
    assert client.get("/notifications/summary").json() == {"total": 1, "by_status": {"delivered": 1}}


def test_registration_returns_before_notification_delivery(api, session_factory):
    """No worker is running and no notification service exists, yet registration succeeds."""
    _, _, job_id = setup_registration(api)
    job = get_job(session_factory, job_id)
    assert job.status == "pending" and job.attempts == 0
    assert get_attempts(session_factory, job_id) == []


def test_api_never_makes_outbound_http_calls(api, monkeypatch):
    """The API process only writes to SQLite; delivery is the worker's job."""
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("the API process must not make HTTP calls to the notification service")

    # Patch the REAL network transports (the TestClient uses its own in-process transport).
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    setup_registration(api)


# ------------------------------------------------------------------- retries
def test_notification_service_unavailable_then_recovers(api, session_factory, make_worker, clock):
    _, _, job_id = setup_registration(api)
    service = FakeNotificationService(script=[("connect_error",), ("connect_error",)])
    worker = make_worker(service)

    worker.process_once()
    job = get_job(session_factory, job_id)
    assert job.status == "retrying" and job.attempts == 1
    assert "Connection error" in job.last_error
    assert job.next_retry_at == clock.now + timedelta(seconds=2)

    assert worker.process_once() == 0, "job must not be retried before next_retry_at"
    clock.advance(2)
    worker.process_once()
    assert get_job(session_factory, job_id).status == "retrying"
    clock.advance(4)
    worker.process_once()
    job = get_job(session_factory, job_id)
    assert job.status == "delivered" and job.attempts == 3
    outcomes = [a.outcome for a in get_attempts(session_factory, job_id)]
    assert outcomes == ["connection_error", "connection_error", "success"]


def test_retry_after_http_500(api, session_factory, make_worker, clock):
    _, _, job_id = setup_registration(api)
    worker = make_worker(FakeNotificationService(script=[("status", 500)]))
    worker.process_once()
    job = get_job(session_factory, job_id)
    assert job.status == "retrying" and job.last_error == "HTTP 500 from notification service"
    attempts = get_attempts(session_factory, job_id)
    assert attempts[0].outcome == "http_error" and attempts[0].http_status == 500
    clock.advance(2)
    worker.process_once()
    assert get_job(session_factory, job_id).status == "delivered"


def test_retry_after_timeout(api, session_factory, make_worker, clock):
    _, _, job_id = setup_registration(api)
    worker = make_worker(FakeNotificationService(script=[("timeout",)]))
    worker.process_once()
    job = get_job(session_factory, job_id)
    assert job.status == "retrying" and "timed out" in job.last_error
    assert get_attempts(session_factory, job_id)[0].outcome == "timeout"
    clock.advance(2)
    worker.process_once()
    assert get_job(session_factory, job_id).status == "delivered"


def test_eventual_delivery_after_temporary_failures(api, session_factory, make_worker, clock):
    _, _, job_id = setup_registration(api)
    worker = make_worker(FakeNotificationService(script=[("status", 503), ("timeout",), ("status", 500), ("connect_error",)]))
    for _ in range(4):
        worker.process_once()
        assert get_job(session_factory, job_id).status == "retrying"
        clock.advance(61)
    worker.process_once()  # 5th attempt succeeds (still within the limit)
    job = get_job(session_factory, job_id)
    assert job.status == "delivered" and job.attempts == 5
    assert len(get_attempts(session_factory, job_id)) == 5


def test_permanent_failure_after_retry_limit(api, session_factory, make_worker, clock):
    _, _, job_id = setup_registration(api)
    service = FakeNotificationService(script=[("status", 500)] * 10)
    worker = make_worker(service)
    for _ in range(5):
        worker.process_once()
        clock.advance(61)
    job = get_job(session_factory, job_id)
    assert job.status == "permanently_failed" and job.attempts == 5
    assert job.last_error and job.delivered_at is None
    assert len(service.requests) == 5
    # No more attempts are ever made for a permanently failed job.
    clock.advance(10_000)
    assert worker.process_once() == 0
    assert len(service.requests) == 5


def test_exponential_backoff_schedule_is_exact(api, session_factory, make_worker, clock):
    _, _, job_id = setup_registration(api)
    worker = make_worker(FakeNotificationService(script=[("status", 500)] * 5))
    delays = []
    for _ in range(4):
        worker.process_once()
        delays.append((get_job(session_factory, job_id).next_retry_at - clock.now).total_seconds())
        clock.advance(delays[-1])
    assert delays == [2.0, 4.0, 8.0, 16.0]


def test_backoff_is_capped_and_jitter_is_bounded():
    cfg = Settings(retry_base_delay_seconds=2, retry_max_delay_seconds=10, retry_jitter_ratio=0.0)
    assert [compute_backoff_delay(n, cfg) for n in (1, 2, 3, 4, 5, 10)] == [2, 4, 8, 10, 10, 10]
    jittery = replace(cfg, retry_jitter_ratio=0.5)
    rng = random.Random(42)
    for attempt in (1, 2, 3, 6):
        base = min(10, 2 * 2 ** (attempt - 1))
        for _ in range(50):
            value = compute_backoff_delay(attempt, jittery, rng)
            assert 0 <= value <= 10 and base * 0.5 - 1e-9 <= value or value == 10


def test_client_error_is_not_retried_but_429_is(api, session_factory, make_worker, clock):
    _, _, bad_job = setup_registration(api)
    worker = make_worker(FakeNotificationService(script=[("status", 400)]))
    worker.process_once()
    assert get_job(session_factory, bad_job).status == "permanently_failed"

    _, _, limited_job = setup_registration(api)
    worker2 = make_worker(FakeNotificationService(script=[("status", 429)]))
    worker2.process_once()
    assert get_job(session_factory, limited_job).status == "retrying"


# ------------------------------------------------------------- independence
def test_one_failed_recipient_does_not_block_another(api, session_factory, make_worker, clock):
    workshop = api.workshop()
    bad, good = api.member(email="bad@example.com"), api.member(email="good@example.com")
    bad_job = api.register_ok(workshop["id"], bad["id"])["confirmation_notification_id"]
    good_job = api.register_ok(workshop["id"], good["id"])["confirmation_notification_id"]
    service = FakeNotificationService(fail_emails={"bad@example.com"})
    worker = make_worker(service)

    worker.process_once()
    assert get_job(session_factory, bad_job).status == "retrying"
    assert get_job(session_factory, good_job).status == "delivered"

    service.fail_emails.clear()  # the problem is fixed
    clock.advance(2)
    worker.process_once()
    assert get_job(session_factory, bad_job).status == "delivered"
    assert get_job(session_factory, good_job).attempts == 1  # untouched, not re-sent


def test_failing_job_first_in_queue_does_not_starve_later_jobs(api, session_factory, make_worker):
    workshop = api.workshop()
    members = [api.member(email=f"m{i}@example.com") for i in range(5)]
    jobs = [api.register_ok(workshop["id"], m["id"])["confirmation_notification_id"] for m in members]
    worker = make_worker(FakeNotificationService(fail_emails={"m0@example.com"}))
    assert worker.process_once() == 5
    statuses = [get_job(session_factory, j).status for j in jobs]
    assert statuses == ["retrying", "delivered", "delivered", "delivered", "delivered"]


# ---------------------------------------------------------------------- API
def test_job_listing_and_filters(api, make_worker):
    workshop = api.workshop()
    m1, m2 = api.member(), api.member()
    api.register_ok(workshop["id"], m1["id"])
    api.register_ok(workshop["id"], m2["id"])
    make_worker(FakeNotificationService(fail_emails={m2["email"]})).process_once()
    assert len(api.jobs()) == 2
    assert [j["member_id"] for j in api.jobs(status="delivered")] == [m1["id"]]
    assert [j["member_id"] for j in api.jobs(status="retrying")] == [m2["id"]]
    assert len(api.jobs(workshop_id=workshop["id"])) == 2
    assert api.client.get("/notifications/9999").status_code == 404
    assert api.client.get("/notifications", params={"status": "nonsense"}).status_code == 422
