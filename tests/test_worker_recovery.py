from datetime import timedelta

from sqlalchemy import update

from app.database import build_engine, make_session_factory
from app.models import NotificationJob
from app.services import notification_service as svc
from tests.helpers import FakeNotificationService, get_attempts, get_job


def register_one(api):
    workshop, member = api.workshop(), api.member()
    return api.register_ok(workshop["id"], member["id"])["confirmation_notification_id"]


def test_pending_jobs_survive_worker_restart(api, settings, session_factory, make_worker, engine):
    job_ids = [register_one(api) for _ in range(3)]
    # "Stop" everything: the API/worker engine is closed. Only the SQLite file remains.
    engine.dispose()

    new_engine = build_engine(settings.database_url)  # brand-new process = brand-new connection
    new_factory = make_session_factory(new_engine)
    service = FakeNotificationService()
    worker = make_worker(service, sf=new_factory, worker_id="worker-after-restart")
    assert worker.process_once() == 3
    assert all(get_job(new_factory, j).status == "delivered" for j in job_ids)
    assert len(service.delivered) == 3
    new_engine.dispose()


def test_retrying_jobs_survive_worker_restart(api, settings, session_factory, make_worker, clock, engine):
    job_id = register_one(api)
    make_worker(FakeNotificationService(script=[("status", 500)]), worker_id="old-worker").process_once()
    assert get_job(session_factory, job_id).status == "retrying"
    engine.dispose()

    new_engine = build_engine(settings.database_url)
    new_factory = make_session_factory(new_engine)
    new_worker = make_worker(FakeNotificationService(), sf=new_factory, worker_id="new-worker")
    assert new_worker.process_once() == 0  # backoff time is stored in the DB and still respected
    clock.advance(2)
    assert new_worker.process_once() == 1
    job = get_job(new_factory, job_id)
    assert job.status == "delivered" and job.attempts == 2
    new_engine.dispose()


def test_job_left_processing_by_crashed_worker_is_recovered(api, settings, session_factory, make_worker, clock):
    job_id = register_one(api)
    # Simulate a worker that claimed the job and then died (kill -9) before finishing.
    with session_factory() as s:
        claimed = svc.claim_next_job(s, "crashed-worker", clock(), settings)
    assert claimed is not None
    job = get_job(session_factory, job_id)
    assert job.status == "processing" and job.locked_by == "crashed-worker" and job.attempts == 1

    service = FakeNotificationService()
    worker = make_worker(service)
    assert worker.process_once() == 0, "lease still valid: nobody may steal the job yet"
    assert get_job(session_factory, job_id).status == "processing"

    clock.advance(settings.lease_seconds + 1)  # lease expires
    assert worker.process_once() == 1
    job = get_job(session_factory, job_id)
    assert job.status == "delivered" and job.attempts == 2 and job.locked_by is None
    assert len(service.delivered) == 1
    assert [a.attempt_number for a in get_attempts(session_factory, job_id)] == [2]


def test_explicit_recovery_requeues_expired_jobs(api, settings, session_factory, make_worker, clock):
    job_id = register_one(api)
    with session_factory() as s:
        svc.claim_next_job(s, "crashed-worker", clock(), settings)
    worker = make_worker()
    assert worker.recover_stuck_jobs() == 0  # lease not expired yet
    clock.advance(settings.lease_seconds + 1)
    assert worker.recover_stuck_jobs() == 1
    job = get_job(session_factory, job_id)
    assert job.status == "retrying" and job.locked_by is None and "lease expired" in job.last_error


def test_crash_on_final_attempt_marks_job_permanently_failed(api, settings, session_factory, make_worker, clock):
    job_id = register_one(api)
    with session_factory() as s, s.begin():  # a job that already used its last attempt and then crashed
        s.execute(update(NotificationJob).where(NotificationJob.id == job_id).values(
            status="processing", attempts=settings.max_delivery_attempts, locked_by="dead",
            lease_expires_at=clock() - timedelta(seconds=1)))
    service = FakeNotificationService()
    assert make_worker(service).process_once() == 0
    job = get_job(session_factory, job_id)
    assert job.status == "permanently_failed" and service.requests == []


def test_stale_worker_result_is_ignored_after_lease_takeover(api, settings, session_factory, clock):
    job_id = register_one(api)
    with session_factory() as s:
        old = svc.claim_next_job(s, "old-worker", clock(), settings)
    clock.advance(settings.lease_seconds + 1)
    with session_factory() as s:
        new = svc.claim_next_job(s, "new-worker", clock(), settings)  # takes over after recovery
    assert new is not None and new.attempts == 2

    ok = svc.DeliveryResult(True, "success", True, 201, None)
    with session_factory() as s:  # the old worker wakes up and reports late
        assert svc.finalize_attempt(s, old, "old-worker", ok, clock(), clock(), settings) == "lease_lost"
    assert get_job(session_factory, job_id).status == "processing"  # still owned by new-worker
    with session_factory() as s:
        assert svc.finalize_attempt(s, new, "new-worker", ok, clock(), clock(), settings) == "delivered"
    assert get_job(session_factory, job_id).status == "delivered"


def test_unexpected_error_in_delivery_does_not_kill_worker(api, session_factory, make_worker, monkeypatch):
    bad, good = register_one(api), register_one(api)
    worker = make_worker(FakeNotificationService())
    original = worker.http.post
    state = {"n": 0}

    def flaky_post(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("unexpected programming error")
        return original(*args, **kwargs)

    monkeypatch.setattr(worker.http, "post", flaky_post)
    assert worker.process_once() == 2
    assert get_job(session_factory, bad).status == "retrying"
    assert get_attempts(session_factory, bad)[0].outcome == "internal_error"
    assert get_job(session_factory, good).status == "delivered"
