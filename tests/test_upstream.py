from datetime import datetime
import json

import httpx
from peewee import IntegrityError
import pytest
from fastapi.testclient import TestClient

from app.database import create_store, initialize
from app.main import create_app
from app.upstream import UpstreamFailure, fetch_records, refresh_snapshot, replace_snapshot


@pytest.fixture
def store(tmp_path):
    db, sighting = create_store(str(tmp_path / "snapshot.sqlite"))
    initialize(db, sighting)
    return db, sighting


@pytest.fixture
def record():
    return {"id": "test-1", "date": "2024-12-30T12:00:00", "location": "London",
            "species": "Example tick", "latinName": "Example species"}


def test_snapshot_replaces_updates_and_deduplicates(store, record):
    db, sighting = store
    first = replace_snapshot([record, record, dict(record, id="removed")], db, sighting)
    assert (first.received, first.stored, first.duplicates, first.invalid) == (3, 2, 1, 0)
    second = replace_snapshot([dict(record, location="Bristol")], db, sighting)
    assert second.stored == 1
    with db.connection_context():
        assert sighting.select().count() == 1
        assert sighting.get_by_id("test-1").location == "Bristol"
    assert replace_snapshot([record, record], db, sighting).stored == 1


def test_missing_optional_and_invalid_records(store, record):
    valid = {key: value for key, value in record.items() if key not in ("species", "latinName")}
    invalid = [None, [], {}, dict(record, id=" "), dict(record, date="bad"),
               dict(record, date=123), dict(record, location=42)]
    result = replace_snapshot([valid, *invalid], *store)
    assert (result.stored, result.invalid) == (1, len(invalid))
    db, sighting = store
    with db.connection_context():
        assert sighting.get().species is None


@pytest.mark.parametrize("timestamp", ["2024-02-30T12:00:00", "2024-12-30",
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_all_invalid_snapshot_preserves_cache(store, record, timestamp):
    replace_snapshot([record], *store)
    with pytest.raises(UpstreamFailure, match="no valid sightings"):
        replace_snapshot([dict(record, date=timestamp)], *store)
    db, sighting = store
    with db.connection_context():
        assert sighting.select().count() == 1


def test_timezone_and_whitespace(store, record):
    replace_snapshot([dict(record, id=" test-1 ", location=" London ",
                           date="2024-12-30T13:00:00+01:00")], *store)
    db, sighting = store
    with db.connection_context():
        assert sighting.get().date == datetime(2024, 12, 30, 12)
        assert sighting.get().location == "London"


def test_empty_snapshot_clears_removed_records(store, record):
    replace_snapshot([record], *store)
    assert replace_snapshot([], *store).stored == 0
    db, sighting = store
    with db.connection_context():
        assert sighting.select().count() == 0


def test_database_error_rolls_back_replacement(store, record):
    db, sighting = store
    replace_snapshot([record], db, sighting)
    with db.connection_context():
        db.execute_sql("CREATE TRIGGER reject_row BEFORE INSERT ON sighting WHEN NEW.id = 'reject' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
    with pytest.raises(IntegrityError):
        replace_snapshot([dict(record, id="new"), dict(record, id="reject")], db, sighting)
    with db.connection_context():
        assert [row.id for row in sighting.select()] == ["test-1"]


def test_health_then_single_download(record):
    requests = []
    def handler(request):
        requests.append(request)
        payload = {"status": "healthy"} if request.url.path.endswith("/health") else [record]
        return httpx.Response(200, json=payload)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_records(client) == [record]
    assert [str(request.url) for request in requests] == [
        "https://dev-task.elancoapps.com/data/health",
        "https://dev-task.elancoapps.com/data/tick-sightings",
    ]
    assert all(request.extensions["timeout"]["read"] == 20 for request in requests)


@pytest.mark.parametrize("status", [429, 500, 503, 404])
def test_failed_health_stops_download(status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "60"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamFailure, match=f"HTTP {status}") as failure:
            fetch_records(client)
    assert failure.value.retry_after == "60"
    assert len(requests) == 1


@pytest.mark.parametrize("health", [{"status": "unhealthy"}, {}, [], None])
def test_unhealthy_payload_stops_download(health):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=json.dumps(health))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamFailure, match="health check failed"):
            fetch_records(client)
    assert len(requests) == 1


@pytest.mark.parametrize("error,code", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
def test_network_failure(error, code):
    def handler(request):
        raise error("unavailable", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamFailure) as failure:
            fetch_records(client)
    assert failure.value.status_code == code


@pytest.mark.parametrize("content", [b"not-json", b"{}", b"null"])
def test_bad_download_preserves_snapshot(store, record, content):
    replace_snapshot([record], *store)
    def handler(request):
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(200, content=content)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamFailure):
            refresh_snapshot(client, *store)
    db, sighting = store
    with db.connection_context():
        assert sighting.get().id == "test-1"


def test_every_startup_refreshes_and_failed_health_keeps_cache(tmp_path, record):
    path = str(tmp_path / "startup.sqlite")
    requests = []
    payload = [record]
    healthy = True
    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy" if healthy else "unhealthy"})
        return httpx.Response(200, json=payload)
    transport = httpx.MockTransport(handler)
    with TestClient(create_app(path, upstream_transport=transport)) as client:
        assert client.get("/sightings").json()["items"][0]["location"] == "London"
    payload = [dict(record, location="Bristol")]
    with TestClient(create_app(path, upstream_transport=transport)) as client:
        assert client.get("/sightings").json()["items"][0]["location"] == "Bristol"
    healthy = False
    with TestClient(create_app(path, upstream_transport=transport)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/sightings").json()["items"][0]["location"] == "Bristol"
    assert requests == ["/data/health", "/data/tick-sightings"] * 2 + ["/data/health"]


@pytest.mark.parametrize("path", [
    "/sightings?location=London&page_size=1",
    "/reports/regions?location=London",
    "/reports/trends?location=London&period=monthly",
])
def test_missing_data_retries_upstream_with_same_api_shape(tmp_path, record, path):
    requests = []
    records = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"status": "healthy"} if request.url.path.endswith("/health") else records)
    app = create_app(str(tmp_path / "fallback.sqlite"), upstream_transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        records = [record, dict(record, id="other", location="Bristol")]
        response = client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 1
        assert "proxied" not in body and "source" not in body
        if path.startswith("/sightings"):
            assert body["total"] == 1
            assert body["items"][0]["id"] == record["id"]
        else:
            assert body["items"][0]["count"] == 1
        assert len(requests) == 4  # Startup health/data, then fallback health/data.
        assert client.get(path).status_code == 200
        assert len(requests) == 4  # Matching cached reads do not call upstream.


def test_filter_miss_refreshes_existing_snapshot(tmp_path, record):
    records = [dict(record, location="Bristol")]
    def handler(request):
        return httpx.Response(200, json={"status": "healthy"} if request.url.path.endswith("/health") else records)
    with TestClient(create_app(str(tmp_path / "miss.sqlite"), upstream_transport=httpx.MockTransport(handler))) as client:
        records = [record]
        response = client.get("/sightings?location=London")
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["location"] == "London"


@pytest.mark.parametrize("status,code", [(429, 503), (500, 502)])
def test_fallback_error_codes_and_retry_after(tmp_path, status, code):
    def handler(request):
        return httpx.Response(status, headers={"Retry-After": "60"})
    with TestClient(create_app(str(tmp_path / "failure.sqlite"), upstream_transport=httpx.MockTransport(handler))) as client:
        response = client.get("/sightings")
        assert response.status_code == code
        assert response.headers["Retry-After"] == "60"
        assert response.json() == {"detail": f"Upstream HTTP {status}"}
        assert client.get("/docs").status_code == 200


def test_fallback_timeout(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)
    with TestClient(create_app(str(tmp_path / "timeout.sqlite"), upstream_transport=httpx.MockTransport(handler))) as client:
        assert client.get("/sightings").status_code == 504


def test_detail_uses_cache_then_documented_upstream_route(tmp_path, record):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/data/tick-sightings":
            return httpx.Response(200, json=[record])
        return httpx.Response(200, json=dict(record, id="remote"))
    with TestClient(create_app(str(tmp_path / "detail.sqlite"), upstream_transport=httpx.MockTransport(handler))) as client:
        assert client.get("/sightings/test-1").json()["id"] == "test-1"
        assert len(requests) == 2
        body = client.get("/sightings/remote").json()
        assert body["id"] == "remote"
        assert set(body) == {"id", "date", "location", "species", "latinName"}
    assert requests[-1] == "/data/tick-sightings/remote"


@pytest.mark.parametrize("status,payload,expected", [
    (404, {"error": "Not found"}, 404),
    (500, {}, 502),
    (200, {}, 502),
    (200, {"id": "wrong", "date": "2024-01-01T12:00:00", "location": "London"}, 502),
])
def test_detail_missing_or_bad_upstream(tmp_path, status, payload, expected):
    def handler(request):
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/data/tick-sightings":
            return httpx.Response(200, json=[])
        return httpx.Response(status, json=payload)
    with TestClient(create_app(str(tmp_path / "detail-error.sqlite"), upstream_transport=httpx.MockTransport(handler))) as client:
        assert client.get("/sightings/missing").status_code == expected


def test_invalid_query_does_not_call_upstream_again(tmp_path):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(503)
    with TestClient(create_app(str(tmp_path / "invalid.sqlite"), upstream_transport=httpx.MockTransport(handler))) as client:
        assert client.get("/sightings?page=0").status_code == 422
        assert len(requests) == 1


@pytest.mark.parametrize("status", [429, 500])
def test_failed_download_preserves_cache_after_healthy_check(store, record, status):
    replace_snapshot([record], *store)
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamFailure, match=f"HTTP {status}"):
            refresh_snapshot(client, *store)
    assert requests == ["/data/health", "/data/tick-sightings"]
    db, sighting = store
    with db.connection_context():
        assert sighting.get().id == record["id"]


def test_failed_filter_fallback_preserves_other_cached_sightings(tmp_path, record):
    healthy = True
    def handler(request):
        if not healthy:
            return httpx.Response(500)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(200, json=[record])
    with TestClient(create_app(str(tmp_path / "retained.sqlite"), upstream_transport=httpx.MockTransport(handler))) as client:
        healthy = False
        assert client.get("/sightings?location=Bristol").status_code == 502
        assert client.get("/sightings?location=London").json()["total"] == 1
