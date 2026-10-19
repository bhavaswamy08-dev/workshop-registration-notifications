"""The database itself enforces integrity (second line of defence behind the service checks)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Member, NotificationJob, Registration, Workshop


def _workshop(**kw):
    return Workshop(title="W", description="", starts_at=datetime.now(timezone.utc) + timedelta(days=1),
                    capacity=kw.pop("capacity", 5), **kw)


def _job(member_id, workshop_id, key="k1", **kw):
    return NotificationJob(notification_type=kw.pop("notification_type", "registration_confirmation"),
                           member_id=member_id, workshop_id=workshop_id, idempotency_key=key,
                           payload={}, status=kw.pop("status", "pending"), attempts=0, **kw)


def test_unique_registration_per_member_and_workshop(session_factory):
    with session_factory() as s, s.begin():
        w, m = _workshop(), Member(name="A", email="a@example.com")
        s.add_all([w, m]); s.flush()
        s.add(Registration(workshop_id=w.id, member_id=m.id))
        s.flush()
        ids = (w.id, m.id)
    with pytest.raises(IntegrityError):
        with session_factory() as s, s.begin():
            s.add(Registration(workshop_id=ids[0], member_id=ids[1]))


def test_unique_idempotency_key(session_factory):
    with session_factory() as s, s.begin():
        w, m = _workshop(), Member(name="A", email="a@example.com")
        s.add_all([w, m]); s.flush()
        s.add(_job(m.id, w.id, "same-key")); s.flush()
        ids = (w.id, m.id)
    with pytest.raises(IntegrityError):
        with session_factory() as s, s.begin():
            s.add(_job(ids[1], ids[0], "same-key"))


def test_foreign_keys_are_enforced(session_factory):
    with pytest.raises(IntegrityError):
        with session_factory() as s, s.begin():
            s.add(Registration(workshop_id=4242, member_id=4242))


def test_check_constraints(session_factory):
    with pytest.raises(IntegrityError):
        with session_factory() as s, s.begin():
            s.add(_workshop(capacity=0))
    with pytest.raises(IntegrityError):
        with session_factory() as s, s.begin():
            s.add(_workshop(status="exploded"))
    with session_factory() as s, s.begin():
        w, m = _workshop(), Member(name="A", email="a@example.com")
        s.add_all([w, m]); s.flush()
        ids = (w.id, m.id)
    with pytest.raises(IntegrityError):
        with session_factory() as s, s.begin():
            s.add(_job(ids[1], ids[0], "k", status="bogus"))


def test_unique_member_email(session_factory):
    with session_factory() as s, s.begin():
        s.add(Member(name="A", email="same@example.com"))
    with pytest.raises(IntegrityError):
        with session_factory() as s, s.begin():
            s.add(Member(name="B", email="same@example.com"))
