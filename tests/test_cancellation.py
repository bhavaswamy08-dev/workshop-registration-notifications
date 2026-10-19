import pytest

from tests.helpers import FakeNotificationService, all_jobs, get_job


def cancellation_jobs(session_factory):
    return all_jobs(session_factory, notification_type="workshop_cancellation")


def test_cancellation_notifies_every_registered_member(api, session_factory, make_worker):
    workshop = api.workshop()
    members = [api.member() for _ in range(3)]
    for m in members:
        api.register_ok(workshop["id"], m["id"])

    response = api.cancel(workshop["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["workshop"]["status"] == "cancelled" and body["workshop"]["cancelled_at"]
    assert body["already_cancelled"] is False and body["notifications_created"] == 3

    jobs = cancellation_jobs(session_factory)
    assert sorted(j.member_id for j in jobs) == sorted(m["id"] for m in members)
    assert all(j.status == "pending" for j in jobs), "cancel returns before delivery"

    service = FakeNotificationService()
    make_worker(service).process_once()
    expected = {f"workshop_cancellation:{workshop['id']}:{m['id']}" for m in members}
    assert expected <= set(service.delivered)
    assert all(get_job(session_factory, j.id).status == "delivered" for j in cancellation_jobs(session_factory))


def test_cancellation_does_not_notify_unregistered_members(api, session_factory, make_worker):
    workshop, other_workshop = api.workshop(), api.workshop()
    registered, outsider, elsewhere = api.member(), api.member(), api.member()
    api.register_ok(workshop["id"], registered["id"])
    api.register_ok(other_workshop["id"], elsewhere["id"])  # registered, but for a different workshop
    api.cancel(workshop["id"])

    jobs = cancellation_jobs(session_factory)
    assert [j.member_id for j in jobs] == [registered["id"]]
    assert outsider["id"] not in {j.member_id for j in all_jobs(session_factory)}
    assert elsewhere["id"] not in {j.member_id for j in jobs}
    # the other workshop is untouched
    assert api.client.get(f"/workshops/{other_workshop['id']}").json()["status"] == "scheduled"


def test_duplicate_cancellation_does_not_create_duplicate_jobs(api, session_factory):
    workshop = api.workshop()
    for _ in range(3):
        api.register_ok(workshop["id"], api.member()["id"])
    first = api.cancel(workshop["id"]).json()
    second = api.cancel(workshop["id"])
    third = api.cancel(workshop["id"]).json()

    assert first["notifications_created"] == 3 and first["already_cancelled"] is False
    assert second.status_code == 200
    assert second.json()["already_cancelled"] is True and second.json()["notifications_created"] == 0
    assert third["notifications_created"] == 0
    assert len(cancellation_jobs(session_factory)) == 3
    assert len({j.idempotency_key for j in cancellation_jobs(session_factory)}) == 3


def test_cancel_workshop_without_registrations(api, session_factory):
    workshop = api.workshop()
    body = api.cancel(workshop["id"]).json()
    assert body["workshop"]["status"] == "cancelled" and body["notifications_created"] == 0
    assert all_jobs(session_factory) == []


def test_cancel_missing_workshop_returns_404(api):
    response = api.cancel(4040)
    assert response.status_code == 404 and response.json()["code"] == "workshop_not_found"


def test_cancellation_delivery_failure_for_one_member_does_not_block_others(api, session_factory, make_worker, clock):
    workshop = api.workshop()
    bad, good1, good2 = api.member(email="bad@example.com"), api.member(), api.member()
    for m in (bad, good1, good2):
        api.register_ok(workshop["id"], m["id"])
    api.cancel(workshop["id"])

    service = FakeNotificationService(fail_emails={"bad@example.com"})
    worker = make_worker(service)
    worker.process_once()  # delivers 3 confirmations (bad one fails) + 3 cancellations (bad one fails)

    by_member = {(j.member_id, j.notification_type): j.status for j in all_jobs(session_factory)}
    assert by_member[(good1["id"], "workshop_cancellation")] == "delivered"
    assert by_member[(good2["id"], "workshop_cancellation")] == "delivered"
    assert by_member[(bad["id"], "workshop_cancellation")] == "retrying"


def test_cancellation_is_atomic(api, client, session_factory, monkeypatch):
    """If creating jobs fails half-way, the workshop must NOT end up cancelled."""
    workshop = api.workshop()
    for _ in range(3):
        api.register_ok(workshop["id"], api.member()["id"])
    calls = {"n": 0}
    original = __import__("app.services.cancellation_service", fromlist=["build_payload"]).build_payload

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash after the first job was inserted")
        return original(*args, **kwargs)

    monkeypatch.setattr("app.services.cancellation_service.build_payload", flaky)
    with pytest.raises(RuntimeError):
        api.cancel(workshop["id"])
    monkeypatch.undo()

    assert client.get(f"/workshops/{workshop['id']}").json()["status"] == "scheduled"
    assert cancellation_jobs(session_factory) == []
    retry = api.cancel(workshop["id"]).json()  # a clean retry works
    assert retry["notifications_created"] == 3 and retry["already_cancelled"] is False


def test_member_gets_both_confirmation_and_cancellation(api, session_factory):
    workshop, member = api.workshop(), api.member()
    api.register_ok(workshop["id"], member["id"])
    api.cancel(workshop["id"])
    types = sorted(j.notification_type for j in all_jobs(session_factory, member_id=member["id"]))
    assert types == ["registration_confirmation", "workshop_cancellation"]
