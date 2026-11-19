import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as time_of_day

from .analytics import summarize
from .models import (
    CACHE_TTL_SECONDS,
    DEFAULT_LOW_POWER_THRESHOLD,
    SHANGHAI,
    Check,
    ElectricityError,
    Location,
    ProviderResult,
    Report,
    Window,
    local_now,
    number,
    timestamp,
)
from .storage import RoomState, Store

RETRY_BACKOFF_SECONDS = 60.0


def validate_room_name(room_name: str) -> str:
    name = room_name.strip()
    if not (name.isascii() and name.isdigit() and 3 <= len(name) <= 5):
        raise ElectricityError(
            "宿舍号需为 3～5 位数字（一至两位楼层加两位房间号），例如 601 或 1201。"
        )
    return name


def _valid_until(now: float) -> float:
    """A fetched day stays fresh until tonight's 23:59 publication.

    Publication is approximate. During the midnight window, retry after five
    minutes instead of caching an early/empty response for another whole day.
    """
    local = datetime.fromtimestamp(now, SHANGHAI)
    boundary = local.replace(hour=23, minute=59, second=0, microsecond=0)
    if local >= boundary or (local.hour == 0 and local.minute < 10):
        return now + 5 * 60
    return boundary.timestamp()


def _settled_at(day: date) -> float:
    """Fetches at or after the next day's 00:10 count as settled for `day`."""
    return datetime.combine(
        day + timedelta(days=1), time_of_day(0, 10), tzinfo=SHANGHAI
    ).timestamp()


def _in_window(text: str, window: Window) -> bool:
    when = timestamp(text)
    return when is not None and window.begin <= when.date() <= window.end


class ElectricityService:
    def __init__(
        self,
        provider,
        store: Store,
        *,
        provider_selector: Callable | None = None,
        clock: Callable[[], float] = time.time,
        logger=None,
    ):
        self._provider, self.store = provider, store
        self._provider_selector = provider_selector
        self._clock = clock
        self.logger = logger or logging.getLogger(__name__)
        self._catalog_lock = asyncio.Lock()
        self._limit = asyncio.Semaphore(3)
        self._queries: dict[tuple, asyncio.Task] = {}

    @property
    def provider(self):
        return self._provider_selector() if self._provider_selector else self._provider

    async def catalog(self):
        provider = self.provider
        async with self._catalog_lock:
            cache = await self.store.catalog(provider.name)
            if cache and 0 <= self._clock() - cache[0] < CACHE_TTL_SECONDS:
                self.logger.debug(
                    "目录缓存命中 source=%s age_seconds=%.1f",
                    provider.name,
                    self._clock() - cache[0],
                )
                return cache[1]
            self.logger.info("获取宿舍目录 source=%s", provider.name)
            try:
                async with self._limit:
                    catalog = await provider.catalog()
            except Exception as exc:
                self.logger.warning(
                    "宿舍目录获取失败 source=%s reason=%s",
                    provider.name,
                    str(exc) if isinstance(exc, ElectricityError) else type(exc).__name__,
                )
                raise
            await self.store.save_catalog(provider.name, catalog, self._clock())
            self.logger.info(
                "宿舍目录获取完成 source=%s areas=%s buildings=%s",
                provider.name,
                len(catalog.areas),
                sum(len(a.buildings) for a in catalog.areas),
            )
            return catalog

    async def query(
        self,
        location: Location,
        detail=False,
        threshold: float = DEFAULT_LOW_POWER_THRESHOLD,
    ):
        validate_room_name(location.roomName)
        key = (self.provider.name, location.key, detail)
        task = self._queries.get(key)
        if task is None:
            task = asyncio.create_task(self._query(self.provider, location, detail, threshold))
            self._queries[key] = task
            task.add_done_callback(lambda t: self._finished(key, t))
        else:
            self.logger.debug("复用进行中的查询 source=%s detail=%s", self.provider.name, detail)
        return await asyncio.shield(task)

    def _finished(self, key, task):
        if self._queries.get(key) is task:
            self._queries.pop(key, None)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even if every waiter was cancelled.

    @staticmethod
    def _error_text(provider, exc: Exception) -> str:
        if isinstance(exc, ElectricityError):
            return str(exc)
        if isinstance(exc, TimeoutError):
            return f"{provider.name} 数据源电费查询超时，请稍后重试。"
        return "电费查询失败，请稍后重试。"

    async def _fetch(self, provider, location: Location, work) -> object:
        async with self._limit, asyncio.timeout(90):
            return await work()

    async def _query(self, provider, location: Location, detail: bool, threshold: float):
        now = self._clock()
        window = Window.for_days(31 if detail else 3, end=local_now().date())
        room = await self.store.load_room(provider.name, location.key)
        # A recent failure backs off; waiting callers reuse the cached history.
        if now < room.retry_after:
            if room.usage_updated_at is None:
                raise ElectricityError(room.error or "电量查询暂不可用，请稍后重试。")
            return self._report(provider.name, location, window, room, threshold, now, room.error)
        fetched = False
        for chunk in self._required_windows(window, room, now, force=False, recharge=False):
            try:
                data = await self._fetch(
                    provider, location, lambda c=chunk: provider.query(location, c)
                )
            except Exception as exc:
                room.error = self._error_text(provider, exc)
                room.retry_after = self._clock() + RETRY_BACKOFF_SECONDS
                break
            self._merge_usage(room, chunk, data, self._clock())
            fetched = True
            room.error = None
            room.retry_after = 0.0
        # A missing estimate means the recent window lacks at least one day of
        # data, yet the fresh marks above would keep serving this cache all
        # day. Retry the recent days once with force semantics unless this
        # round already fetched or the first pass failed (backoff).
        if not fetched and room.error is None:
            probe = self._report(provider.name, location, window, room, threshold, now, None)
            if probe.estimated_days is None:
                for chunk in self._required_windows(
                    window, room, self._clock(), force=True, recharge=False
                ):
                    try:
                        data = await self._fetch(
                            provider, location, lambda c=chunk: provider.query(location, c)
                        )
                    except Exception as exc:
                        room.error = self._error_text(provider, exc)
                        room.retry_after = self._clock() + RETRY_BACKOFF_SECONDS
                        break
                    self._merge_usage(room, chunk, data, self._clock())
                    fetched = True
                    room.error = None
                    room.retry_after = 0.0
        if getattr(provider, "supports_recharges", False):
            await self._refresh_recharges(provider, location, room, window, force=False)
        await self.store.save_room(provider.name, location.key, room, self._clock())
        if room.usage_updated_at is None:
            raise ElectricityError(room.error or "尚无本地用电数据。")
        report = self._report(
            provider.name, location, window, room, threshold, self._clock(), room.error
        )
        self.logger.info(
            "用电查询完成 source=%s detail=%s fetched=%s observed_at=%s usable=%s warned=%s",
            provider.name,
            detail,
            fetched,
            report.observed_at,
            report.estimated_days is not None,
            report.warning_reason is not None,
        )
        if not fetched:
            self.logger.debug("用电 fresh 命中 source=%s days=%s", provider.name, window.days)
        return report

    async def _refresh_recharges(
        self, provider, location: Location, room: RoomState, window: Window, *, force: bool
    ):
        now = self._clock()
        if not force and now < room.recharge_retry_after:
            return
        room.recharge_error = None
        for chunk in self._required_windows(window, room, now, force=force, recharge=True):
            try:
                rows = await self._fetch(
                    provider, location, lambda c=chunk: provider.recharges(location, c)
                )
            except Exception as exc:
                # Recharge failures do not block the usage result.
                room.recharge_error = self._error_text(provider, exc)
                room.recharge_retry_after = self._clock() + RETRY_BACKOFF_SECONDS
                break
            now = self._clock()
            room.recharges = [r for r in room.recharges if not _in_window(r.time, chunk)]
            room.recharges.extend(r for r in rows if _in_window(r.time, chunk))
            self._mark(room.recharge_checks, chunk, now)
            room.recharge_updated_at = now
            room.recharge_retry_after = 0.0
        room.recharges.sort(
            key=lambda r: timestamp(r.time) or datetime.min.replace(tzinfo=SHANGHAI),
            reverse=True,
        )

    @staticmethod
    def _mark(checks: dict[str, Check], window: Window, now: float):
        expires = _valid_until(now)
        day = window.begin
        while day <= window.end:
            checks[day.isoformat()] = Check(now, expires)
            day += timedelta(days=1)

    def _merge_usage(self, room: RoomState, chunk: Window, data: ProviderResult, now: float):
        # Keep prior values if a temporarily incomplete response omits them;
        # new rows replace the same day's previous snapshot.
        for reading in sorted(data.readings, key=lambda r: r.at):
            day = reading.at.date()
            if not chunk.begin <= day <= chunk.end:
                continue
            room.readings = [r for r in room.readings if r.at.date() != day]
            room.readings.append(reading)
        balance = number(data.remaining)
        if balance is not None:
            room.balance = balance
            room.balance_observed_at = data.observed_at or local_now()
        self._mark(room.usage_checks, chunk, now)
        room.usage_updated_at = now

    def _required_windows(
        self,
        window: Window,
        room: RoomState,
        now: float,
        *,
        force: bool,
        recharge: bool,
    ) -> list[Window]:
        """Windows that actually need fetching for the requested range.

        Old positive usage readings and confirmed historical recharge windows
        are stable. Empty usage days are retried once per publication cycle.
        """
        today = local_now().date()
        checks = room.recharge_checks if recharge else room.usage_checks
        ranges: list[Window] = []
        current: Window | None = None
        day = window.begin
        while day <= window.end:
            key = day.isoformat()
            old = day < today - timedelta(days=2)
            check = checks.get(key)
            settled = check is not None and check.fetched_at >= _settled_at(day)
            if recharge:
                stable = old and settled and check is not None
            else:
                stable = (
                    old
                    and settled
                    and any(
                        reading.at.date() == day
                        and (
                            number(reading.cumulative) is not None
                            or number(reading.daily) is not None
                        )
                        for reading in room.readings
                    )
                )
            fresh = (
                check is not None
                and check.expires_at > now
                and check.expires_at <= now + 24 * 60 * 60
            )
            needed = (not stable and not fresh) or (force and not old)
            if needed:
                current = Window(day, day) if current is None else Window(current.begin, day)
            elif current is not None:
                ranges.append(current)
                current = None
            day += timedelta(days=1)
        if current is not None:
            ranges.append(current)
        chunks = []
        for entry in ranges:
            chunks.extend(entry.chunks(limit=20))
        return chunks

    @staticmethod
    def _report(
        source: str,
        location: Location,
        window: Window,
        room: RoomState,
        threshold: float,
        now: float,
        refresh_error: str | None,
    ) -> Report:
        # The prediction window spans further back than the query window, so
        # feed the whole cached history into the summary.
        report = summarize(
            location,
            source,
            window,
            ProviderResult(room.readings, room.balance, room.balance_observed_at),
            room.recharges,
            threshold=threshold,
            now=datetime.fromtimestamp(now, SHANGHAI),
        )
        if refresh_error:
            report = replace(report, refresh_error=refresh_error)
        return report

    async def close(self):
        tasks = list(self._queries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
