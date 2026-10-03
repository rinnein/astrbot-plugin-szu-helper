"""QQ text-chain, Markdown, keyboard and recall support through the QQ SDK."""

import asyncio
import html
import random
import re
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import ElectricityError


def mention(user_id: str) -> str:
    return f'<qqbot-at-user id="{html.escape(str(user_id), quote=True)}" />'


def escape_markdown(text: str) -> str:
    text = html.escape(text, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def own_reference(source) -> str | None:
    """Only extract this message's msg_idx; never copy auth_token/ref_msg_idx."""
    data = getattr(source, "raw_data", source)
    scene = (
        data.get("message_scene")
        if isinstance(data, Mapping)
        else getattr(source, "message_scene", None)
    )
    ext = scene.get("ext", []) if isinstance(scene, Mapping) else getattr(scene, "ext", [])
    if isinstance(ext, (list, tuple)):
        for item in ext:
            if isinstance(item, str):
                key, separator, value = item.partition("=")
                if key == "msg_idx" and separator and value:
                    return value
    return None


@dataclass(frozen=True)
class MessageView:
    text: str
    markdown: str | None = None
    keyboard: dict | None = None
    fallback_markdown: str | None = None


@dataclass(frozen=True)
class SentMessage:
    platform_id: str
    scene: str
    peer_id: str
    message_id: str
    sent_at: float
    api: object
    keyboard: bool = False


def _error_code(exc) -> int | None:
    value = getattr(exc, "msgs", None)
    if isinstance(value, Mapping):
        value = value.get("code")
        return value if isinstance(value, int) else None
    match = re.search(r'(?:["\']?code["\']?\s*[:=]\s*|错误码\s*[:：]\s*)(\d+)', str(exc), re.I)
    return int(match.group(1)) if match else None


def _rejected_feature(exc, feature: str) -> bool:
    # Only explicit protocol rejections justify changing the payload. Transport
    # failures may have delivered it already and must not trigger duplicate sends.
    if not type(exc).__module__.startswith("botpy.errors"):
        return False
    code = _error_code(exc)
    text = str(exc).lower()
    if feature == "keyboard":
        return code in {305007, 40034029} or any(
            word in text for word in ("keyboard", "button", "按钮", "键盘")
        )
    if feature == "markdown":
        return (
            code in {304036, 304061, 40034010, 40034011, 40034124, 40034127}
            or "markdown" in text
            or "qqbot-cmd" in text
        )
    if feature == "trigger":
        return code in {304103, 40034005, 40034024, 40034025, 40034026, 40034128} or (
            ("msg_id" in text or "event_id" in text)
            and any(word in text for word in ("expired", "过期", "无效", "越权"))
        )
    return False


class QQSendError(ElectricityError):
    """A delivery failed; sending another error reply could duplicate delivery."""


def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def correlation(value) -> str:
    # Stable within this process; no raw user/event/platform IDs in logs.
    import hashlib

    return hashlib.sha256(str(value).encode()).hexdigest()[:10]


async def send_message(
    api,
    platform_id,
    scene,
    peer,
    view,
    logger,
    *,
    recipients=(),
    require_markdown=False,
    reference=None,
    msg_id=None,
    event_id=None,
    sequence=None,
) -> SentMessage:
    """Send through the SDK without AstrBot's implicit plaintext fallback."""
    view = MessageView(view) if isinstance(view, str) else view
    if not peer:
        raise QQSendError("QQ 官方发送缺少目标会话。")
    targets = {
        "group": ("post_group_message", "group_openid"),
        "c2c": ("post_c2c_message", "openid"),
        "channel": ("post_message", "channel_id"),
        "dm": ("post_dms", "guild_id"),
    }
    method, target = targets[scene]
    send = getattr(api, method)
    recipients = tuple(dict.fromkeys(str(user) for user in recipients if user))
    prefix = "".join(mention(user) + "\n" for user in recipients)
    require_markdown = require_markdown or bool(recipients) or view.markdown is not None
    payload = {target: peer}
    if require_markdown:
        payload["markdown"] = {
            "content": prefix
            + (view.markdown if view.markdown is not None else escape_markdown(view.text))
        }
    else:
        payload["content"] = html.escape(view.text, quote=False)
    if view.keyboard and require_markdown:
        payload["keyboard"] = view.keyboard
    if reference:
        payload["message_reference"] = {"message_id": str(reference)}
    if event_id:
        payload["event_id"] = event_id
    elif msg_id:
        payload["msg_id"] = str(msg_id)
    if scene in {"group", "c2c"}:
        payload["msg_type"] = 2 if require_markdown else 0
        payload["msg_seq"] = sequence or random.randint(1, 10000)

    # Layout fallback stays Markdown, including the final minimally formatted body.
    alternatives = list(
        dict.fromkeys(item for item in (view.fallback_markdown, escape_markdown(view.text)) if item)
    )
    for _ in range(6):
        try:
            sent_at = time.monotonic()
            result = await send(**payload)
            break
        except Exception as exc:
            code = _error_code(exc)
            if _rejected_feature(exc, "trigger") and ("msg_id" in payload or "event_id" in payload):
                payload.pop("msg_id", None)
                payload.pop("event_id", None)
                logger.warning("QQ 被动回复标识失效，改用主动发送，保留引用 code=%s", code)
            elif "keyboard" in payload and _rejected_feature(exc, "keyboard"):
                payload.pop("keyboard")
                logger.warning("QQ keyboard 被拒绝，降级为 Markdown 编号选择 code=%s", code)
            elif "markdown" in payload and _rejected_feature(exc, "markdown"):
                current = payload["markdown"]["content"]
                while alternatives and prefix + alternatives[0] == current:
                    alternatives.pop(0)
                if not alternatives:
                    logger.warning("QQ Markdown 被拒绝，停止发送，不降级纯文本 code=%s", code)
                    raise QQSendError(
                        "QQ Markdown 发送被拒绝，未发送；请检查 Markdown 权限及插件日志。"
                    ) from exc
                payload["markdown"] = {"content": prefix + alternatives.pop(0)}
                logger.warning("QQ Markdown 排版被拒绝，尝试简化 Markdown code=%s", code)
            else:
                logger.warning(
                    "QQ 消息发送失败 scene=%s error=%s code=%s", scene, type(exc).__name__, code
                )
                raise QQSendError("QQ 消息发送失败，请查看插件日志中的错误类别及错误码。") from exc
    else:
        raise QQSendError("QQ 消息降级发送未成功。")
    message_id = field(result, "id")
    if not message_id:
        raise QQSendError("QQ 发送接口未返回消息 ID，无法确认消息发送成功。")
    logger.debug(
        "QQ 消息已发送 trace=%s scene=%s quoted=%s markdown=%s keyboard=%s recipients=%s",
        correlation(message_id),
        scene,
        bool(reference),
        "markdown" in payload,
        "keyboard" in payload,
        len(recipients),
    )
    return SentMessage(
        str(platform_id), scene, str(peer), str(message_id), sent_at, api, "keyboard" in payload
    )


async def reply(event, text: str | MessageView, logger, *, interaction=None) -> SentMessage:
    import botpy.message
    from astrbot.api.event import AstrMessageEvent, MessageChain
    from astrbot.api.message_components import Plain

    source = getattr(event.message_obj, "raw_message", None)
    if isinstance(source, botpy.message.GroupMessage):
        scene, peer, reference = "group", source.group_openid, own_reference(source)
    elif isinstance(source, botpy.message.C2CMessage):
        scene, peer, reference = "c2c", source.author.user_openid, own_reference(source)
    elif isinstance(source, botpy.message.Message):
        scene, peer = "channel", source.channel_id
        reference = getattr(event.message_obj, "message_id", None)
    elif isinstance(source, botpy.message.DirectMessage):
        scene, peer = "dm", source.guild_id
        reference = getattr(event.message_obj, "message_id", None)
    else:
        raise QQSendError("QQ 官方回复缺少可识别的原消息上下文。")
    sequence = event.get_extra("szu_qq_reply_sequence")
    sequence = random.randint(1, 10000) if sequence is None else sequence % 10000 + 1
    event.set_extra("szu_qq_reply_sequence", sequence)
    if not reference:
        logger.debug("QQ 回复缺少引用索引 scene=%s", scene)
    receipt = await send_message(
        event.bot.api,
        event.get_platform_id(),
        scene,
        peer,
        text,
        logger,
        recipients=(event.get_sender_id(),) if scene in {"group", "channel"} else (),
        reference=reference,
        sequence=sequence,
        event_id=field(interaction, "event_id") if interaction is not None else None,
        msg_id=getattr(event.message_obj, "message_id", None) if interaction is None else None,
    )
    view = MessageView(text) if isinstance(text, str) else text
    await AstrMessageEvent.send(event, MessageChain([Plain(view.text)]))
    return receipt


async def notify(adapter, binding, recipients, text, logger) -> SentMessage:
    from astrbot.core.platform.message_session import MessageSession

    session_id = MessageSession.from_str(binding.origin).session_id
    if binding.is_group:
        session_id = session_id.rsplit("_", 1)[-1]
        scene = getattr(adapter, "_session_scene", {}).get(session_id)
        if scene not in {"group", "channel"}:
            raise QQSendError(
                "QQ 官方机器人的目标会话上下文缺失，请先在该群或频道给机器人发送一条消息后重试。"
            )
        msg_id = getattr(adapter, "_session_last_message_id", {}).get(session_id)
        proactive = scene == "group" and getattr(adapter, "_allow_group_proactive_send", False)
        if not msg_id and not proactive:
            raise QQSendError(
                "QQ 官方机器人的目标会话上下文缺失，请先在该群或频道给机器人发送一条消息后重试。"
            )
        if proactive:
            msg_id = None
    else:
        scene, msg_id, recipients = "c2c", None, ()
    return await send_message(
        adapter.client.api,
        binding.platform_id,
        scene,
        session_id,
        text,
        logger,
        recipients=recipients,
        require_markdown=True,
        msg_id=msg_id,
    )


@dataclass(frozen=True)
class UserSelectionMessage:
    """A validated incoming selection, never a bot receipt or command-supplied ID."""

    platform_id: str
    peer_id: str
    message_id: str
    sent_at: float
    api: object
    scene: str = "group"


class MessageRetirer:
    """Recall bot menus and validated group selections within QQ's time limit."""

    def __init__(self, logger, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.logger, self.clock, self.sleep = logger, clock, sleep
        self._locks = defaultdict(asyncio.Lock)
        self._last = {}
        self._pending = set()
        self._attempted = {}
        self._tasks = set()

    def schedule_selection(self, event, initial_event):
        import botpy.message

        if event.get_platform_name() != "qq_official" or event is initial_event:
            return
        if (event.get_platform_id(), event.unified_msg_origin, event.get_sender_id()) != (
            initial_event.get_platform_id(),
            initial_event.unified_msg_origin,
            initial_event.get_sender_id(),
        ):
            return
        source = getattr(event.message_obj, "raw_message", None)
        initial = getattr(initial_event.message_obj, "raw_message", None)
        if not isinstance(source, botpy.message.GroupMessage):
            return
        raw = getattr(source, "raw_data", source)
        message_id, peer = field(raw, "id"), field(raw, "group_openid")
        author = field(raw, "author")
        sender = field(author, "member_openid")
        if (
            not message_id
            or not peer
            or sender != event.get_sender_id()
            or peer != event.get_group_id()
        ):
            return
        if message_id == field(getattr(initial, "raw_data", initial), "id"):
            return
        timestamp = field(raw, "timestamp")
        try:
            timestamp = (
                datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
            )
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                raise ValueError("missing timestamp")
            age = (datetime.now(UTC) - timestamp).total_seconds()
        except (ValueError, TypeError, OverflowError):
            self.logger.debug("跳过用户选择消息撤回 reason=missing_timestamp")
            return
        if not 0 <= age < 120:
            self.logger.debug("跳过用户选择消息撤回 reason=expired_or_future")
            return
        self.schedule(
            UserSelectionMessage(
                event.get_platform_id(),
                str(peer),
                str(message_id),
                self.clock() - age,
                event.bot.api,
            )
        )

    def schedule(self, receipt: SentMessage | UserSelectionMessage | None):
        if receipt is None or receipt.scene not in {"group", "c2c"}:
            return
        key = (receipt.platform_id, receipt.scene, receipt.peer_id, receipt.message_id)
        now = self.clock()
        self._attempted = {key: at for key, at in self._attempted.items() if now - at < 120}
        if key in self._pending or key in self._attempted:
            return
        self._attempted[key] = now
        self._pending.add(key)
        task = asyncio.create_task(self._recall(receipt))
        self._tasks.add(task)

        def done(task):
            self._pending.discard(key)
            self._tasks.discard(task)
            if not task.cancelled():
                task.exception()

        task.add_done_callback(done)

    async def _recall(self, receipt: SentMessage | UserSelectionMessage):
        from botpy.http import Route

        kind = "user_selection" if isinstance(receipt, UserSelectionMessage) else "bot_menu"
        try:
            async with self._locks[receipt.platform_id]:
                delay = 0.125 - (self.clock() - self._last.get(receipt.platform_id, float("-inf")))
                if delay > 0:
                    await self.sleep(delay)
                if not 0 <= self.clock() - receipt.sent_at < 120:
                    self.logger.debug("跳过超过撤回时限的消息 kind=%s", kind)
                    return
                self._last[receipt.platform_id] = self.clock()
                path = (
                    "/v2/groups/{peer}/messages/{message}"
                    if receipt.scene == "group"
                    else "/v2/users/{peer}/messages/{message}"
                )
                async with asyncio.timeout(5):
                    # QQ DELETE returns no body on success; None is successful here.
                    await receipt.api._http.request(
                        Route("DELETE", path, peer=receipt.peer_id, message=receipt.message_id)
                    )
                self.logger.debug(
                    "选项消息已撤回 kind=%s scene=%s trace=%s",
                    kind,
                    receipt.scene,
                    correlation(receipt.message_id),
                )
        except Exception as exc:
            self.logger.warning(
                "选项消息撤回失败，不影响绑定流程 kind=%s error=%s code=%s",
                kind,
                type(exc).__name__,
                _error_code(exc),
            )

    async def drain(self):
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self):
        for task in list(self._tasks):
            task.cancel()
        await self.drain()
