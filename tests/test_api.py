import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def create_test_app(path):
    def handler(request):
        payload = {"status": "healthy"} if request.url.path.endswith("/health") else []
        return httpx.Response(200, json=payload)
    return create_app(path, upstream_transport=httpx.MockTransport(handler))


@pytest.fixture
def client(tmp_path):
    app = create_test_app(str(tmp_path / "test.sqlite"))
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.fixture
def seeded(client):
    from app.upstream import replace_snapshot
    rows = [
        {"id": "b", "date": "2024-12-30T00:00:00", "location": "London", "species": "Example tick", "latinName": "Example species"},
        {"id": "a", "date": "2024-12-30T00:00:00", "location": "London"},
        {"id": "c", "date": "2024-12-31T23:59:59.999999", "location": "Bristol"},
        {"id": "d", "date": "2025-01-01T00:00:00", "location": "London"},
        {"id": "e", "date": "2025-01-06T12:00:00", "location": "Bristol"},
    ]
    replace_snapshot(rows, client.app.state.db, client.app.state.sighting)
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
    from app.upstream import replace_snapshot
    replace_snapshot([{"id": "sunday", "date": "2025-01-05T23:59:59", "location": "London"}], seeded.app.state.db, seeded.app.state.sighting)
    assert seeded.get("/reports/trends").json()["items"][0] == {"period_start": "2024-12-30", "count": 1}


@pytest.mark.parametrize("path", ["/reports/regions", "/reports/trends", "/reports/trends?period=monthly"])
def test_empty_reports(client, path):
    assert client.get(path).status_code == 200
    assert client.get(path).json()["items"] == []


@pytest.mark.parametrize("path", ["/reports/trends?period=daily", "/reports/trends?start_date=bad", "/reports/regions?location=%20", "/reports/regions?start_date=2025-01-02&end_date=2025-01-01"])
def test_bad_report_query(client, path):
    assert client.get(path).status_code == 422


@pytest.mark.parametrize("path", ["/health", "/sightings", "/reports/regions", "/reports/trends"])
def test_database_unavailable_returns_503(client, monkeypatch, path):
    from peewee import OperationalError
    def unavailable(*args, **kwargs):
        raise OperationalError("private database path must not leak")
    monkeypatch.setattr(client.app.state.db, "execute_sql", unavailable)
    response = client.get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable; try again later"}


def test_location_is_treated_as_data(seeded):
    assert seeded.get("/sightings", params={"location": "London' OR 1=1 --"}).json()["total"] == 0
    assert seeded.get("/sightings").json()["total"] == 5


def test_app_databases_are_isolated(seeded, tmp_path):
    with TestClient(create_test_app(str(tmp_path / "other.sqlite"))) as other:
        assert other.get("/sightings").json()["total"] == 0
        assert seeded.get("/sightings").json()["total"] == 5


def test_documented_routes_in_openapi(client):
    schema = client.get("/openapi.json").json()
    assert {"/health", "/sightings", "/reports/regions", "/reports/trends"} <= schema["paths"].keys()



def test_parallel_reads_use_independent_connections(seeded):
    from concurrent.futures import ThreadPoolExecutor
    paths = ["/sightings", "/reports/regions", "/reports/trends", "/health"] * 4
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(seeded.get, paths))
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["total"] == 5 for response in responses[::4])


def test_missing_database_is_created_on_startup(tmp_path):
    path = tmp_path / "fresh.sqlite"
    assert not path.exists()
    with TestClient(create_test_app(str(path))) as client:
        assert path.exists()
        assert client.get("/health").status_code == 200
        assert client.get("/sightings").json()["items"] == []


def test_swagger_frontend(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger-ui" in docs.text
    assert "/openapi.json" in docs.text
    assert '"tryItOutEnabled": true' in docs.text


def test_exported_openapi_matches_runtime(client):
    import json
    from pathlib import Path
    schema = json.loads((Path(__file__).parents[1] / "openapi.json").read_text())
    assert schema == client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Elanco tick sightings"
    assert schema["paths"]["/sightings"]["get"]["tags"] == ["Sightings"]
