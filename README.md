# Elanco Tick Sightings API

A Python backend for searching UK tick sightings and reporting activity by location,
week and month. It uses FastAPI for the API, HTTPX to retrieve Elanco's data, and
Peewee with SQLite to store it.

## How it works

On startup, the app checks Elanco's health endpoint. If the service is healthy, it
downloads the sightings and saves a fresh copy in SQLite. It repeats this process
once an hour while running.

Each record is checked before saving. Records need an ID, date and location;
missing species names are allowed. Invalid records are skipped, and duplicate IDs
are saved once. The database is replaced in one transaction so a failed refresh
keeps the previous data available.

Searches and reports normally use the saved data. If a search or report has no
matching records, the app tries refreshing from Elanco again. A sighting missing
from an ID lookup is requested directly from Elanco.

This approach keeps normal requests fast and reduces calls to the source API.
SQLite keeps setup simple, and filtering and report calculations happen in the
database. The hourly timer runs inside the app, so use a single server worker.

## Run the app

Use Python 3.11 or later. From the project folder in PowerShell:

```powershell
# create the virtual env
python -m venv .venv

# start it
source .venv/bin/activate

# install requirements
pip install -r requirements.txt

# run the app
python -m uvicorn app.main:app --reload
```

Open [Swagger UI](http://127.0.0.1:8000/docs) to try the endpoints. The API schema is
available at [openapi.json](http://127.0.0.1:8000/openapi.json).

Data is downloaded automatically and stored in `sightings.db` in the project folder.
Set the `SIGHTINGS_DB` environment variable to use a different database path.

## Endpoints

| GET endpoint | What it does |
| --- | --- |
| `/health` | Checks the app's database connection |
| `/sightings` | Searches sightings with pagination |
| `/sightings/{sighting_id}` | Returns one sighting by ID |
| `/reports/regions` | Counts sightings by location |
| `/reports/trends` | Counts sightings by week or month |

Search and reports accept `start_date`, `end_date` and `location`. Dates use
`YYYY-MM-DD`, and both date boundaries are included. Location matching ignores
capitalisation but requires the full location name.

Search also accepts `page` and `page_size`. These default to 1 and 50, with a maximum
page size of 200. For example:

```text
/sightings?location=London&start_date=2024-01-01&end_date=2024-12-31&page=1&page_size=20
```

For trends, use `period=weekly` or `period=monthly`. Weeks start on Monday. Only
periods containing sightings are returned.

```text
/reports/trends?period=monthly&location=London
```

The source provides city names rather than regions, so the region report groups
by the supplied location field.

## Run the tests

```powershell
pytest -q
```

Tests cover data validation, duplicate handling, refresh failures, search filters,
pagination, reports and the hourly timer. They use temporary databases and mocked
API responses, so they do not call Elanco's server.
