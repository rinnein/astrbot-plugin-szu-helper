import asyncio
import logging
from dataclasses import replace
from datetime import datetime, time, timedelta

from szu_electricity.analytics import summarize
from szu_electricity.models import (
    DEFAULT_LOW_POWER_THRESHOLD,
    SHANGHAI,
    ProviderResult,
    Reading,
    Window,
)
from szu_electricity.monitor import Monitor
from szu_electricity.service import ElectricityService


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=None):
        """Default advance crosses into the next day past the publication
        window, so stored days stop being fresh and a refetch happens."""
        if seconds is None:
            local = datetime.fromtimestamp(self.now, SHANGHAI)
            nxt = (local + timedelta(days=1)).replace(hour=0, minute=11, second=0, microsecond=0)
            self.now = nxt.timestamp()
        else:
            self.now += seconds


def patched_time(monkeypatch, clock) -> None:
    """Tie service-side calendar days to the fake clock."""
    from szu_electricity import service as service_module

    monkeypatch.setattr(
        service_module,
        "local_now",
        lambda: datetime.fromtimestamp(clock(), SHANGHAI),
    )


class Provider:
    name = "official"

    def __init__(self, clock=None):
        self.calls = 0
        self.remaining = 4
        self.expired = False
        self.before = None
        # Portal readings follow the fake clock so the elapsed-usage
        # correction ages them by zero days.
        self.clock = clock

    async def query(self, location, window):
        self.calls += 1
        if self.before:
            await self.before()
        observed = (
            datetime.fromtimestamp(self.clock(), SHANGHAI) if self.clock else datetime.now(SHANGHAI)
        )
        if self.expired:
            observed = observed.replace(year=2020)
        rows = [
            Reading(
                datetime.combine(window.begin + timedelta(days=i), time.min, tzinfo=SHANGHAI),
                daily=2,
            )
            for i in range(window.days)
        ]
        return ProviderResult(rows, self.remaining, observed)


async def bind(store, location, user="1", origin="group1"):
    await store.bind("bot", origin, user, "User" + user, True, "aiocqhttp", location)


async def test_coalesce_episodes_restart_new_binding(monkeypatch, store, location):
    clock = Clock()
    provider = Provider(clock)
    patched_time(monkeypatch, clock)
    service = ElectricityService(provider, store, clock=clock)
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
    clock.advance()
    provider.remaining = 5
    await monitor.run()
    clock.advance()
    provider.remaining = 4.99
    await monitor.run()
    assert sent[-2:] == [["1", "2", "4"], ["3"]]
    assert store.path == path


async def test_failure_stale_and_binding_race(monkeypatch, store, location):
    clock = Clock()
    provider = Provider(clock)
    patched_time(monkeypatch, clock)
    service = ElectricityService(provider, store, clock=clock)
    await bind(store, location)
    calls = []

    async def failed(bindings, report):
        calls.append(bindings)
        return False

    monitor = Monitor(store, service, failed, logging.getLogger())
    await monitor.run()
    await monitor.run()
    assert len(calls) == 2  # failed sends were not recorded

    # A dorm without any balance snapshot cannot judge low power at all;
    # stale readings still warn through the projected effective balance.
    clock.advance()
    await store.unbind("bot", "group1", "1")
    await store.unbind("bot", "group1", "2")
    await bind(store, replace(location, roomName="0901"), "9", "group9")
    provider.remaining = None
    await monitor.run()
    assert len(calls) == 2  # the balance-less dorm was queried and skipped

    async def unbind():
        await store.unbind("bot", "group9", "9")

    provider.before = unbind
    await monitor.run()
    # The binding vanished mid-query; the scan delivers to nobody.
    assert len(calls) == 2

    # A live balance on the original dorm still triggers the race delivery.
    await bind(store, location, "5", "group5")
    provider.remaining = 4

    async def unbind_during_query():
        await store.unbind("bot", "group5", "5")

    provider.before = unbind_during_query
    clock.advance()
    await monitor.run()
    assert len(calls) == 2  # delivery found no pending binding


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
    clock = Clock()
    provider = Provider(clock)
    service = ElectricityService(provider, store, clock=clock)
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
    await asyncio.gather(
        *(service.query(replace(location, roomName=f"{n:03d}")) for n in range(10))
    )
    assert peak == 3
    await service.close()


async def test_custom_threshold_boundary_and_changes(monkeypatch, store, location):
    clock = Clock()
    provider = Provider(clock)
    patched_time(monkeypatch, clock)
    service = ElectricityService(provider, store, clock=clock)
    await bind(store, location)
    sent = []

    async def send(bindings, report):
        sent.append(report.remaining)
        return True

    monitor = Monitor(store, service, send, logging.getLogger(), threshold=7.5)
    clock.advance()
    provider.remaining = 7.5
    await monitor.run()
    assert sent == []
    clock.advance()
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
    clock.advance()
    provider.remaining = 6.99
    await monitor.run()
    assert sent == [7.49, 6.99]
    clock.advance()
    provider.remaining = 7
    await monitor.run()
    clock.advance()
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


async def test_manual_ignores_and_preserves_automatic_records(monkeypatch, store, location):
    clock = Clock()
    provider = Provider(clock)
    patched_time(monkeypatch, clock)
    service = ElectricityService(provider, store, clock=clock)
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
        assert provider.calls == prior_calls  # manual sends reuse the 2 h result cache
        assert result.checked_dorms == result.low_dorms == 1
        assert result.sent_messages == 2 and result.send_failures == 0
        assert sent[-2:] == [["1", "2"], ["3"]]
        assert await automatic_records(store) == before
    count = len(sent)
    await monitor.run()
    assert len(sent) == count
    before = await automatic_records(store)
    clock.advance()
    provider.remaining = 20
    assert (await monitor.run_manual()).low_dorms == 0
    assert await automatic_records(store) == before  # even recovery is not recorded
    await service.close()


async def test_manual_does_not_consume_first_scheduled_warning(monkeypatch, store, location):
    clock = Clock()
    provider = Provider(clock)
    patched_time(monkeypatch, clock)
    service = ElectricityService(provider, store, clock=clock)
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


async def test_manual_failures_stale_and_unbind(monkeypatch, store, location):
    clock = Clock()
    provider = Provider(clock)
    patched_time(monkeypatch, clock)
    service = ElectricityService(provider, store, clock=clock)
    await bind(store, location)
    await bind(store, location, "2", "group2")

    async def send(bindings, report):
        return bindings[0].origin == "group2"

    monitor = Monitor(store, service, send, logging.getLogger())
    result = await monitor.run_manual()
    assert result.sent_messages == result.send_failures == 1
    # A dorm whose balance is missing entirely cannot judge low power: bind a
    # fresh room and drop the old ones so only the balance-less dorm remains.
    clock.advance()
    await store.unbind("bot", "group1", "1")
    await store.unbind("bot", "group2", "2")
    await bind(store, replace(location, roomName="0901"), "3", "group3")
    provider.remaining = None
    result = await monitor.run_manual()
    assert result.stale_dorms == 1 and result.sent_messages == 0

    async def fail_query():
        raise RuntimeError("temporary failure")

    # With no history at all a failed fetch counts as a query failure.
    provider.before = fail_query
    await bind(store, replace(location, roomName="0802"), "4", "group4")
    result = await monitor.run_manual()
    assert result.query_failures == 1 and result.sent_messages == 0

    async def remove_bindings():
        await store.unbind("bot", "group3", "3")

    provider.before = remove_bindings
    result = await monitor.run_manual()
    assert result.sent_messages == result.send_failures == 0
    assert await automatic_records(store) == ([], [])
    await service.close()


async def test_manual_and_scheduled_runs_are_independent(monkeypatch, store, location):
    import pytest

    from szu_electricity.models import ElectricityError

    clock = Clock()
    provider = Provider(clock)
    patched_time(monkeypatch, clock)
    service = ElectricityService(provider, store, clock=clock)
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

    async def tracked_query(location, detail=False, threshold=DEFAULT_LOW_POWER_THRESHOLD):
        nonlocal query_count
        query_count += 1
        if query_count == 2:
            joined.set()
        return await query(location, detail, threshold)

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


async def test_source_switch_does_not_join_or_relabel_previous_query(store, location):
    first_started, release = asyncio.Event(), asyncio.Event()
    official, iotun = Provider(), Provider()
    iotun.name = "iotun"

    async def wait_for_release():
        first_started.set()
        await release.wait()

    official.before = wait_for_release
    selected = [official]
    service = ElectricityService(official, store, provider_selector=lambda: selected[0])
    pending = asyncio.create_task(service.query(location))
    await first_started.wait()
    selected[0] = iotun
    current = await service.query(location)
    assert current.source == "iotun" and iotun.calls == 1
    release.set()
    previous = await pending
    assert previous.source == "official" and official.calls == 1
    await service.close()


async def test_valid_balance_can_alert_without_a_prediction(store, location):
    from types import SimpleNamespace

    now = datetime(2026, 10, 3, 0, 15, tzinfo=SHANGHAI)
    observed = datetime(2026, 10, 1, 23, 59, tzinfo=SHANGHAI)
    report = summarize(
        location, "iotun", Window.for_days(3, now.date()), ProviderResult([], 88, observed), now=now
    )

    async def query(_, *args, **kwargs):
        return report

    service = SimpleNamespace(query=query)
    await bind(store, location)
    sent = []

    async def send(bindings, result):
        sent.append(result)
        return True

    monitor = Monitor(store, service, send, logging.getLogger(), threshold=100)
    result = await monitor.run_manual()
    assert result.sent_messages == 1 and result.stale_dorms == 0
    assert sent[0].estimated_days is None
