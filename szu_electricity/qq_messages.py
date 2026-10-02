"""QQ Official text delivery without losing mentions or message references.

AstrBot 4.28.2 ignores generic At/Reply components in its QQ converter. Use
QQ's content markup and message_reference fields for this plugin's text replies.
"""

import html
import random
from collections.abc import Mapping

from .models import ElectricityError


def mention(user_id: str) -> str:
    # IDs come from the adapter's member_openid/user ID, never from a nickname.
    return f"<@{html.escape(str(user_id), quote=True)}>"


def own_reference(source) -> str | None:
    """Read this incoming message's quote index, not the message it quotes.

    message_scene.ext can also contain auth_token; never forward or log it.
    """
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


async def reply(event, text: str, logger) -> None:
    import botpy.message
    from astrbot.api.event import AstrMessageEvent, MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )

    source = getattr(event.message_obj, "raw_message", None)
    api = event.bot.api
    body = html.escape(text, quote=False)
    if event.get_group_id() and event.get_sender_id():
        body = mention(event.get_sender_id()) + "\n" + body
    payload = {"content": body}
    message_id = getattr(event.message_obj, "message_id", None)
    if message_id:
        payload["msg_id"] = str(message_id)

    if isinstance(source, (botpy.message.GroupMessage, botpy.message.C2CMessage)):
        reference = own_reference(source)
        payload["msg_type"] = 0  # Native mentions are content markup, not Markdown.
        sequence = event.get_extra("szu_qq_reply_sequence")
        sequence = random.randint(1, 10000) if sequence is None else sequence % 10000 + 1
        event.set_extra("szu_qq_reply_sequence", sequence)
        payload["msg_seq"] = sequence
        if isinstance(source, botpy.message.GroupMessage):

            async def send(parameters):
                return await api.post_group_message(group_openid=source.group_openid, **parameters)
        else:

            async def send(parameters):
                return await api.post_c2c_message(openid=source.author.user_openid, **parameters)
    elif isinstance(source, botpy.message.Message):
        reference = str(message_id) if message_id else None

        async def send(parameters):
            return await api.post_message(channel_id=source.channel_id, **parameters)
    elif isinstance(source, botpy.message.DirectMessage):
        reference = str(message_id) if message_id else None

        async def send(parameters):
            return await api.post_dms(guild_id=source.guild_id, **parameters)
    else:
        raise ElectricityError("QQ 官方回复缺少可识别的原消息上下文。")

    if reference:
        payload["message_reference"] = {"message_id": reference}
    else:
        logger.debug("QQ 回复缺少当前消息的引用索引，保留提及并发送正文")

    # Preserve AstrBot's existing fallback for expired passive-reply tokens.
    # It does not discard the independent quote reference or native mention.
    fallback = getattr(QQOfficialMessageEvent, "_send_with_markdown_fallback", None)
    if callable(fallback):
        await fallback(send_func=send, payload=payload, plain_text=body)
    else:
        await send(payload)
    await AstrMessageEvent.send(event, MessageChain([Plain(body)]))
    logger.debug(
        "QQ 命令回复已发送 quoted=%s mention=%s", bool(reference), bool(event.get_group_id())
    )
