"""Read-only tick sighting API. Import data explicitly with app.importer."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import create_store, initialize


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

    return app


app = create_app()
