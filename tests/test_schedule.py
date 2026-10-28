import asyncio
from threading import Event
import time

from fastapi.testclient import TestClient
import httpx
from peewee import OperationalError

from app.main import create_app
from app.schedule import REFRESH_INTERVAL_SECONDS, refresh_periodically
from app.upstream import UpstreamFailure


def test_default_interval_and_shutdown_without_an_extra_download(tmp_path):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        payload = {"status": "healthy"} if request.url.path.endswith("/health") else []
        return httpx.Response(200, json=payload)
    app = create_app(str(tmp_path / "timer.sqlite"), upstream_transport=httpx.MockTransport(handler))
    assert REFRESH_INTERVAL_SECONDS == 3600
    with TestClient(app):
        task = app.state.refresh_task
        assert not task.done()
    assert task.done() and not task.cancelled()
    assert task.exception() is None
    assert app.state.upstream.is_closed
    assert requests == ["/data/health", "/data/tick-sightings"]


def test_scheduled_errors_do_not_stop_future_attempts(caplog):
    async def scenario():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        attempts = []
        def refresh():
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise UpstreamFailure("simulated upstream outage")
            if len(attempts) == 2:
                raise OperationalError("simulated database lock")
            loop.call_soon_threadsafe(stop.set)
        await asyncio.wait_for(refresh_periodically(refresh, stop, interval=0.001), timeout=2)
        assert attempts == [1, 2, 3]
    asyncio.run(scenario())
    assert caplog.text.count("Scheduled snapshot refresh failed") == 2


def test_shutdown_waits_for_inflight_refresh():
    async def scenario():
        stop = asyncio.Event()
        started = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()
        completed = []
        def refresh():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2)
            completed.append(True)
        task = asyncio.create_task(refresh_periodically(refresh, stop, interval=0.001))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            stop.set()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=2)
        assert completed == [True]
    asyncio.run(scenario())


def test_hourly_refresh_replaces_data_and_recovers_from_failure(tmp_path):
    failed = Event()
    recover = Event()
    health_calls = 0
    downloads = 0
    def handler(request):
        nonlocal health_calls, downloads
        if request.url.path.endswith("/health"):
            health_calls += 1
            if health_calls > 1 and not recover.is_set():
                failed.set()
                return httpx.Response(503)
            return httpx.Response(200, json={"status": "healthy"})
        downloads += 1
        return httpx.Response(200, json=[{
            "id": "example", "date": "2024-12-30T12:00:00",
            "location": "London" if downloads == 1 else "Bristol",
        }])
    app = create_app(
        str(tmp_path / "refresh.sqlite"),
        upstream_transport=httpx.MockTransport(handler), refresh_interval=0.02,
    )
    with TestClient(app) as client:
        assert failed.wait(2)
        assert client.get("/sightings").json()["items"][0]["location"] == "London"
        assert downloads == 1
        recover.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if client.get("/sightings").json()["items"][0]["location"] == "Bristol":
                break
            time.sleep(0.005)
        else:
            raise AssertionError("Scheduled refresh did not replace the snapshot")
        assert downloads >= 2
    assert app.state.refresh_task.done()
