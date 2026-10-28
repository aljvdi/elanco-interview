"""Read-only tick sighting API. Import data explicitly with app.importer."""
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Query

from app.database import create_store, initialize
from app.queries import SearchFilters, SearchResponse, filtered, serialize


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

    return app


app = create_app()

