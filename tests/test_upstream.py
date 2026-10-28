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
