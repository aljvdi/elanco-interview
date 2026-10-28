"""Read-only tick sighting API. Import data explicitly with app.importer."""
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from peewee import OperationalError, fn

from app.database import create_store, initialize
from app.queries import Filters, RegionReport, SearchFilters, SearchResponse, TrendFilters, TrendReport, filtered, serialize


def create_app(db_path: str | None = None) -> FastAPI:
    db, sighting = create_store(db_path)

    @asynccontextmanager
    async def lifespan(app):
        initialize(db, sighting)
        yield
        if not db.is_closed():
            db.close()

    app = FastAPI(title="Tick sightings", lifespan=lifespan)
    app.state.db = db
    app.state.sighting = sighting

    @app.exception_handler(OperationalError)
    async def database_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable; try again later"})

    @app.get("/health")
    def health():
        with db.connection_context():
            db.execute_sql("SELECT 1")
        return {"status": "ok"}

    @app.get("/sightings", response_model=SearchResponse)
    def search(filters: Annotated[SearchFilters, Query()]):
        with db.connection_context(), db.atomic():
            query = filtered(sighting.select(), sighting, filters)
            total = query.count()
            rows = query.order_by(sighting.date, sighting.id).paginate(filters.page, filters.page_size)
            return {"items": [serialize(row) for row in rows], "total": total,
                    "page": filters.page, "page_size": filters.page_size}

    @app.get("/reports/regions", response_model=RegionReport)
    def regions(filters: Annotated[Filters, Query()]):
        with db.connection_context():
            query = filtered(sighting.select(sighting.location.alias("region"), fn.COUNT(sighting.id).alias("count")), sighting, filters)
            rows = query.group_by(sighting.location).order_by(sighting.location).dicts()
            return {"grouped_by": "location", "items": list(rows)}

    @app.get("/reports/trends", response_model=TrendReport)
    def trends(filters: Annotated[TrendFilters, Query()]):
        if filters.period == "weekly":
            # The Monday of the containing week, including across year boundaries.
            bucket = fn.date(sighting.date, "-6 days", "weekday 1")
        else:
            bucket = fn.strftime("%Y-%m-01", sighting.date)
        with db.connection_context():
            query = filtered(sighting.select(bucket.alias("period_start"), fn.COUNT(sighting.id).alias("count")), sighting, filters)
            rows = query.group_by(bucket).order_by(bucket).dicts()
            return {"period": filters.period, "items": list(rows)}

    return app


app = create_app()



