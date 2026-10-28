"""Tick sightings API backed by a health-checked upstream snapshot."""
import asyncio
from contextlib import asynccontextmanager
import logging
from threading import Lock
from typing import Annotated

import httpx

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, RedirectResponse
from peewee import OperationalError, fn
from starlette.concurrency import run_in_threadpool

from app.database import create_store, initialize
from app.queries import (
    Filters, RegionReport, SearchFilters, SearchResponse, SightingResponse,
    TrendFilters, TrendReport, filtered, serialize,
)

from app.schedule import REFRESH_INTERVAL_SECONDS, refresh_periodically
from app.upstream import UpstreamFailure, fetch_sighting, refresh_snapshot

logger = logging.getLogger(__name__)


def create_app(
    db_path: str | None = None,
    *, upstream_transport: httpx.BaseTransport | None = None,
    refresh_interval: float = REFRESH_INTERVAL_SECONDS,
) -> FastAPI:
    if refresh_interval <= 0:
        raise ValueError("refresh_interval must be positive")
    db, sighting = create_store(db_path)
    refresh_lock = Lock()

    def refresh():
        # Startup, scheduled and request-triggered snapshots share the same lock.
        with refresh_lock:
            app.state.snapshot = refresh_snapshot(app.state.upstream, db, sighting)

    @asynccontextmanager
    async def lifespan(app):
        initialize(db, sighting)
        with httpx.Client(transport=upstream_transport) as upstream:
            app.state.upstream = upstream
            try:
                await run_in_threadpool(refresh)
            except UpstreamFailure as exc:
                logger.warning("Startup snapshot refresh failed: %s", exc)
            stop = asyncio.Event()
            task = asyncio.create_task(refresh_periodically(refresh, stop, refresh_interval))
            app.state.refresh_task = task
            try:
                yield
            finally:
                stop.set()
                # Finish any in-flight refresh before closing its HTTP client.
                await task
                if not db.is_closed():
                    db.close()

    app = FastAPI(
        title="Elanco tick sightings",
        version="1.0.0",
        description="Search tick sightings and explore counts by location and time.",
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
        swagger_ui_parameters={"displayRequestDuration": True, "tryItOutEnabled": True},
        openapi_tags=[
            {"name": "Health", "description": "Service availability"},
            {"name": "Sightings", "description": "Paginated sighting search"},
            {"name": "Reports", "description": "Location and time aggregates"},
        ],
        responses={
            502: {"description": "Upstream request or response failed"},
            503: {"description": "Database or upstream unavailable"},
            504: {"description": "Upstream request timed out"},
        },
    )
    app.state.db = db
    app.state.sighting = sighting

    @app.exception_handler(OperationalError)
    async def database_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable; try again later"})

    @app.exception_handler(UpstreamFailure)
    async def upstream_unavailable(request, exc):
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)}, headers=headers)

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse(url="/docs")

    @app.get("/health", tags=["Health"], summary="Check service health")
    def health():
        with db.connection_context():
            db.execute_sql("SELECT 1")
        return {"status": "ok"}

    def ensure_matches(filters: Filters):
        with db.connection_context():
            found = filtered(sighting.select(), sighting, filters).exists()
        if not found:
            refresh()

    @app.get("/sightings", response_model=SearchResponse, tags=["Sightings"], summary="Search sightings")
    def search(filters: Annotated[SearchFilters, Query()]):
        ensure_matches(filters)
        with db.connection_context(), db.atomic():
            query = filtered(sighting.select(), sighting, filters)
            total = query.count()
            rows = query.order_by(sighting.date, sighting.id).paginate(filters.page, filters.page_size)
            return {"items": [serialize(row) for row in rows], "total": total,
                    "page": filters.page, "page_size": filters.page_size}

    @app.get(
        "/sightings/{sighting_id}", response_model=SightingResponse,
        tags=["Sightings"], summary="Get a sighting by ID",
        responses={404: {"description": "Sighting not found"}},
    )
    def detail(sighting_id: str):
        with db.connection_context():
            row = sighting.get_or_none(sighting.id == sighting_id)
            if row is not None:
                return serialize(row)
        return fetch_sighting(app.state.upstream, sighting_id)

    @app.get("/reports/regions", response_model=RegionReport, tags=["Reports"], summary="Count sightings by location")
    def regions(filters: Annotated[Filters, Query()]):
        ensure_matches(filters)
        with db.connection_context():
            query = filtered(sighting.select(sighting.location.alias("region"), fn.COUNT(sighting.id).alias("count")), sighting, filters)
            rows = query.group_by(sighting.location).order_by(sighting.location).dicts()
            return {"grouped_by": "location", "items": list(rows)}

    @app.get("/reports/trends", response_model=TrendReport, tags=["Reports"], summary="Count sightings by week or month")
    def trends(filters: Annotated[TrendFilters, Query()]):
        ensure_matches(filters)
        # Stored timestamps are normalized before saving. Group by their calendar
        # date so SQLite cannot round fractional seconds into the following day.
        day = fn.substr(sighting.date, 1, 10)
        if filters.period == "weekly":
            bucket = fn.date(day, "-6 days", "weekday 1")
        else:
            bucket = fn.strftime("%Y-%m-01", day)
        with db.connection_context():
            query = filtered(sighting.select(bucket.alias("period_start"), fn.COUNT(sighting.id).alias("count")), sighting, filters)
            rows = query.group_by(bucket).order_by(bucket).dicts()
            return {"period": filters.period, "items": list(rows)}

    return app


app = create_app()
