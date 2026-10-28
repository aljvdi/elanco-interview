"""Validated query parameters shared by search and reports."""
from datetime import date, datetime, time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Filters(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    start_date: date | None = None
    end_date: date | None = None
    location: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class SearchFilters(Filters):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


def filtered(query, sighting, filters: Filters):
    if filters.start_date:
        query = query.where(sighting.date >= datetime.combine(filters.start_date, time.min))
    if filters.end_date:
        query = query.where(sighting.date <= datetime.combine(filters.end_date, time.max))
    if filters.location:
        query = query.where(sighting.location == filters.location)
    return query


class SightingResponse(BaseModel):
    id: str
    date: datetime
    location: str
    species: str | None
    latinName: str | None


class SearchResponse(BaseModel):
    items: list[SightingResponse]
    total: int
    page: int
    page_size: int


def serialize(row) -> dict:
    return {"id": row.id, "date": row.date, "location": row.location,
            "species": row.species, "latinName": row.latin_name}
