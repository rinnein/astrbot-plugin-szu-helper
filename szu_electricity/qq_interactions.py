"""Scoped QQ keyboard callbacks and the common text/button binding state machine."""

import asyncio
import copy
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

from .conversation import Selection
from .models import ElectricityError
from .presentation import choice_card

INTERACTION_INTENT = 1 << 26


@dataclass
class _Hook:
    client: object
    previous: object
    wrapper: object
    ready: bool
    active: bool = True


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

    def attach(self, adapter):
        if self.closed:
            return
        platform_id = adapter.meta().id
        client = getattr(adapter, "client", None)
        if client is None or not isinstance(getattr(client, "intents", None), int):
            return
        existing = self.hooks.get(platform_id)
        if existing and existing.client is client:
            return
        if existing:
            self._detach(existing)
            for dialog in list(self.dialogs.values()):
                if dialog.platform_id == platform_id:
                    dialog.close()
        previous = getattr(client, "on_interaction_create", None)
        subscribed = bool(client.intents & INTERACTION_INTENT)
        connected = getattr(client, "_connection", None) is not None
        needs_reconnect = getattr(client, "_szu_interaction_needs_reconnect", False) or (
            connected and not subscribed
        )
        client._szu_interaction_needs_reconnect = needs_reconnect
        client.intents |= INTERACTION_INTENT
        state = _Hook(client, previous, None, not needs_reconnect)

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
        client.on_interaction_create = handle
        self.hooks[platform_id] = state
        if needs_reconnect:
            self.logger.warning("QQ keyboard 需要重连此机器人以订阅互动事件；当前使用编号选项")
        else:
            self.logger.info("QQ keyboard 互动事件已接入")

    @staticmethod
    def _button_data(interaction):
        data = getattr(
            getattr(getattr(interaction, "data", None), "resolved", None), "button_data", ""
        )
        return data if isinstance(data, str) else ""

    def available(self, event):
        if event.get_platform_name() != "qq_official":
            return False
        import botpy.message

        source = getattr(event.message_obj, "raw_message", None)
        hook = self.hooks.get(event.get_platform_id())
        return bool(
            hook
            and hook.ready
            and hook.active
            and isinstance(source, (botpy.message.GroupMessage, botpy.message.C2CMessage))
        )

    async def handle(self, platform_id, api, interaction):
        data = self._button_data(interaction)
        if not data.startswith("szuh:") or len(data) > 128:
            return
        interaction_id = getattr(interaction, "id", None)
        if not isinstance(interaction_id, str) or not interaction_id:
            self.logger.warning("QQ keyboard 回调缺少 interaction id")
            return
        key = (platform_id, interaction_id)
        now = time.monotonic()
        while self.seen and (next(iter(self.seen.values())) < now - 300 or len(self.seen) >= 4096):
            self.seen.popitem(last=False)
        if key in self.seen:
            return
        self.seen[key] = now
        parts = data.split(":")
        dialog = self.dialogs.get(parts[1]) if len(parts) == 4 else None
        code, action = 1, None
        if dialog and not dialog.expired():
            scene = getattr(interaction, "scene", None)
            peer = (
                getattr(interaction, "group_openid", None)
                if scene == "group"
                else getattr(interaction, "user_openid", None)
            )
            sender = (
                getattr(interaction, "group_member_openid", None)
                if scene == "group"
                else getattr(interaction, "user_openid", None)
            )
            if (
                platform_id != dialog.platform_id
                or scene != dialog.scene
                or peer != dialog.peer_id
                or sender != dialog.owner
            ):
                code = 4
            elif getattr(interaction, "type", None) != 11:
                code = 1
            elif parts[2] != str(dialog.revision) or dialog.busy:
                code = 3
            elif parts[3] in dialog.actions:
                code, action = 0, dialog.actions[parts[3]]
                dialog.busy = True  # reserve before the ACK yields control
        try:
            async with asyncio.timeout(3):
                await api.on_interaction_result(interaction_id, code)
        except Exception as exc:
            if action is not None:
                dialog.busy = False
            self.logger.warning("QQ keyboard 回调确认失败：%s", type(exc).__name__)
            return
        if action is not None:
            await dialog.process(action, dialog.event, interaction=interaction, claimed=True)

    @staticmethod
    def _detach(hook):
        hook.active = False
        if getattr(hook.client, "on_interaction_create", None) is hook.wrapper:
            if hook.previous is None:
                delattr(hook.client, "on_interaction_create")
            else:
                hook.client.on_interaction_create = hook.previous
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
                await self.finish(event, confirmation, interaction=interaction)
                return
            await self.present(event, candidate, interaction=interaction)
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

    async def finish(self, event, text, *, interaction=None):
        # Persisting a binding has already succeeded; a failed confirmation must
        # not allow an old button to repeat or replace that committed operation.
        self.closed = True
        try:
            await self.send(event, text, interaction=interaction)
        finally:
            self.controller.stop()
            self.retirer.schedule(self.receipt)
            self.receipt = None

    def close(self):
        self.closed = True
        self.controller.stop()
        self.bridge.dialogs.pop(self.token, None)
        self.retirer.schedule(self.receipt)
        self.receipt = None
