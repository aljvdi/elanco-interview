"""Explicit local-file or single-request upstream import; never runs on API reads."""
import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys

import httpx
from peewee import PeeweeException
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.database import create_store, initialize

UPSTREAM_URL = "https://dev-task.elancoapps.com/data/tick-sightings"


class ImportFailure(Exception):
    """An import could not read its source; no records have been written."""


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
        if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
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
class ImportResult:
    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    invalid: int = 0
    errors: list[dict] = field(default_factory=list)


def require_array(payload):
    if not isinstance(payload, list):
        raise ImportFailure("Expected a JSON array of sightings")
    return payload


def fetch_records(client: httpx.Client) -> list:
    try:
        response = client.get(UPSTREAM_URL, timeout=20)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        retry_after = exc.response.headers.get("Retry-After")
        detail = f"; Retry-After: {retry_after}" if retry_after else ""
        raise ImportFailure(f"Upstream HTTP {exc.response.status_code}{detail}; retry manually later") from exc
    except httpx.RequestError as exc:
        raise ImportFailure(f"Upstream request failed ({type(exc).__name__}); retry manually later") from exc
    try:
        return require_array(response.json())
    except ValueError as exc:
        raise ImportFailure("Upstream response is not valid JSON") from exc


def load_records(path: Path) -> list:
    try:
        return require_array(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ImportFailure(f"Cannot read JSON input ({type(exc).__name__})") from exc


def import_records(payload: list, db, sighting) -> ImportResult:
    records = require_array(payload)
    result = ImportResult(received=len(records))
    with db.connection_context(), db.atomic():
        for index, raw in enumerate(records):
            try:
                row = SourceSighting.model_validate(raw)
            except ValidationError as exc:
                result.invalid += 1
                if len(result.errors) < 10:
                    result.errors.append({"index": index, "fields": [".".join(map(str, e["loc"])) or "record" for e in exc.errors()]})
                continue
            added = (sighting.insert(
                id=row.id, date=row.date, location=row.location,
                species=row.species, latin_name=row.latinName,
            ).on_conflict(action="NOTHING", conflict_target=[sighting.id]).as_rowcount().execute())
            result.inserted += added
            result.duplicates += 1 - added
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Import a local JSON array")
    source.add_argument("--api", action="store_true", help="Fetch the documented upstream endpoint once")
    parser.add_argument("--db", help="SQLite path (default: SIGHTINGS_DB or sightings.db)")
    args = parser.parse_args(argv)
    try:
        if args.file:
            records = load_records(args.file)
        else:
            with httpx.Client() as client:
                records = fetch_records(client)
        db, sighting = create_store(args.db)
        initialize(db, sighting)
        result = import_records(records, db, sighting)
    except (ImportFailure, PeeweeException, sqlite3.Error) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

