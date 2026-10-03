from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import AstrBotConfig
from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.session_waiter import (
    FILTERS,
    USER_SESSIONS,
    SessionController,
    SessionFilter,
    SessionWaiter,
)

from .szu_electricity.analytics import fmt, render
from .szu_electricity.config import Settings
from .szu_electricity.conversation import ReuseSelection, Selection
from .szu_electricity.models import SHANGHAI, Binding, ElectricityError, Location, Report
from .szu_electricity.monitor import Monitor
from .szu_electricity.presentation import detail_view
from .szu_electricity.providers import IotunProvider, OfficialProvider
from .szu_electricity.qq_interactions import BindingDialog, KeyboardBridge
from .szu_electricity.qq_messages import MessageRetirer, MessageView, QQSendError
from .szu_electricity.qq_messages import mention as qq_mention
from .szu_electricity.qq_messages import notify as qq_notify
from .szu_electricity.qq_messages import reply as qq_reply
from .szu_electricity.service import ElectricityService
from .szu_electricity.sharing import decode, encode
from .szu_electricity.storage import Store

COMMANDS = {"绑定宿舍", "导出宿舍", "用电", "解绑宿舍", "发送低电量预警", "宿舍选项"}


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
        # AstrBot 4.28 provides a dedicated logger with WebUI level controls.
        # Keep compatibility with versions predating Star.logger.
        if not hasattr(self, "logger"):
            self.logger = astrbot_logger
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
        self.keyboard = KeyboardBridge(context, self.logger)
        self.retirer = MessageRetirer(self.logger)

    async def initialize(self):
        settings = Settings.parse(self.config)
        await self.store.open()
        providers = {
            "official": OfficialProvider(logger=self.logger),
            "iotun": IotunProvider(logger=self.logger),
        }
        self.service = ElectricityService(
            providers[settings.source],
            self.store,
            provider_selector=lambda: providers[Settings.parse(self.config).source],
            logger=self.logger,
        )
        self.monitor = Monitor(
            self.store, self.service, self._notify, self.logger, settings.low_power_threshold
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
        self.keyboard.attach_platforms()
        self.logger.info(
            "SZU 插件就绪 source=%s daily_check=%s time=%02d:%02d threshold=%g",
            settings.source,
            settings.enabled,
            settings.hour,
            settings.minute,
            settings.low_power_threshold,
        )

    async def terminate(self):
        self._ready = False
        await self.keyboard.close()
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        tasks = list(self._flows.values()) + list(self._commands)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._flows.clear()
        await self.retirer.close()
        if self.monitor:
            await self.monitor.close()
        if self.service:
            await self.service.close()
        await self.store.close()
        self.logger.info("SZU 插件已停止，任务及数据库连接已关闭")

    @filter.on_platform_loaded()
    async def on_platform_loaded(self):
        self.keyboard.attach_platforms()

    @staticmethod
    def _identity(event):
        return event.get_platform_id(), event.unified_msg_origin, event.get_sender_id()

    @staticmethod
    def _argument(event) -> str:
        parts = event.message_str.strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _mention(platform: str, is_group: bool, sender_id: str, name: str, *, qq_scene=None):
        if not is_group or not sender_id:
            return []
        if platform == "qq_official":
            return [Plain(qq_mention(sender_id) + "\n")]
        return [At(qq=sender_id, name=name), Plain("\n")]

    async def _reply(self, event: AstrMessageEvent, text: str | MessageView, *, interaction=None):
        if event.get_platform_name() == "qq_official":
            return await qq_reply(event, text, self.logger, interaction=interaction)
        if isinstance(text, MessageView):
            text = text.text
        chain = self._mention(
            event.get_platform_name(),
            bool(event.get_group_id()),
            event.get_sender_id(),
            event.get_sender_name(),
        )
        message_id = getattr(event.message_obj, "message_id", None)
        if message_id:
            chain.insert(0, Reply(id=message_id))
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
        token = (
            event.message_str.strip().split(maxsplit=1)[0].lstrip("/")
            if event.message_str.strip()
            else ""
        )
        command = token if token in COMMANDS else "unknown"
        self.logger.debug("处理指令 command=%s platform=%s", command, event.get_platform_name())
        try:
            await work()
        except QQSendError as exc:
            self.logger.warning("QQ 指令回复发送失败 command=%s reason=%s", command, str(exc))
        except ElectricityError as exc:
            self.logger.warning("指令未完成 command=%s reason=%s", command, str(exc))
            await self._reply(event, str(exc))
        except Exception as exc:
            self.logger.error("指令处理异常 command=%s error=%s", command, type(exc).__name__)
            await self._reply(event, "操作未完成，请稍后重试或联系管理员查看插件日志。")
        finally:
            self._commands.discard(task)

    async def _cancel_flow(self, event):
        key = SenderSessionFilter().filter(event)
        old = self._flows.pop(key, None)
        if old:
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)

    async def _persist_binding(self, event, location: Location):
        await self.store.bind(
            *self._identity(event),
            event.get_sender_name(),
            bool(event.get_group_id()),
            event.get_platform_name(),
            location,
        )
        text = f"已绑定：{location.buildingName} {location.roomName}。可发送 /用电 查询。"
        self.logger.info(
            "宿舍绑定已保存 platform=%s group=%s",
            event.get_platform_name(),
            bool(event.get_group_id()),
        )
        platform = self.context.get_platform_inst(event.get_platform_id())
        if platform is not None and not getattr(platform.meta(), "support_proactive_message", True):
            text += "\n当前平台不支持定时主动推送，仍可使用查询命令。"
        return text

    async def _save(self, event, location: Location):
        return await self._reply(event, await self._persist_binding(event, location))

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
            self.logger.debug("发现可复用的宿舍配置 count=%s", len(locations))
            # Reusing an already saved location is a local operation. Only a
            # choice to start over needs the currently selected data source.
            await self._select(event, ReuseSelection(locations))
            return
        await self._select(event, Selection(await self.service.catalog()))

    @filter.command("宿舍选项")
    async def select_dorm_option(self, event: AstrMessageEvent):
        """处理 QQ 选项表格生成的流程指令。"""

        async def work():
            await self.keyboard.handle_command(event, self._argument(event), self._reply)

        await self._guard(event, work)

    async def _select(self, event, selection: Selection | ReuseSelection):
        session_filter = SenderSessionFilter(event)
        session_id = session_filter.filter(event)
        session = SessionWaiter(session_filter, session_id, record_history_chains=False)
        dialog = BindingDialog(
            event,
            selection,
            session.session_controller,
            self.keyboard,
            self.retirer,
            self._reply,
            self._persist_binding,
            self.service.catalog,
            self.logger,
        )

        async def waiter(controller: SessionController, reply: AstrMessageEvent):
            text = reply.message_str.strip()
            command = text.lstrip("/").split(maxsplit=1)[0] if text else ""
            if command in COMMANDS or text.startswith("/"):
                return
            reply.stop_event()
            await dialog.process(text, reply)

        # This is the same registration used by AstrBot's session_waiter helper,
        # retaining the controller so button callbacks can reset its timeout too.
        FILTERS.append(session_filter)
        task = asyncio.create_task(session.register_wait(waiter, timeout=120))
        try:
            await asyncio.sleep(0)
            await dialog.present(event, selection)
            await task
        except TimeoutError:
            await self._reply(event, "绑定已超时，原配置未变更。请重新发送 /绑定宿舍。")
        finally:
            dialog.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if USER_SESSIONS.get(session_id) is session:
                USER_SESSIONS.pop(session_id, None)
            if session_filter in FILTERS:
                FILTERS.remove(session_filter)
            if session.session_controller.current_event:
                session.session_controller.current_event.set()

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
            if arg == "详情" and event.get_platform_name() == "qq_official":
                await self._reply(
                    event, detail_view(report, Settings.parse(self.config).detail_layout)
                )
            else:
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
        adapter = self.context.get_platform_inst(bindings[0].platform_id)
        if adapter is None:
            raise ElectricityError("绑定的平台实例已不可用，请在目标会话重新绑定宿舍。")
        metadata = adapter.meta()
        platform = metadata.name
        if not getattr(metadata, "support_proactive_message", True):
            raise ElectricityError(
                f"当前 {platform} 适配器声明不支持主动消息，请检查 AstrBot 版本及平台配置。"
            )
        text = f"宿舍 {report.location.buildingName} {report.location.roomName} 剩余电量 {fmt(report.remaining)} 度，低于 {self.monitor.threshold:g} 度，请及时充值。"
        if platform == "qq_official":
            await qq_notify(
                adapter, bindings[0], [b.sender_id for b in bindings], text, self.logger
            )
            sent = True
        else:
            chain = []
            for binding in bindings:
                chain.extend(
                    self._mention(
                        platform,
                        binding.is_group,
                        binding.sender_id,
                        binding.sender_name,
                    )
                )
            sent = await self.context.send_message(
                bindings[0].origin, MessageChain([*chain, Plain(text)])
            )
            if not sent:
                raise ElectricityError("主动发送时未找到目标平台实例，请检查机器人连接状态。")
        self.logger.info(
            "低电量预警已发送 platform=%s group=%s recipients=%s",
            platform,
            bindings[0].is_group,
            len(bindings),
        )
        return bool(sent)
