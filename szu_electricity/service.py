import asyncio
import time
from collections.abc import Callable

from .analytics import summarize
from .models import ElectricityError, Location, Window
from .storage import Store


class ElectricityService:
    def __init__(self, provider, store: Store, *, provider_selector: Callable | None = None):
        self._provider, self.store = provider, store
        self._provider_selector = provider_selector
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
            if cache and time.time() - cache[0] < 86400:
                return cache[1]
            async with self._limit:
                catalog = await provider.catalog()
            await self.store.save_catalog(provider.name, catalog, time.time())
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
        return await asyncio.shield(task)

    def _finished(self, key, task):
        if self._queries.get(key) is task:
            self._queries.pop(key, None)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even if every waiter was cancelled.

    async def _query(self, provider, location: Location, window: Window):
        try:
            async with self._limit, asyncio.timeout(90):
                data = await provider.query(location, window)
            return summarize(location, provider.name, window, data)
        except TimeoutError as exc:
            raise ElectricityError(f"{provider.name} 数据源电费查询超时，请稍后重试。") from exc

    async def close(self):
        tasks = list(self._queries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
