"""SQLite storage; each application owns its database and model."""
import os

from peewee import CharField, DateTimeField, Model, SqliteDatabase


def create_store(path: str | None = None):
    db = SqliteDatabase(
        path or os.getenv("SIGHTINGS_DB", "sightings.db"),
        pragmas={"journal_mode": "wal", "foreign_keys": 1},
        timeout=5,
    )

    class Sighting(Model):
        id = CharField(primary_key=True)
        date = DateTimeField(index=True)
        location = CharField(index=True, collation="NOCASE")
        species = CharField(null=True)
        latin_name = CharField(null=True, column_name="latinName")

        class Meta:
            database = db
            indexes = ((("location", "date"), False),)

    return db, Sighting


def initialize(db, sighting):
    with db.connection_context():
        db.create_tables([sighting])
