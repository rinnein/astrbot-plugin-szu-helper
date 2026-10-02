from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest

from szu_electricity.catalog import empty_catalog
from szu_electricity.models import (
    CACHE_TTL_SECONDS,
    SHANGHAI,
    ElectricityError,
    Option,
    ProviderResult,
    Reading,
)
from szu_electricity.service import ElectricityService


class Source:
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
        at = datetime.combine(window.end, time(12), tzinfo=SHANGHAI)
        if self.expired:
            at -= timedelta(days=3)
        return ProviderResult([Reading(at, remaining=self.balance, daily=2)], self.balance, at)

    async def catalog(self):
        self.catalog_calls += 1
        c = empty_catalog()
        c.areas[0].buildings = [Option("54", self.name)]
        return c


async def test_query_cache_expires_at_two_hours_without_sliding(store, location):
    source = Source()
    now = [1000.0]
    service = ElectricityService(source, store, clock=lambda: now[0])
    first = await service.query(location)
    source.balance = 20
    now[0] += CACHE_TTL_SECONDS - 1
    assert await service.query(location) == first
    assert len(source.calls) == 1
    now[0] += 1
    assert (await service.query(location)).remaining == 20
    assert len(source.calls) == 2
    await service.close()


async def test_query_cache_survives_store_and_service_reload(store, location):
    source = Source()
    service = ElectricityService(source, store, clock=lambda: 1000)
    first = await service.query(location)
    await service.close()
    await store.close()
    await store.open()
    new_source = Source()
    new_source.failure = True
    reloaded = ElectricityService(new_source, store, clock=lambda: 2000)
    assert await reloaded.query(location) == first
    assert new_source.calls == []
    await reloaded.close()


async def test_query_cache_isolates_sources_rooms_and_windows(store, location):
    official, iotun = Source(), Source("iotun")
    iotun.balance = 99
    selected = [official]
    service = ElectricityService(official, store, provider_selector=lambda: selected[0])
    assert (await service.query(location)).remaining == 10
    selected[0] = iotun
    assert (await service.query(location)).remaining == 99
    await service.query(location, detail=True)
    await service.query(replace(location, roomName="0801"))
    assert len(iotun.calls) == 3
    assert [days for _, days in iotun.calls] == [3, 31, 3]
    selected[0] = official
    assert (await service.query(location)).source == "official"
    assert len(official.calls) == 1
    await service.close()


@pytest.mark.parametrize("failure", ["exception", "expired", "missing"])
async def test_failed_or_unusable_results_are_not_cached(store, location, failure):
    source = Source()
    service = ElectricityService(source, store)
    if failure == "exception":
        source.failure = True
        with pytest.raises(ElectricityError):
            await service.query(location)
        source.failure = False
    elif failure == "expired":
        source.expired = True
        assert (await service.query(location)).expired
        source.expired = False
    else:
        source.balance = None
        assert (await service.query(location)).remaining is None
        source.balance = 10
    assert await store._all("SELECT * FROM query_cache") == []
    assert (await service.query(location)).remaining == 10
    assert len(source.calls) == 2
    await service.close()


async def test_catalog_cache_uses_two_hours_and_source_isolation(store):
    official, iotun = Source(), Source("iotun")
    now, selected = [1000.0], [official]
    service = ElectricityService(
        official, store, provider_selector=lambda: selected[0], clock=lambda: now[0]
    )
    assert (await service.catalog()).areas[0].buildings[0].name == "official"
    selected[0] = iotun
    assert (await service.catalog()).areas[0].buildings[0].name == "iotun"
    now[0] += CACHE_TTL_SECONDS - 1
    await service.catalog()
    assert iotun.catalog_calls == 1
    now[0] += 1
    await service.catalog()
    assert iotun.catalog_calls == 2
    await service.close()
