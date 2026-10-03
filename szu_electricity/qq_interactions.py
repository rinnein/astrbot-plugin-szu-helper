"""Scoped QQ keyboard callbacks and the common text/button binding state machine."""

import asyncio
import copy
import secrets
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from .conversation import Selection
from .models import ElectricityError
from .presentation import choice_card
from .qq_messages import QQSendError, _error_code, correlation, field

INTERACTION_INTENT = 1 << 26


@dataclass
class _Hook:
    client: object
    previous: object
    wrapper: object
    ready: bool
    active: bool = True
    status: str = ""
    methods: dict = dataclass_field(default_factory=dict)
    sessions: dict = dataclass_field(default_factory=dict)


class KeyboardBridge:
    def __init__(self, context, logger):
        self.context, self.logger = context, logger
        self.hooks = {}
        self.dialogs = {}
        self.seen = OrderedDict()
        self.tasks = set()
        self.closed = False

    def attach_platforms(self):
        if self.closed:
            return
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return
        get_insts = getattr(manager, "get_insts", None)
        instances = get_insts() if callable(get_insts) else getattr(manager, "platform_insts", [])
        for adapter in instances:
            if adapter.meta().name == "qq_official":
                self.attach(adapter)

    def _refresh(self, hook):
        """Read negotiated session intents, never infer them from client.intents."""
        client = hook.client
        sockets = getattr(client, "_active_websockets", None)
        if sockets is not None:
            sessions = [
                ws._session
                for ws in sockets
                if getattr(ws, "_session", None) is not None
                and getattr(ws, "_conn", None) is not None
                and not getattr(ws._conn, "closed", False)
            ]
        else:
            # Older SDKs do not expose active sockets; our instance-scoped
            # bot_connect hook keeps the actual sessions until disconnect.
            sessions = list(hook.sessions.values())
        ready = bool(sessions) and all(
            s.get("session_id") and s.get("intent", 0) & INTERACTION_INTENT for s in sessions
        )
        status = "ready" if ready else "waiting_subscription"
        if hook.status != status:
            hook.status = status
            if ready:
                self.logger.info("QQ keyboard 已就绪：当前连接已订阅互动事件")
            else:
                self.logger.warning(
                    "QQ keyboard 等待互动订阅；当前使用 Markdown 编号选项。"
                    "若机器人已连接，请在机器人管理中重连；仅恢复旧连接不能增加订阅。"
                )
        hook.ready = ready

    @staticmethod
    def _install(hook, name, wrapper):
        client = hook.client
        hook.methods[name] = (name in vars(client), getattr(client, name, None), wrapper)
        setattr(client, name, wrapper)

    def attach(self, adapter):
        if self.closed:
            return
        platform_id = adapter.meta().id
        client = getattr(adapter, "client", None)
        if client is None or not isinstance(getattr(client, "intents", None), int):
            self.logger.debug("QQ keyboard 不支持当前客户端接口")
            return
        existing = self.hooks.get(platform_id)
        if existing and existing.client is client:
            self._refresh(existing)
            return
        if existing:
            self._detach(existing)
            for dialog in list(self.dialogs.values()):
                if dialog.platform_id == platform_id:
                    dialog.close()
        previous = getattr(client, "on_interaction_create", None)
        client.intents |= INTERACTION_INTENT
        state = _Hook(client, previous, None, False)

        async def handle(interaction):
            data = self._button_data(interaction)
            if state.active and not self.closed and data.startswith("szuh:"):
                task = asyncio.current_task()
                self.tasks.add(task)
                try:
                    await self.handle(platform_id, client.api, interaction)
                finally:
                    self.tasks.discard(task)
            elif callable(previous):
                await previous(interaction)

        state.wrapper = handle
        self._install(state, "on_interaction_create", handle)

        def readiness_handler(previous_handler):
            async def ready(*args, **kwargs):
                if state.active and not self.closed:
                    self._refresh(state)
                if callable(previous_handler):
                    await previous_handler(*args, **kwargs)

            return ready

        for name in ("on_ready", "on_resumed"):
            self._install(state, name, readiness_handler(getattr(client, name, None)))

        connect = getattr(client, "bot_connect", None)
        if callable(connect):

            async def bot_connect(session):
                if state.active and not self.closed:
                    # A new IDENTIFY can add intents. RESUME must keep the
                    # original negotiated mask and may still require reconnect.
                    if not session.get("session_id"):
                        session["intent"] = session.get("intent", 0) | INTERACTION_INTENT
                    shard = session.get("shards", {}).get("shard_id", 0)
                    state.sessions[shard] = session
                try:
                    return await connect(session)
                finally:
                    if state.active and not self.closed:
                        if state.sessions.get(shard) is session:
                            state.sessions.pop(shard, None)
                        self._refresh(state)

            self._install(state, "bot_connect", bot_connect)
            # botpy captures the bound connector when constructing ConnectionSession.
            # Hot-loading must also wrap that instance's captured callback.
            connection = getattr(client, "_connection", None)
            if getattr(connection, "_connect", None) == connect:
                connection._connect = bot_connect
        self.hooks[platform_id] = state
        self._refresh(state)

    @staticmethod
    def _payload(interaction):
        return field(interaction, "d", interaction)

    @classmethod
    def _button_data(cls, interaction):
        data = field(field(field(cls._payload(interaction), "data"), "resolved"), "button_data", "")
        return data if isinstance(data, str) else ""

    def available(self, event):
        if event.get_platform_name() != "qq_official":
            return False
        import botpy.message

        # A platform can be replaced independently of plugin reload.
        get_adapter = getattr(self.context, "get_platform_inst", None)
        adapter = get_adapter(event.get_platform_id()) if callable(get_adapter) else None
        if adapter is not None:
            self.attach(adapter)
        source = getattr(event.message_obj, "raw_message", None)
        hook = self.hooks.get(event.get_platform_id())
        supported = isinstance(source, (botpy.message.GroupMessage, botpy.message.C2CMessage))
        if hook:
            self._refresh(hook)
        ready = bool(hook and hook.ready and hook.active and supported)
        self.logger.debug(
            "QQ keyboard 可用性 status=%s",
            "unsupported" if not supported or not hook else hook.status,
        )
        return ready

    async def handle(self, platform_id, api, interaction):
        data = self._button_data(interaction)
        if not data.startswith("szuh:") or len(data) > 128:
            return
        payload = self._payload(interaction)
        # Only inline keyboard callbacks belong to this plugin, not shortcut menus.
        if field(payload, "type") != 11:
            return
        interaction_id = field(payload, "id")
        if not isinstance(interaction_id, str) or not interaction_id:
            self.logger.warning("QQ keyboard 回调缺少 interaction id")
            return
        trace = correlation(interaction_id)
        key = (platform_id, interaction_id)
        now = time.monotonic()
        while self.seen and (next(iter(self.seen.values())) < now - 300 or len(self.seen) >= 4096):
            self.seen.popitem(last=False)
        if key in self.seen:
            self.logger.debug("QQ keyboard 重复回调 trace=%s", trace)
            return
        # Reserve before any await. Even an ambiguous ACK timeout must not cause
        # a second ACK for this ID; a fresh click can retry the unchanged step.
        self.seen[key] = now
        parts = data.split(":")
        dialog = self.dialogs.get(parts[1]) if len(parts) == 4 else None
        code, action, reason = 1, None, "expired_or_unknown_flow"
        if dialog and not dialog.expired():
            scene = field(payload, "scene") or {0: "guild", 1: "group", 2: "c2c"}.get(
                field(payload, "chat_type")
            )
            peer = field(payload, "group_openid" if scene == "group" else "user_openid")
            sender = field(payload, "group_member_openid" if scene == "group" else "user_openid")
            if (
                platform_id != dialog.platform_id
                or scene != dialog.scene
                or peer != dialog.peer_id
                or sender != dialog.owner
            ):
                code, reason = 4, "wrong_owner_or_session"
            elif parts[2] != str(dialog.revision) or dialog.busy:
                code, reason = 3, "stale_or_busy"
            elif parts[3] in dialog.actions:
                code, action, reason = 0, dialog.actions[parts[3]], "accepted"
                dialog.busy = True  # reserve before the ACK yields control
            else:
                reason = "unknown_action"
        self.logger.debug("QQ keyboard 回调 trace=%s result=%s code=%s", trace, reason, code)
        try:
            async with asyncio.timeout(3):
                await api.on_interaction_result(interaction_id, code)
        except (Exception, asyncio.CancelledError) as exc:
            if action is not None:
                dialog.busy = False
            if isinstance(exc, asyncio.CancelledError):
                raise
            self.logger.warning(
                "QQ keyboard 回调确认失败 trace=%s error=%s code=%s",
                trace,
                type(exc).__name__,
                _error_code(exc),
            )
            return
        if action is not None:
            # Preserve the outer dispatch ID for the follow-up passive message.
            from types import SimpleNamespace

            trigger = SimpleNamespace(
                event_id=field(interaction, "id")
                if isinstance(interaction, Mapping) and "d" in interaction
                else field(interaction, "event_id")
            )
            await dialog.process(action, dialog.event, interaction=trigger, claimed=True)

    @staticmethod
    def _detach(hook):
        hook.active = False
        connection = getattr(hook.client, "_connection", None)
        connector = hook.methods.get("bot_connect")
        if connector and getattr(connection, "_connect", None) is connector[2]:
            connection._connect = connector[1]
        for name, (was_local, previous, wrapper) in hook.methods.items():
            if getattr(hook.client, name, None) is wrapper:
                if was_local:
                    setattr(hook.client, name, previous)
                else:
                    delattr(hook.client, name)
        hook.sessions.clear()
        # Do not remove a connection-wide Intent that another plugin may need.

    async def close(self):
        self.closed = True
        for hook in self.hooks.values():
            self._detach(hook)
        for dialog in list(self.dialogs.values()):
            dialog.close()
        self.dialogs.clear()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.hooks.clear()
        self.seen.clear()


class BindingDialog:
    def __init__(
        self, event, selection, controller, bridge, retirer, send, persist, load_catalog, logger
    ):
        self.event, self.selection, self.controller = event, selection, controller
        self.bridge, self.retirer, self.send = bridge, retirer, send
        self.persist, self.load_catalog, self.logger = persist, load_catalog, logger
        self.platform_id, self.owner = event.get_platform_id(), event.get_sender_id()
        self.scene = "group" if event.get_group_id() else "c2c"
        self.peer_id = event.get_group_id() or event.get_sender_id()
        self.token = secrets.token_urlsafe(12)
        self.revision = 0
        self._next_revision = 0
        self.actions = {}
        self.receipt = None
        self.busy = False
        self.closed = False
        self.keyboard_disabled = False
        self.deadline = time.monotonic() + 120
        bridge.dialogs[self.token] = self

    def expired(self):
        return self.closed or self.controller.future.done() or time.monotonic() >= self.deadline

    def keep(self):
        self.deadline = time.monotonic() + 120
        self.controller.keep(timeout=120, reset_timeout=True)

    async def present(self, event, selection, *, interaction=None, error=""):
        self._next_revision += 1
        revision = self._next_revision
        keyboard = not self.keyboard_disabled and self.bridge.available(self.event)
        view, actions = choice_card(
            selection, self.token, revision, self.owner, keyboard=keyboard, error=error
        )
        receipt = await self.send(event, view, interaction=interaction)
        if self.expired():
            self.retirer.schedule(receipt)
            return
        old = self.receipt
        self.selection, self.revision, self.receipt = selection, revision, receipt
        self.keep()
        if keyboard and receipt is not None and not receipt.keyboard:
            self.keyboard_disabled = True
            self.logger.warning(
                "QQ keyboard 本次流程已降级 status=rejected；继续使用 Markdown 编号选项"
            )
        self.actions = actions if keyboard and not self.keyboard_disabled else {}
        self.retirer.schedule(old)

    async def process(self, text, event, *, interaction=None, claimed=False):
        if not claimed:
            if self.expired() or self.busy:
                return
            self.busy = True
        try:
            if self.expired():
                return
            self.keep()
            if text.strip() in ("取消", "退出"):
                await self.finish(event, "已取消绑定，原配置未变更。", interaction=interaction)
                return
            candidate = copy.deepcopy(self.selection)
            location = candidate.accept(text)
            if location == "new":
                candidate = Selection(await self.load_catalog())
            if self.expired():
                return
            if location is not None and location != "new":
                confirmation = await self.persist(event, location)
                await self.finish(event, confirmation, interaction=interaction, committed=True)
                return
            await self.present(event, candidate, interaction=interaction)
        except QQSendError as exc:
            self.logger.warning("绑定消息发送失败 committed=%s reason=%s", self.closed, str(exc))
        except Exception as exc:
            self.logger.warning(
                "绑定步骤未完成：%s",
                str(exc) if isinstance(exc, ElectricityError) else type(exc).__name__,
            )
            if not self.expired():
                try:
                    await self.present(
                        event,
                        self.selection,
                        interaction=interaction,
                        error=str(exc)
                        if isinstance(exc, ElectricityError)
                        else "操作未完成，请重试。",
                    )
                except Exception as send_error:
                    self.logger.warning(
                        "绑定选项发送失败，保留上一张可操作卡片：%s", type(send_error).__name__
                    )
        finally:
            self.busy = False

    async def finish(self, event, text, *, interaction=None, committed=False):
        # Persisting a binding has already succeeded; a failed confirmation must
        # not allow an old button to repeat or replace that committed operation.
        try:
            await self.send(event, text, interaction=interaction)
        except QQSendError:
            if committed:
                self.closed = True
                self.controller.stop()
                # Leave the prior card visible but inert if the confirmation
                # failed. The committed binding must not be applied a second time.
                self.receipt = None
            raise
        self.closed = True
        self.controller.stop()
        self.retirer.schedule(self.receipt)
        self.receipt = None

    def close(self):
        self.closed = True
        self.controller.stop()
        self.bridge.dialogs.pop(self.token, None)
        self.retirer.schedule(self.receipt)
        self.receipt = None
