import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "test.sqlite"))
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.fixture
def seeded(client):
    from app.importer import import_records
    rows = [
        {"id": "b", "date": "2024-12-30T00:00:00", "location": "London", "species": "Example tick", "latinName": "Example species"},
        {"id": "a", "date": "2024-12-30T00:00:00", "location": "London"},
        {"id": "c", "date": "2024-12-31T23:59:59.999999", "location": "Bristol"},
        {"id": "d", "date": "2025-01-01T00:00:00", "location": "London"},
        {"id": "e", "date": "2025-01-06T12:00:00", "location": "Bristol"},
    ]
    import_records(rows, client.app.state.db, client.app.state.sighting)
    return client


def test_empty_search(client):
    assert client.get("/sightings").json() == {"items": [], "total": 0, "page": 1, "page_size": 50}


def test_date_location_and_serialization(seeded):
    response = seeded.get("/sightings", params={"start_date": "2024-12-30", "end_date": "2024-12-31", "location": " london "})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [row["id"] for row in body["items"]] == ["a", "b"]
    assert body["items"][1]["latinName"] == "Example species"
    assert body["items"][0]["species"] is None
    assert body["items"][0]["date"] == "2024-12-30T00:00:00"


def test_inclusive_end_date_and_open_ranges(seeded):
    assert seeded.get("/sightings?end_date=2024-12-31").json()["total"] == 3
    assert seeded.get("/sightings?start_date=2025-01-01").json()["total"] == 2
    assert seeded.get("/sightings?end_date=9999-12-31").json()["total"] == 5
    assert seeded.get("/sightings?location=Lon").json()["total"] == 0


def test_pagination_is_stable(seeded):
    pages = [seeded.get(f"/sightings?page={page}&page_size=2").json() for page in range(1, 5)]
    assert [[row["id"] for row in page["items"]] for page in pages] == [["a", "b"], ["c", "d"], ["e"], []]
    assert all(page["total"] == 5 for page in pages)


@pytest.mark.parametrize("query", ["start_date=no", "end_date=2024-02-30", "start_date=2025-01-02&end_date=2025-01-01", "page=0", "page_size=201", "page_size=-1", "page=abc", "location=", "location=%20%20", "typo=London"])
def test_bad_search_query(client, query):
    assert client.get(f"/sightings?{query}").status_code == 422
