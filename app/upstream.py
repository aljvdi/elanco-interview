"""Health-checked downloads and atomic snapshots of Elanco sightings."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

BASE_URL = "https://dev-task.elancoapps.com/data"


class UpstreamFailure(Exception):
    def __init__(self, message: str, status_code: int = 502, retry_after: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class SourceSighting(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    id: str
    date: datetime
    location: str
    species: str | None = None
    latinName: str | None = None

    @field_validator("id", "location")
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("date", mode="before")
    @classmethod
    def iso_timestamp(cls, value):
        if not isinstance(value, str) or not re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value
        ):
            raise ValueError("expected an ISO 8601 timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            try:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            except OverflowError as exc:
                raise ValueError("timestamp outside supported range") from exc
        return parsed

    @field_validator("species", "latinName")
    @classmethod
    def empty_optional(cls, value):
        return value or None


@dataclass
class SnapshotResult:
    received: int = 0
    stored: int = 0
    duplicates: int = 0
    invalid: int = 0


def get_json(client: httpx.Client, path: str, *, allow_not_found: bool = False):
    try:
        response = client.get(f"{BASE_URL}{path}", timeout=20)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = 404 if status == 404 and allow_not_found else 503 if status == 429 else 502
        message = "Sighting not found" if code == 404 else f"Upstream HTTP {status}"
        raise UpstreamFailure(message, code, exc.response.headers.get("Retry-After")) from exc
    except httpx.TimeoutException as exc:
        raise UpstreamFailure("Upstream request timed out", 504) from exc
    except httpx.RequestError as exc:
        raise UpstreamFailure("Upstream request failed") from exc
    try:
        return response.json()
    except ValueError as exc:
        raise UpstreamFailure("Upstream response is not valid JSON") from exc


def fetch_records(client: httpx.Client) -> list:
    health = get_json(client, "/health")
    if not isinstance(health, dict) or health.get("status") != "healthy":
        raise UpstreamFailure("Upstream health check failed", 503)
    records = get_json(client, "/tick-sightings")
    if not isinstance(records, list):
        raise UpstreamFailure("Expected a JSON array of sightings")
    return records


def replace_snapshot(records: list, db, sighting) -> SnapshotResult:
    if not isinstance(records, list):
        raise UpstreamFailure("Expected a JSON array of sightings")
    result = SnapshotResult(received=len(records))
    rows = {}
    for raw in records:
        try:
            row = SourceSighting.model_validate(raw)
        except ValidationError:
            result.invalid += 1
            continue
        if row.id in rows:
            result.duplicates += 1
        else:
            rows[row.id] = row
    if records and not rows:
        raise UpstreamFailure("Upstream snapshot contains no valid sightings")
    with db.connection_context(), db.atomic():
        sighting.delete().execute()
        for row in rows.values():
            sighting.create(
                id=row.id, date=row.date, location=row.location,
                species=row.species, latin_name=row.latinName,
            )
    result.stored = len(rows)
    return result


def refresh_snapshot(client: httpx.Client, db, sighting) -> SnapshotResult:
    return replace_snapshot(fetch_records(client), db, sighting)


def fetch_sighting(client: httpx.Client, sighting_id: str) -> dict:
    payload = get_json(
        client, f"/tick-sightings/{quote(sighting_id, safe='')}", allow_not_found=True
    )
    try:
        row = SourceSighting.model_validate(payload)
    except ValidationError as exc:
        raise UpstreamFailure("Upstream sighting is invalid") from exc
    if row.id != sighting_id:
        raise UpstreamFailure("Upstream returned a different sighting")
    return row.model_dump()
