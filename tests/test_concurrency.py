"""Real concurrency: many simultaneous requests / workers against one SQLite file."""
import asyncio
import threading
from collections import Counter

import httpx
from sqlalchemy import func, select

from app.main import app
from app.models import NotificationJob, Registration
from app.services import notification_service as svc
from tests.helpers import FakeNotificationService, all_jobs


def post_concurrently(requests: list[tuple[str, dict | None]]) -> list[httpx.Response]:
    """Fire all requests at the ASGI app at the same time (sync endpoints run in a thread pool)."""
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await asyncio.gather(*[ac.post(path, json=body) for path, body in requests])

    return asyncio.run(run())


def count(session_factory, model) -> int:
    with session_factory() as s, s.begin():
        return s.scalar(select(func.count()).select_from(model))


def test_concurrent_duplicate_registrations_create_only_one(api, session_factory):
    workshop, member = api.workshop(), api.member()
    payload = {"workshop_id": workshop["id"], "member_id": member["id"]}
    responses = post_concurrently([("/registrations", payload)] * 20)

    codes = Counter(r.status_code for r in responses)
    assert codes == {201: 1, 409: 19}, codes
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"already_registered"}
    assert count(session_factory, Registration) == 1
    assert count(session_factory, NotificationJob) == 1


def test_concurrent_registrations_cannot_exceed_capacity(api, session_factory):
    workshop = api.workshop(capacity=3)
    members = [api.member() for _ in range(15)]
    responses = post_concurrently(
        [("/registrations", {"workshop_id": workshop["id"], "member_id": m["id"]}) for m in members])

    codes = Counter(r.status_code for r in responses)
    assert codes == {201: 3, 409: 12}, codes
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"workshop_full"}
    assert count(session_factory, Registration) == 3
    assert count(session_factory, NotificationJob) == 3  # exactly one job per successful registration


def test_concurrent_cancellations_create_each_job_once(api, session_factory):
    workshop = api.workshop()
    for _ in range(4):
        api.register_ok(workshop["id"], api.member()["id"])
    responses = post_concurrently([(f"/workshops/{workshop['id']}/cancel", None)] * 8)

    assert all(r.status_code == 200 for r in responses)
    bodies = [r.json() for r in responses]
    assert sum(b["notifications_created"] for b in bodies) == 4
    assert sum(1 for b in bodies if not b["already_cancelled"]) == 1
    assert len(all_jobs(session_factory, notification_type="workshop_cancellation")) == 4


def test_two_workers_never_claim_the_same_job(api, settings, session_factory, clock):
    workshop = api.workshop(capacity=40)
    for _ in range(30):
        api.register_ok(workshop["id"], api.member()["id"])

    claimed: dict[str, list[int]] = {}
    barrier = threading.Barrier(4)

    def claim_loop(name: str):
        mine = []
        barrier.wait()
        while True:
            with session_factory() as s:
                job = svc.claim_next_job(s, name, clock(), settings)
            if job is None:
                break
            mine.append(job.id)
        claimed[name] = mine

    threads = [threading.Thread(target=claim_loop, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    all_claimed = [j for ids in claimed.values() for j in ids]
    assert len(all_claimed) == 30 and len(set(all_claimed)) == 30, "a job was claimed twice (or lost)"
    assert all(j.attempts == 1 and j.status == "processing" for j in all_jobs(session_factory))


def test_multiple_running_workers_deliver_each_job_exactly_once(api, session_factory, make_worker):
    workshop = api.workshop(capacity=40)
    for _ in range(24):
        api.register_ok(workshop["id"], api.member()["id"])
    service = FakeNotificationService()
    workers = [make_worker(service, worker_id=f"w{i}") for i in range(3)]
    threads = [threading.Thread(target=w.process_once) for w in workers]
    [t.start() for t in threads]
    [t.join() for t in threads]

    jobs = all_jobs(session_factory)
    assert all(j.status == "delivered" and j.attempts == 1 for j in jobs)
    assert len(service.requests) == 24, "each job must be sent exactly once when nothing fails"
