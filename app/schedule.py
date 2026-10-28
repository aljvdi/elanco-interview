"""One refresh loop per application process, managed by FastAPI lifespan."""
import asyncio
from collections.abc import Callable
import logging

from peewee import PeeweeException
from starlette.concurrency import run_in_threadpool

from app.upstream import UpstreamFailure

logger = logging.getLogger(__name__)
REFRESH_INTERVAL_SECONDS = 60 * 60


async def refresh_periodically(
    refresh: Callable, stop: asyncio.Event, interval: float = REFRESH_INTERVAL_SECONDS,
):
    # Wait after each completed attempt, so slow refreshes never overlap or pile up.
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            if stop.is_set():
                break
            try:
                await run_in_threadpool(refresh)
            except (UpstreamFailure, PeeweeException) as exc:
                logger.warning("Scheduled snapshot refresh failed: %s", exc)
