import asyncio
import logging
import time
from collections.abc import Callable

from .analytics import summarize
from .models import CACHE_TTL_SECONDS, ElectricityError, Location, Window, local_now
from .storage import Store


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

    async def query(self, location: Location, detail=False):
        provider = self.provider
        window = Window.for_days(31 if detail else 3)
        key = (provider.name, location.key, window.begin, window.end)
        task = self._queries.get(key)
        if task is None:
            task = asyncio.create_task(self._query(provider, location, window))
            self._queries[key] = task
            task.add_done_callback(lambda t: self._finished(key, t))
        else:
            self.logger.debug("复用进行中的查询 source=%s days=%s", provider.name, window.days)
        return await asyncio.shield(task)

    def _finished(self, key, task):
        if self._queries.get(key) is task:
            self._queries.pop(key, None)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even if every waiter was cancelled.

    async def _query(self, provider, location: Location, window: Window):
        cache = await self.store.cached_query(provider.name, location, window)
        if cache and 0 <= self._clock() - cache[0] < CACHE_TTL_SECONDS:
            report = summarize(location, provider.name, window, cache[1], now=local_now())
            if not report.expired and report.remaining is not None:
                self.logger.debug(
                    "用电缓存命中 source=%s days=%s age_seconds=%.1f observed_at=%s",
                    provider.name,
                    window.days,
                    self._clock() - cache[0],
                    report.observed_at,
                )
                return report
            self.logger.debug(
                "缓存读数不可用，重新查询 source=%s reason=%s",
                provider.name,
                report.unavailable_reason,
            )
        try:
            started = time.monotonic()
            self.logger.info(
                "开始用电查询 source=%s begin=%s end=%s", provider.name, window.begin, window.end
            )
            async with self._limit, asyncio.timeout(90):
                data = await provider.query(location, window)
            report = summarize(location, provider.name, window, data, now=local_now())
            self.logger.info(
                "用电查询完成 source=%s rows=%s valid_days=%s balance_present=%s observed_at=%s usable=%s elapsed_ms=%.0f",
                provider.name,
                len(data.readings),
                report.valid_days,
                report.remaining is not None,
                report.observed_at,
                not report.expired,
                (time.monotonic() - started) * 1000,
            )
            if not report.expired and report.remaining is not None:
                await self.store.save_query(provider.name, location, window, data, self._clock())
            else:
                self.logger.warning(
                    "余额暂不可用于预警 source=%s reason=%s observed_at=%s",
                    provider.name,
                    report.unavailable_reason,
                    report.observed_at,
                )
            return report
        except TimeoutError as exc:
            self.logger.warning("用电查询超时 source=%s days=%s", provider.name, window.days)
            raise ElectricityError(f"{provider.name} 数据源电费查询超时，请稍后重试。") from exc
        except Exception as exc:
            self.logger.warning(
                "用电查询失败 source=%s reason=%s",
                provider.name,
                str(exc) if isinstance(exc, ElectricityError) else type(exc).__name__,
            )
            raise

    async def close(self):
        tasks = list(self._queries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
