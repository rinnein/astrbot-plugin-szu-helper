from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest

from szu_electricity.catalog import empty_catalog
from szu_electricity.models import (
    SHANGHAI,
    ElectricityError,
    Option,
    ProviderResult,
    Reading,
)
from szu_electricity.service import ElectricityService


class Source:
    """Fake provider: one daily=2 row per requested day plus a live balance."""

    def __init__(self, name="official"):
        self.name = name
        self.balance = 10
        self.calls = []
        self.catalog_calls = 0
        self.failure = False
        self.expired = False

    async def query(self, location, window):
        self.calls.append((location.key, window.days))
        if self.failure:
            raise ElectricityError("temporary failure")
        shift = timedelta(days=3) if self.expired else timedelta()
        rows = [
            Reading(
                datetime.combine(day, time.min, tzinfo=SHANGHAI) - shift,
                remaining=self.balance,
                daily=2,
            )
            for i in range(window.days)
            if (day := window.begin + timedelta(days=i)) or True
        ]
        at = datetime.combine(window.end, time.min, tzinfo=SHANGHAI) - shift
        return ProviderResult(rows, self.balance, at)

    async def catalog(self):
        self.catalog_calls += 1
        c = empty_catalog()
        c.areas[0].buildings = [Option("54", self.name)]
        return c


def patched_time(monkeypatch, clock: list[float]) -> None:
    """Tie service-side calendar days to the fake clock."""
    from szu_electricity import service as service_module

    monkeypatch.setattr(
        service_module,
        "local_now",
        lambda: datetime.fromtimestamp(clock[0], SHANGHAI),
    )


async def test_repeat_reads_within_a_day_make_no_requests(monkeypatch, store, location):
    clock = [1000.0]
    patched_time(monkeypatch, clock)
    source = Source()
    service = ElectricityService(source, store, clock=lambda: clock[0])
    first = await service.query(location)
    assert len(source.calls) == 1
    clock[0] += 3600
    assert await service.query(location) == first
    assert len(source.calls) == 1
    await service.close()


async def test_publication_window_retries_then_next_day_refreshes(monkeypatch, store, location):
    local = datetime.now(SHANGHAI).replace(hour=23, minute=58, second=0, microsecond=0)
    clock = [local.timestamp()]
    patched_time(monkeypatch, clock)
    source = Source()
    service = ElectricityService(source, store, clock=lambda: clock[0])
    first = await service.query(location)
    assert len(source.calls) == 1
    # Just after 23:59 the day is no longer fresh; the retry mark expires in
    # five minutes so an empty publication does not poison the whole night.
    clock[0] += 70
    await service.query(location)
    assert len(source.calls) == 2
    clock[0] += 20
    await service.query(location)
    assert len(source.calls) == 2
    # At 00:05 the fresh mark has run out and the new day is needed.
    clock[0] += 6 * 60
    latest = await service.query(location)
    assert len(source.calls) == 3
    assert latest.observed_at.date() == datetime.fromtimestamp(clock[0], SHANGHAI).date()
    assert latest.observed_at.date() > first.observed_at.date()
    await service.close()


async def test_reloaded_store_serves_cached_history_without_requests(monkeypatch, store, location):
    clock = [1000.0]
    patched_time(monkeypatch, clock)
    source = Source()
    service = ElectricityService(source, store, clock=lambda: clock[0])
    first = await service.query(location)
    await service.close()
    await store.close()
    await store.open()
    clock = [2000.0]
    patched_time(monkeypatch, clock)
    new_source = Source()
    new_source.failure = True
    reloaded = ElectricityService(new_source, store, clock=lambda: clock[0])
    assert await reloaded.query(location) == first
    assert new_source.calls == []
    await reloaded.close()


async def test_sources_rooms_and_detail_windows_are_isolated(store, location):
    official, iotun = Source(), Source("iotun")
    iotun.balance = 99
    selected = [official]
    service = ElectricityService(official, store, provider_selector=lambda: selected[0])
    assert (await service.query(location)).remaining == 10
    selected[0] = iotun
    assert (await service.query(location)).remaining == 99
    await service.query(location, detail=True)
    await service.query(replace(location, roomName="0801"))
    assert len(iotun.calls) == 4
    # The detail window reuses the already-fresh last three days: 31 − 3
    # splits into a 20-day and an 8-day chunk.
    assert [days for _, days in iotun.calls] == [3, 20, 8, 3]
    selected[0] = official
    assert (await service.query(location)).source == "official"
    assert len(official.calls) == 1
    await service.close()


async def test_later_failures_serve_cached_history_with_backoff(monkeypatch, store, location):
    clock = [1000.0]
    patched_time(monkeypatch, clock)
    source = Source()
    service = ElectricityService(source, store, clock=lambda: clock[0])
    await service.query(location)
    # Push past the fresh mark so a fetch is actually attempted, then fail.
    clock[0] = _after_publication(clock[0])
    source.failure = True
    cached = await service.query(location)
    assert cached.refresh_error == "temporary failure"
    assert cached.remaining == 10 and len(source.calls) == 2
    # Inside the one-minute backoff even a recovered source is not contacted.
    source.failure = False
    clock[0] += 1
    assert (await service.query(location)).remaining == 10
    assert len(source.calls) == 2
    clock[0] += 60
    assert (await service.query(location)).remaining == 10
    assert len(source.calls) == 3
    recovered = await service.query(location)
    assert recovered.refresh_error is None
    await service.close()


async def test_failed_first_query_raises_and_persists_nothing(store, location):
    source = Source()
    source.failure = True
    service = ElectricityService(source, store, clock=lambda: 1000)
    with pytest.raises(ElectricityError):
        await service.query(location)
    with pytest.raises(ElectricityError):
        await service.query(location)
    assert len(source.calls) == 1
    # No usage rows, but the backoff metadata persists so restarts honor it.
    assert await store._all("SELECT * FROM readings") == []
    rooms = await store._all("SELECT * FROM rooms")
    assert rooms and rooms[0]["error"] == "temporary failure" and rooms[0]["retry_after"] > 1000
    await service.close()


async def test_missing_balance_rows_are_still_recorded(monkeypatch, store, location):
    clock = [1000.0]
    patched_time(monkeypatch, clock)
    source = Source()
    source.balance = None
    service = ElectricityService(source, store, clock=lambda: clock[0])
    report = await service.query(location)
    assert report.remaining is None and report.unavailable_reason == "missing_balance"
    # Usage rows exist even without a balance; predictions still work.
    assert report.prediction_average == 2.0
    # A cached view without a balance estimate refetches the recent window
    # once (probe semantics) as soon as the source has one again.
    source.balance = 10
    recovered = await service.query(location)
    assert recovered.remaining == 10 and len(source.calls) == 2
    await service.close()


async def test_cached_readings_do_not_renew_their_observation_time(monkeypatch, store, location):
    clock = [1000.0]
    patched_time(monkeypatch, clock)
    source = Source()
    service = ElectricityService(source, store, clock=lambda: clock[0])
    first = await service.query(location)
    clock[0] += 60
    cached = await service.query(location)
    assert cached.observed_at == first.observed_at and len(source.calls) == 1
    # Push into the next day so the fetch actually reruns and the portal's
    # newer snapshot replaces the stored one.
    clock[0] = _after_publication(clock[0]) + 3600
    refreshed = await service.query(location)
    assert len(source.calls) == 2 and refreshed.observed_at > first.observed_at
    await service.close()


def _after_publication(epoch: float) -> float:
    """Advance to the first instant a stored day is no longer fresh."""
    local = datetime.fromtimestamp(epoch, SHANGHAI)
    boundary = local.replace(hour=23, minute=59, second=0, microsecond=0)
    return boundary.timestamp() + 10
