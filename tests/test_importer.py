from datetime import datetime
import json

import httpx
import pytest

from app.database import create_store, initialize
from app.importer import ImportFailure, fetch_records, import_records, load_records, main


@pytest.fixture
def store(tmp_path):
    db, sighting = create_store(str(tmp_path / "import.sqlite"))
    initialize(db, sighting)
    return db, sighting


@pytest.fixture
def record():
    # Synthetic example with the actual upstream field names; no private source data.
    return {"id": "test-1", "date": "2024-12-30T12:00:00", "location": "London", "species": "Example tick", "latinName": "Example species"}


def test_repeat_import_and_duplicate_ids(store, record):
    db, sighting = store
    first = import_records([record, record], db, sighting)
    assert (first.received, first.inserted, first.duplicates, first.invalid) == (2, 1, 1, 0)
    changed = dict(record, location="Bristol")
    assert import_records([changed], db, sighting).duplicates == 1
    with db.connection_context():
        assert sighting.select().count() == 1
        assert sighting.get_by_id("test-1").location == "London"


def test_bad_records_and_missing_optional_fields(store, record):
    db, sighting = store
    valid = {key: value for key, value in record.items() if key not in ("species", "latinName")}
    invalid = [None, [], {}, dict(record, id=" "), dict(record, date="bad"), dict(record, date=123), dict(record, location=42)]
    result = import_records([valid, *invalid], db, sighting)
    assert (result.inserted, result.invalid) == (1, len(invalid))
    assert len(result.errors) == len(invalid)
    with db.connection_context():
        assert sighting.get().species is None


def test_error_examples_are_bounded(store):
    result = import_records([{}] * 30, *store)
    assert result.invalid == 30
    assert len(result.errors) == 10


def test_timezone_and_whitespace_normalization(store, record):
    import_records([dict(record, id=" test-1 ", location=" London ", date="2024-12-30T13:00:00+01:00")], *store)
    db, sighting = store
    with db.connection_context():
        assert sighting.get().date == datetime(2024, 12, 30, 12)
        assert sighting.get().location == "London"


@pytest.mark.parametrize("status", [429, 500, 503, 404])
def test_upstream_http_errors(status, record):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, headers={"Retry-After": "60"}))) as client:
        with pytest.raises(ImportFailure, match=f"HTTP {status}.*Retry-After: 60"):
            fetch_records(client)


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_upstream_network_failure(error):
    def handler(request):
        raise error("unavailable", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImportFailure, match="request failed"):
            fetch_records(client)


@pytest.mark.parametrize("payload", [{"data": []}, None, "invalid"])
def test_reject_wrong_envelope(payload):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(payload)))) as client:
        with pytest.raises(ImportFailure, match="JSON array"):
            fetch_records(client)


def test_fetch_uses_documented_endpoint_once(record):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[record])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_records(client) == [record]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://dev-task.elancoapps.com/data/tick-sightings"
    assert requests[0].extensions["timeout"]["read"] == 20


def test_invalid_json_response():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))) as client:
        with pytest.raises(ImportFailure, match="not valid JSON"):
            fetch_records(client)


def test_cli_bad_file_does_not_create_database(tmp_path, capsys):
    database = tmp_path / "absent.sqlite"
    assert main(["--file", str(tmp_path / "missing.json"), "--db", str(database)]) == 1
    assert not database.exists()
    assert "Import failed" in capsys.readouterr().err


def test_file_wrong_envelope(tmp_path):
    source = tmp_path / "source.json"
    source.write_text('{}')
    with pytest.raises(ImportFailure, match="JSON array"):
        load_records(source)

