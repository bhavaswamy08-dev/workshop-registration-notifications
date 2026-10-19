import pytest


def test_create_member_success(client):
    response = client.post("/members", json={"name": " Asha Rao ", "email": "Asha@Example.com"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Asha Rao"
    assert body["email"] == "asha@example.com"  # normalised to lower case
    assert body["id"] >= 1


def test_duplicate_email_rejected(client):
    client.post("/members", json={"name": "A", "email": "dup@example.com"})
    response = client.post("/members", json={"name": "B", "email": "DUP@example.com"})
    assert response.status_code == 409
    assert response.json()["code"] == "email_taken"


@pytest.mark.parametrize("email", ["plainaddress", "a@b", "@example.com", "a b@example.com", ""])
def test_invalid_email_rejected(client, email):
    assert client.post("/members", json={"name": "X", "email": email}).status_code == 422


def test_empty_name_rejected(client):
    assert client.post("/members", json={"name": "  ", "email": "x@example.com"}).status_code == 422


def test_list_and_get_members(api, client):
    a, b = api.member(), api.member()
    assert [m["id"] for m in client.get("/members").json()] == [a["id"], b["id"]]
    assert client.get(f"/members/{b['id']}").json()["email"] == b["email"]


def test_get_missing_member_returns_404(client):
    response = client.get("/members/12345")
    assert response.status_code == 404 and response.json()["code"] == "member_not_found"
