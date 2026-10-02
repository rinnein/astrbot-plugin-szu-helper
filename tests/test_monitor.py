import asyncio
import logging
from dataclasses import replace
from datetime import datetime

from szu_electricity.analytics import summarize
from szu_electricity.models import SHANGHAI, ProviderResult, Window
from szu_electricity.monitor import Monitor
from szu_electricity.service import ElectricityService


class Provider:
    name = "official"

    def __init__(self):
        self.calls = 0
        self.remaining = 4
        self.expired = False
        self.before = None

    async def query(self, location, window):
        self.calls += 1
        if self.before:
            await self.before()
        return ProviderResult(
            [],
            self.remaining,
            datetime.now(SHANGHAI).replace(year=2020) if self.expired else datetime.now(SHANGHAI),
        )


async def bind(store, location, user="1", origin="group1"):
    await store.bind("bot", origin, user, "User" + user, True, "aiocqhttp", location)


async def test_coalesce_episodes_restart_new_binding(store, location):
    provider = Provider()
    service = ElectricityService(provider, store)
    await bind(store, location)
    await bind(store, location, "2")
    await bind(store, location, "3", "group2")
    sent = []

    async def send(bindings, report):
        sent.append([b.sender_id for b in bindings])
        return True

    monitor = Monitor(store, service, send, logging.getLogger())
    await monitor.run()
    assert provider.calls == 1 and sent == [["1", "2"], ["3"]]
    await monitor.run()
    assert len(sent) == 2
    path = store.path
    await store.close()
    await store.open()
    await bind(store, location)  # rebinding same location does not clear delivery
    await monitor.run()
    assert len(sent) == 2
    await bind(store, location, "4")
    await monitor.run()
    assert sent[-1] == ["4"]
    provider.remaining = 5
    await monitor.run()
    provider.remaining = 4.99
    await monitor.run()
    assert sent[-2:] == [["1", "2", "4"], ["3"]]
    assert store.path == path


async def test_failure_stale_and_binding_race(store, location):
    provider = Provider()
    service = ElectricityService(provider, store)
    await bind(store, location)
    calls = []

    async def failed(bindings, report):
        calls.append(bindings)
        return False

    monitor = Monitor(store, service, failed, logging.getLogger())
    await monitor.run()
    await monitor.run()
    assert len(calls) == 2  # failed sends were not recorded
    provider.expired = True
    await monitor.run()
    assert len(calls) == 2
    provider.expired = False

    async def unbind():
        await store.unbind("bot", "group1", "1")

    provider.before = unbind
    await monitor.run()
    assert len(calls) == 2


async def test_cross_session_identity_and_older_data(store, location):
    await bind(store, location)
    other = replace(location, roomName="0801")
    await bind(store, other, origin="group2")
    assert len(await store.bindings()) == 2
    report = summarize(
        location, "official", Window.for_days(3), ProviderResult([], 4, datetime.now(SHANGHAI))
    )
    assert await store.observe(report, 5.0) == 1
    older = replace(report, remaining=8, observed_at=report.observed_at.replace(year=2020))
    assert await store.observe(older, 5.0) is None
    assert await store.observe(report, 5.0) == 1


async def test_singleflight_cancellation_and_limit(store, location):
    provider = Provider()
    service = ElectricityService(provider, store)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block():
        entered.set()
        await release.wait()

    provider.before = block
    a = asyncio.create_task(service.query(location))
    b = asyncio.create_task(service.query(location))
    await entered.wait()
    a.cancel()
    release.set()
    await asyncio.gather(a, return_exceptions=True)
    await b
    assert provider.calls == 1
    active = peak = 0

    async def concurrent():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    provider.before = concurrent
    await asyncio.gather(*(service.query(replace(location, roomName=str(n))) for n in range(10)))
    assert peak == 3
    await service.close()


async def test_custom_threshold_boundary_and_changes(store, location):
    provider = Provider()
    service = ElectricityService(provider, store)
    await bind(store, location)
    sent = []

    async def send(bindings, report):
        sent.append(report.remaining)
        return True

    monitor = Monitor(store, service, send, logging.getLogger(), threshold=7.5)
    provider.remaining = 7.5
    await monitor.run()
    assert sent == []
    provider.remaining = 7.49
    await monitor.run()
    await monitor.run()
    assert sent == [7.49]
    # Reloading with a higher threshold must not repeat an ongoing low episode.
    monitor = Monitor(store, service, send, logging.getLogger(), threshold=10)
    await monitor.run()
    assert sent == [7.49]
    # Lowering the threshold makes this fresh reading healthy and rearms alerts.
    monitor = Monitor(store, service, send, logging.getLogger(), threshold=7)
    await monitor.run()
    provider.remaining = 6.99
    await monitor.run()
    assert sent == [7.49, 6.99]
    provider.remaining = 7
    await monitor.run()
    provider.remaining = 6.99
    await monitor.run()
    assert sent == [7.49, 6.99, 6.99]
    await service.close()


async def automatic_records(store):
    return (
        [tuple(row) for row in await store._all("SELECT * FROM dorm_state ORDER BY dorm_key")],
        [
            tuple(row)
            for row in await store._all("SELECT * FROM deliveries ORDER BY binding_id,episode")
        ],
    )


async def test_manual_ignores_and_preserves_automatic_records(store, location):
    provider = Provider()
    service = ElectricityService(provider, store)
    for sender, origin in [("1", "group1"), ("2", "group1"), ("3", "group2")]:
        await bind(store, location, sender, origin)
    sent = []

    async def send(bindings, report):
        sent.append([b.sender_id for b in bindings])
        return True

    monitor = Monitor(store, service, send, logging.getLogger())
    await monitor.run()
    before = await automatic_records(store)
    for _ in range(2):
        prior_calls = provider.calls
        result = await monitor.run_manual()
        assert provider.calls == prior_calls + 1
        assert result.checked_dorms == result.low_dorms == 1
        assert result.sent_messages == 2 and result.send_failures == 0
        assert sent[-2:] == [["1", "2"], ["3"]]
        assert await automatic_records(store) == before
    count = len(sent)
    await monitor.run()
    assert len(sent) == count
    before = await automatic_records(store)
    provider.remaining = 20
    assert (await monitor.run_manual()).low_dorms == 0
    assert await automatic_records(store) == before  # even recovery is not recorded
    await service.close()


async def test_manual_does_not_consume_first_scheduled_warning(store, location):
    provider = Provider()
    service = ElectricityService(provider, store)
    await bind(store, location)
    sent = []

    async def send(bindings, report):
        sent.append(bindings)
        return True

    monitor = Monitor(store, service, send, logging.getLogger())
    assert (await monitor.run_manual()).sent_messages == 1
    assert await automatic_records(store) == ([], [])
    await monitor.run()
    assert len(sent) == 2
    assert await automatic_records(store) != ([], [])
    await service.close()


async def test_manual_failures_stale_and_unbind(store, location):
    provider = Provider()
    service = ElectricityService(provider, store)
    await bind(store, location)
    await bind(store, location, "2", "group2")

    async def send(bindings, report):
        return bindings[0].origin == "group2"

    monitor = Monitor(store, service, send, logging.getLogger())
    result = await monitor.run_manual()
    assert result.sent_messages == result.send_failures == 1
    provider.expired = True
    result = await monitor.run_manual()
    assert result.stale_dorms == 1 and result.sent_messages == 0
    provider.expired = False

    async def fail_query():
        raise RuntimeError("temporary failure")

    provider.before = fail_query
    result = await monitor.run_manual()
    assert result.query_failures == 1 and result.sent_messages == 0

    async def remove_bindings():
        await store.unbind("bot", "group1", "1")
        await store.unbind("bot", "group2", "2")

    provider.before = remove_bindings
    result = await monitor.run_manual()
    assert result.sent_messages == result.send_failures == 0
    assert await automatic_records(store) == ([], [])
    await service.close()


async def test_manual_and_scheduled_runs_are_independent(store, location):
    import pytest

    from szu_electricity.models import ElectricityError

    provider = Provider()
    service = ElectricityService(provider, store)
    await bind(store, location)
    entered, release = asyncio.Event(), asyncio.Event()

    async def block():
        entered.set()
        await release.wait()

    provider.before = block
    sent = []

    async def send(bindings, report):
        sent.append(bindings)
        return True

    monitor = Monitor(store, service, send, logging.getLogger())
    joined = asyncio.Event()
    query_count = 0
    query = service.query

    async def tracked_query(location, detail=False):
        nonlocal query_count
        query_count += 1
        if query_count == 2:
            joined.set()
        return await query(location, detail)

    service.query = tracked_query
    manual = asyncio.create_task(monitor.run_manual())
    await entered.wait()
    with pytest.raises(ElectricityError, match="正在执行"):
        await monitor.run_manual()
    scheduled = asyncio.create_task(monitor.run())
    await asyncio.wait_for(joined.wait(), timeout=1)
    assert monitor._lock.locked()
    release.set()
    await asyncio.gather(manual, scheduled)
    assert provider.calls == 1  # same dorm/window shares in-flight network I/O
    assert len(sent) == 2  # each requested run delivers independently
    assert len((await automatic_records(store))[1]) == 1
    await service.close()
