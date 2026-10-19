import pytest

from tests.helpers import future


def test_create_workshop_success(client):
    response = client.post("/workshops", json={"title": "  Intro to FastAPI ", "description": "d",
                                              "starts_at": future(), "capacity": 25})
    assert response.status_code == 201
    body = response.json()
    assert body["id"] >= 1
    assert body["title"] == "Intro to FastAPI"  # whitespace stripped
    assert body["status"] == "scheduled"
    assert body["capacity"] == 25 and body["registered_count"] == 0 and body["seats_remaining"] == 25
    assert body["created_at"] and body["cancelled_at"] is None


@pytest.mark.parametrize("override", [
    {"capacity": 0}, {"capacity": -5}, {"capacity": 10_001}, {"title": "ab"}, {"title": ""},
    {"starts_at": "2000-01-01T10:00:00Z"}, {"starts_at": "not-a-date"},
])
def test_create_workshop_validation_errors(client, override):
    payload = {"title": "Valid title", "description": "", "starts_at": future(), "capacity": 5, **override}
    assert client.post("/workshops", json=payload).status_code == 422


def test_create_workshop_missing_fields(client):
    assert client.post("/workshops", json={"title": "Only a title"}).status_code == 422


def test_list_and_get_workshops(api, client):
    first, second = api.workshop(title="First one"), api.workshop(title="Second one")
    listing = client.get("/workshops").json()
    assert [w["id"] for w in listing] == [first["id"], second["id"]]
    single = client.get(f"/workshops/{second['id']}")
    assert single.status_code == 200 and single.json()["title"] == "Second one"


def test_get_missing_workshop_returns_404(client):
    response = client.get("/workshops/999")
    assert response.status_code == 404
    assert response.json()["code"] == "workshop_not_found"


def test_filter_workshops_by_status(api, client):
    keep, gone = api.workshop(title="Keep me"), api.workshop(title="Cancel me")
    assert api.cancel(gone["id"]).status_code == 200
    scheduled = client.get("/workshops", params={"status": "scheduled"}).json()
    cancelled = client.get("/workshops", params={"status": "cancelled"}).json()
    assert [w["id"] for w in scheduled] == [keep["id"]]
    assert [w["id"] for w in cancelled] == [gone["id"]]


def test_registered_count_and_seats_remaining(api, client):
    workshop = api.workshop(capacity=3)
    api.register_ok(workshop["id"], api.member()["id"])
    api.register_ok(workshop["id"], api.member()["id"])
    body = client.get(f"/workshops/{workshop['id']}").json()
    assert body["registered_count"] == 2 and body["seats_remaining"] == 1
