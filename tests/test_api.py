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


def test_region_counts(seeded):
    response = seeded.get("/reports/regions")
    assert response.status_code == 200
    assert response.json() == {"grouped_by": "location", "items": [{"region": "Bristol", "count": 2}, {"region": "London", "count": 3}]}
    assert seeded.get("/reports/regions?start_date=2025-01-01&location=london").json()["items"] == [{"region": "London", "count": 1}]


def test_weekly_crosses_year_boundary(seeded):
    assert seeded.get("/reports/trends?period=weekly").json() == {"period": "weekly", "items": [{"period_start": "2024-12-30", "count": 4}, {"period_start": "2025-01-06", "count": 1}]}


def test_monthly_and_report_filters(seeded):
    assert seeded.get("/reports/trends?period=monthly").json() == {"period": "monthly", "items": [{"period_start": "2024-12-01", "count": 3}, {"period_start": "2025-01-01", "count": 2}]}
    assert seeded.get("/reports/trends?period=monthly&location=bristol&end_date=2024-12-31").json()["items"] == [{"period_start": "2024-12-01", "count": 1}]


def test_sunday_belongs_to_preceding_monday(seeded):
    from app.importer import import_records
    import_records([{"id": "sunday", "date": "2025-01-05T23:59:59", "location": "London"}], seeded.app.state.db, seeded.app.state.sighting)
    assert seeded.get("/reports/trends").json()["items"][0] == {"period_start": "2024-12-30", "count": 5}


@pytest.mark.parametrize("path", ["/reports/regions", "/reports/trends", "/reports/trends?period=monthly"])
def test_empty_reports(client, path):
    assert client.get(path).status_code == 200
    assert client.get(path).json()["items"] == []


@pytest.mark.parametrize("path", ["/reports/trends?period=daily", "/reports/trends?start_date=bad", "/reports/regions?location=%20", "/reports/regions?start_date=2025-01-02&end_date=2025-01-01"])
def test_bad_report_query(client, path):
    assert client.get(path).status_code == 422
