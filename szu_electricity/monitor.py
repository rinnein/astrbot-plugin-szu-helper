import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .models import DEFAULT_LOW_POWER_THRESHOLD, Binding, ElectricityError, Report
from .storage import Store


@dataclass
class CheckSummary:
    checked_dorms: int = 0
    low_dorms: int = 0
    sent_messages: int = 0
    send_failures: int = 0
    query_failures: int = 0
    stale_dorms: int = 0

    def message(self) -> str:
        return (
            f"手动预警检查完成：检查 {self.checked_dorms} 间宿舍，"
            f"其中 {self.low_dorms} 间低于阈值；已发送 {self.sent_messages} 条预警。\n"
            f"查询失败 {self.query_failures} 间，读数过期或缺失 {self.stale_dorms} 间，"
            f"发送失败 {self.send_failures} 条。定时安排及自动预警记录未变更。"
        )


class Monitor:
    def __init__(
        self,
        store: Store,
        service,
        send: Callable[[list[Binding], Report], Awaitable[bool]],
        logger,
        threshold: float = DEFAULT_LOW_POWER_THRESHOLD,
    ):
        self.store, self.service, self.send, self.logger = store, service, send, logger
        self.threshold = threshold
        self._lock = asyncio.Lock()
        self._manual_lock = asyncio.Lock()
        self._running: set[asyncio.Task] = set()

    async def run(self):
        if self._lock.locked():
            return
        async with self._lock:
            await self._scan(manual=False)

    async def run_manual(self) -> CheckSummary:
        if self._manual_lock.locked():
            raise ElectricityError("已有手动预警检查正在执行，请等待完成后再试。")
        async with self._manual_lock:
            return await self._scan(manual=True)

    async def _scan(self, *, manual: bool) -> CheckSummary:
        groups = defaultdict(list)
        for binding in await self.store.bindings():
            groups[binding.location.key].append(binding)
        tasks = [
            asyncio.create_task(self._check(bindings, manual=manual))
            for bindings in groups.values()
        ]
        self._running.update(tasks)
        try:
            results = await asyncio.gather(*tasks)
            return CheckSummary(
                **{
                    field: sum(getattr(result, field) for result in results)
                    for field in CheckSummary.__dataclass_fields__
                }
            )
        finally:
            self._running.difference_update(tasks)

    async def _check(self, bindings: list[Binding], *, manual: bool) -> CheckSummary:
        result = CheckSummary(checked_dorms=1)
        try:
            report = await self.service.query(bindings[0].location)
            if report.expired or report.remaining is None or report.observed_at is None:
                result.stale_dorms = 1
                return result
            result.low_dorms = int(report.remaining < self.threshold)
            if manual:
                if not result.low_dorms:
                    return result
                episode = None
            else:
                episode = await self.store.observe(report, self.threshold)
                if episode is None:
                    return result
            groups = defaultdict(list)
            for binding in bindings:
                groups[binding.origin].append(binding.id)
            for origin, ids in groups.items():
                try:

                    async def send(pending):
                        async with asyncio.timeout(20):
                            return await self.send(pending, report)

                    sent = await self.store.deliver(report.location.key, episode, origin, ids, send)
                    result.sent_messages += int(sent is True)
                    result.send_failures += int(sent is False)
                except Exception as exc:
                    result.send_failures += 1
                    self.logger.warning(f"SZU 低电量提醒发送失败：{type(exc).__name__}")
        except Exception as exc:
            result.query_failures = 1
            self.logger.warning(f"SZU 宿舍检测失败：{type(exc).__name__}")
        return result

    async def close(self):
        tasks = list(self._running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
