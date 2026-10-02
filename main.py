from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.session_waiter import SessionController, SessionFilter, session_waiter

from .szu_electricity.analytics import fmt, render
from .szu_electricity.config import Settings
from .szu_electricity.conversation import ReuseSelection, Selection
from .szu_electricity.models import SHANGHAI, Binding, ElectricityError, Location, Report
from .szu_electricity.monitor import Monitor
from .szu_electricity.providers import IotunProvider, OfficialProvider
from .szu_electricity.service import ElectricityService
from .szu_electricity.sharing import decode, encode
from .szu_electricity.storage import Store

COMMANDS = {"绑定宿舍", "导出宿舍", "用电", "解绑宿舍", "发送低电量预警"}
MENTION_PLATFORMS = {"aiocqhttp", "telegram", "discord", "slack", "lark", "dingtalk"}
NO_PROACTIVE = {"qq_official", "webchat"}


class SenderSessionFilter(SessionFilter):
    def __init__(self, initial_event=None):
        self.initial_event = initial_event

    def filter(self, event: AstrMessageEvent) -> str:
        # AstrBot's built-in session agent stops every matching event, even if
        # the waiter returns without handling it. Exclude commands here so a
        # second bind/unbind can reach its registered command handler.
        text = event.message_str.strip()
        command = text.lstrip("/").split(maxsplit=1)[0] if text else ""
        if self.initial_event is not None and event is not self.initial_event:
            expected = self.initial_event
            same_sender = (
                event.get_platform_id(),
                event.unified_msg_origin,
                event.get_sender_id(),
            ) == (expected.get_platform_id(), expected.unified_msg_origin, expected.get_sender_id())
            if not same_sender or command in COMMANDS or text.startswith("/"):
                return "szu-helper:ignored-command"
        return "szu-helper:" + repr(
            (event.get_platform_id(), event.unified_msg_origin, event.get_sender_id())
        )


class SzuHelperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = Store(
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_szu_helper"
            / "electricity.sqlite3"
        )
        self.service = None
        self.monitor = None
        self.scheduler = None
        self._flows: dict[str, asyncio.Task] = {}
        self._flow_locks = defaultdict(asyncio.Lock)
        self._commands: set[asyncio.Task] = set()
        self._ready = False

    async def initialize(self):
        settings = Settings.parse(self.config)
        await self.store.open()
        providers = {"official": OfficialProvider(), "iotun": IotunProvider()}
        self.service = ElectricityService(
            providers[settings.source],
            self.store,
            provider_selector=lambda: providers[Settings.parse(self.config).source],
        )
        self.monitor = Monitor(
            self.store, self.service, self._notify, logger, settings.low_power_threshold
        )
        self.scheduler = AsyncIOScheduler(timezone=SHANGHAI)
        if settings.enabled:
            self.scheduler.add_job(
                self.monitor.run,
                CronTrigger(hour=settings.hour, minute=settings.minute, timezone=SHANGHAI),
                id="szu-electricity-daily",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
                replace_existing=True,
            )
        self.scheduler.start()
        self._ready = True

    async def terminate(self):
        self._ready = False
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        tasks = list(self._flows.values()) + list(self._commands)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._flows.clear()
        if self.monitor:
            await self.monitor.close()
        if self.service:
            await self.service.close()
        await self.store.close()

    @staticmethod
    def _identity(event):
        return event.get_platform_id(), event.unified_msg_origin, event.get_sender_id()

    @staticmethod
    def _argument(event) -> str:
        parts = event.message_str.strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _mention(platform: str, is_group: bool, sender_id: str, name: str):
        if not is_group or not sender_id:
            return []
        if platform in MENTION_PLATFORMS:
            return [At(qq=sender_id, name=name), Plain("\n")]
        return [Plain(f"{name or sender_id}\n")]

    async def _reply(self, event: AstrMessageEvent, text: str):
        chain = self._mention(
            event.get_platform_name(),
            bool(event.get_group_id()),
            event.get_sender_id(),
            event.get_sender_name(),
        )
        # Keep the payload in its own Plain component, including share codes.
        await event.send(MessageChain([*chain, Plain(text)]))

    async def _guard(self, event, work):
        event.stop_event()
        if not self._ready:
            await self._reply(event, "插件尚未就绪，请稍后重试。")
            return
        if not all(self._identity(event)):
            await self._reply(event, "当前平台未提供完整的会话或发送人标识，无法绑定。")
            return
        task = asyncio.current_task()
        self._commands.add(task)
        try:
            await work()
        except ElectricityError as exc:
            await self._reply(event, str(exc))
        except Exception as exc:
            logger.error(f"SZU 指令处理失败：{type(exc).__name__}")
            await self._reply(event, "操作未完成，请稍后重试或联系管理员查看插件日志。")
        finally:
            self._commands.discard(task)

    async def _cancel_flow(self, event):
        key = SenderSessionFilter().filter(event)
        old = self._flows.pop(key, None)
        if old:
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)

    async def _save(self, event, location: Location):
        await self.store.bind(
            *self._identity(event),
            event.get_sender_name(),
            bool(event.get_group_id()),
            event.get_platform_name(),
            location,
        )
        text = f"已绑定：{location.buildingName} {location.roomName}。可发送 /用电 查询。"
        if event.get_platform_name() in NO_PROACTIVE:
            text += "\n当前平台不支持定时主动推送，仍可使用查询命令。"
        await self._reply(event, text)

    @filter.command("绑定宿舍")
    async def bind_dorm(self, event: AstrMessageEvent):
        """对话式绑定宿舍，或在命令后粘贴 Base16384 配置码。"""

        async def work():
            key = SenderSessionFilter().filter(event)
            async with self._flow_locks[key]:
                await self._cancel_flow(event)
                # Track catalog loading too: a later bind must supersede it.
                task = asyncio.create_task(self._begin_bind(event))
                self._flows[key] = task
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled() or not self._ready:
                    raise
            finally:
                if self._flows.get(key) is task:
                    self._flows.pop(key, None)

        await self._guard(event, work)

    async def _begin_bind(self, event):
        code = self._argument(event)
        if code:
            location = decode(code)
            catalog = await self.service.catalog()
            await self._save(event, catalog.validate(location))
            return
        locations = await self.store.other_locations(*self._identity(event))
        if locations:
            # Reusing an already saved location is a local operation. Only a
            # choice to start over needs the currently selected data source.
            await self._select(event, ReuseSelection(locations))
            return
        await self._select(event, Selection(await self.service.catalog()))

    async def _select(self, event, selection: Selection | ReuseSelection):
        @session_waiter(timeout=120, record_history_chains=False)
        async def waiter(controller: SessionController, reply: AstrMessageEvent):
            nonlocal selection
            text = reply.message_str.strip()
            command = text.lstrip("/").split(maxsplit=1)[0] if text else ""
            if command in COMMANDS or text.startswith("/"):
                return
            reply.stop_event()
            if text in ("取消", "退出"):
                controller.stop()
                await self._reply(reply, "已取消绑定，原配置未变更。")
                return
            try:
                location = selection.accept(text)
                if controller.future.done():
                    return
                if location == "new":
                    controller.keep(timeout=120, reset_timeout=True)
                    catalog = await self.service.catalog()
                    if controller.future.done():
                        return
                    selection = Selection(catalog)
                elif location:
                    await self._save(reply, location)
                    controller.stop()
                    return
                prompt = selection.prompt()
            except ElectricityError as exc:
                prompt = f"{exc}\n{selection.prompt()}"
            controller.keep(timeout=120, reset_timeout=True)
            await self._reply(reply, prompt)

        task = asyncio.create_task(waiter(event, session_filter=SenderSessionFilter(event)))
        try:
            await asyncio.sleep(0)
            await self._reply(event, selection.prompt())
            await task
        except TimeoutError:
            await self._reply(event, "绑定已超时，原配置未变更。请重新发送 /绑定宿舍。")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _binding(self, event):
        binding = await self.store.get_binding(*self._identity(event))
        if binding is None:
            raise ElectricityError("当前会话尚未绑定宿舍，请先发送 /绑定宿舍。")
        return binding

    @filter.command("导出宿舍")
    async def export_dorm(self, event: AstrMessageEvent):
        """导出当前会话的宿舍 Base16384 分享码。"""

        async def work():
            binding = await self._binding(event)
            await self._reply(event, encode(binding.location))

        await self._guard(event, work)

    @filter.command("用电")
    async def electricity(self, event: AstrMessageEvent):
        """查询当前电量；使用 /用电 详情 查看 31 天统计。"""

        async def work():
            arg = self._argument(event)
            if arg not in ("", "详情"):
                raise ElectricityError("用法：/用电 或 /用电 详情。")
            binding = await self._binding(event)
            report = await self.service.query(binding.location, detail=arg == "详情")
            await self._reply(event, render(report, detail=arg == "详情"))

        await self._guard(event, work)

    @filter.command("解绑宿舍")
    async def unbind_dorm(self, event: AstrMessageEvent):
        """解除当前会话中自己的宿舍绑定和提醒订阅。"""

        async def work():
            async with self._flow_locks[SenderSessionFilter().filter(event)]:
                await self._cancel_flow(event)
                await self.store.unbind(*self._identity(event))
            await self._reply(event, "已解除当前会话的宿舍绑定及提醒订阅。")

        await self._guard(event, work)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发送低电量预警")
    async def send_low_power_alert(self, event: AstrMessageEvent):
        """管理员手动检查全部绑定宿舍并发送预警，不改变定时或自动预警记录。"""
        # Also enforce authorization when invoked directly by another handler.
        if not event.is_admin():
            event.stop_event()
            await self._reply(event, "仅 AstrBot 管理员可使用此命令。")
            return

        async def work():
            if self._argument(event):
                raise ElectricityError("用法：/发送低电量预警（无需参数）。")
            await self._reply(event, "正在检查所有已绑定宿舍，按当前阈值发送一次低电量预警。")
            summary = await self.monitor.run_manual()
            await self._reply(event, summary.message())

        await self._guard(event, work)

    async def _notify(self, bindings: list[Binding], report: Report) -> bool:
        platform = bindings[0].platform_name
        if platform in NO_PROACTIVE:
            logger.warning(f"SZU 无法主动推送：平台 {platform} 不支持主动消息。")
            return False
        chain = []
        for binding in bindings:
            chain.extend(
                self._mention(platform, binding.is_group, binding.sender_id, binding.sender_name)
            )
        chain.append(
            Plain(
                f"宿舍 {report.location.buildingName} {report.location.roomName} 剩余电量 {fmt(report.remaining)} 度，低于 {self.monitor.threshold:g} 度，请及时充值。"
            )
        )
        sent = await self.context.send_message(bindings[0].origin, MessageChain(chain))
        if not sent:
            logger.warning("SZU 低电量提醒未发送：找不到对应的平台实例。")
        return bool(sent)
