import pytest
from sqlalchemy import func, select

from app.models import NotificationJob, Registration
from tests.helpers import all_jobs


def count(session_factory, model) -> int:
    with session_factory() as s, s.begin():
        return s.scalar(select(func.count()).select_from(model))


def test_successful_registration_creates_pending_confirmation_job(api, session_factory):
    workshop, member = api.workshop(), api.member()
    response = api.register(workshop["id"], member["id"])
    assert response.status_code == 201
    body = response.json()
    assert body["workshop_id"] == workshop["id"] and body["member_id"] == member["id"]
    assert "queued" in body["message"]
    assert body["confirmation_status"] == "pending"

    # The registration AND the job are already persisted when the response arrives.
    jobs = all_jobs(session_factory)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == body["confirmation_notification_id"]
    assert job.notification_type == "registration_confirmation"
    assert (job.member_id, job.workshop_id, job.registration_id) == (member["id"], workshop["id"], body["id"])
    assert job.status == "pending" and job.attempts == 0 and job.delivered_at is None
    assert job.payload["recipient_email"] == member["email"]
    assert count(session_factory, Registration) == 1


def test_duplicate_registration_rejected(api, session_factory):
    workshop, member = api.workshop(), api.member()
    api.register_ok(workshop["id"], member["id"])
    response = api.register(workshop["id"], member["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "already_registered"
    assert count(session_factory, Registration) == 1
    assert count(session_factory, NotificationJob) == 1  # no second confirmation


def test_registration_for_nonexistent_workshop(api, session_factory):
    member = api.member()
    response = api.register(9999, member["id"])
    assert response.status_code == 404 and response.json()["code"] == "workshop_not_found"
    assert count(session_factory, NotificationJob) == 0


def test_registration_for_nonexistent_member(api, session_factory):
    workshop = api.workshop()
    response = api.register(workshop["id"], 9999)
    assert response.status_code == 404 and response.json()["code"] == "member_not_found"
    assert count(session_factory, Registration) == 0
    assert count(session_factory, NotificationJob) == 0


def test_registration_when_workshop_is_full(api, session_factory):
    workshop = api.workshop(capacity=2)
    api.register_ok(workshop["id"], api.member()["id"])
    api.register_ok(workshop["id"], api.member()["id"])
    response = api.register(workshop["id"], api.member()["id"])
    assert response.status_code == 409 and response.json()["code"] == "workshop_full"
    assert count(session_factory, Registration) == 2
    assert count(session_factory, NotificationJob) == 2  # failed attempt produced no job


def test_registration_after_cancellation(api, session_factory):
    workshop, member = api.workshop(), api.member()
    assert api.cancel(workshop["id"]).status_code == 200
    response = api.register(workshop["id"], member["id"])
    assert response.status_code == 409 and response.json()["code"] == "workshop_cancelled"
    assert count(session_factory, Registration) == 0
    assert count(session_factory, NotificationJob) == 0


@pytest.mark.parametrize("payload", [{}, {"workshop_id": 1}, {"workshop_id": 0, "member_id": 1},
                                     {"workshop_id": "x", "member_id": 1}])
def test_registration_validation(client, payload):
    assert client.post("/registrations", json=payload).status_code == 422


def test_list_and_get_registrations(api, client):
    workshop, other = api.workshop(), api.workshop()
    m1, m2 = api.member(), api.member()
    r1 = api.register_ok(workshop["id"], m1["id"])
    api.register_ok(other["id"], m2["id"])
    assert len(client.get("/registrations").json()) == 2
    assert [r["id"] for r in client.get(f"/workshops/{workshop['id']}/registrations").json()] == [r1["id"]]
    assert [r["id"] for r in client.get("/registrations", params={"member_id": m2["id"]}).json()] != [r1["id"]]
    assert client.get(f"/registrations/{r1['id']}").json()["member_id"] == m1["id"]
    assert client.get("/registrations/777").status_code == 404
    assert client.get("/workshops/777/registrations").status_code == 404


def test_failure_while_creating_job_rolls_back_registration(api, client, session_factory, monkeypatch):
    """Requirement 24: no orphaned registration if job creation blows up."""
    workshop, member = api.workshop(), api.member()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash while creating the notification job")

    monkeypatch.setattr("app.services.registration_service.create_confirmation_job", boom)
    with pytest.raises(RuntimeError):
        api.register(workshop["id"], member["id"])
    monkeypatch.undo()

    assert count(session_factory, Registration) == 0, "orphaned registration left behind"
    assert count(session_factory, NotificationJob) == 0
    # The member can still register normally afterwards.
    assert api.register(workshop["id"], member["id"]).status_code == 201
