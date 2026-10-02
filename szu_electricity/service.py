import asyncio
import time

from .analytics import summarize
from .models import ElectricityError, Location, Window
from .storage import Store


class ElectricityService:
    def __init__(self, provider, store: Store):
        self.provider, self.store = provider, store
        self._catalog_lock = asyncio.Lock()
        self._limit = asyncio.Semaphore(3)
        self._queries: dict[tuple, asyncio.Task] = {}

    async def catalog(self):
        async with self._catalog_lock:
            cache = await self.store.catalog(self.provider.name)
            if cache and time.time() - cache[0] < 86400:
                return cache[1]
            async with self._limit:
                catalog = await self.provider.catalog()
            await self.store.save_catalog(self.provider.name, catalog, time.time())
            return catalog

    async def query(self, location: Location, detail=False):
        window = Window.for_days(31 if detail else 3)
        key = (location.key, window.begin, window.end)
        task = self._queries.get(key)
        if task is None:
            task = asyncio.create_task(self._query(location, window))
            self._queries[key] = task
            task.add_done_callback(lambda t: self._finished(key, t))
        return await asyncio.shield(task)

    def _finished(self, key, task):
        if self._queries.get(key) is task:
            self._queries.pop(key, None)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even if every waiter was cancelled.

    async def _query(self, location: Location, window: Window):
        try:
            async with self._limit, asyncio.timeout(90):
                data = await self.provider.query(location, window)
            return summarize(location, self.provider.name, window, data)
        except TimeoutError as exc:
            raise ElectricityError("电费查询超时，请稍后重试。") from exc

    async def close(self):
        tasks = list(self._queries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
