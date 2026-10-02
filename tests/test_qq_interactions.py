import asyncio
import copy
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_astrbot import SessionAgent, make_qq_incoming, plugin_module
from test_astrbot import plugin as plugin

from szu_electricity.conversation import ReuseSelection, Selection
from szu_electricity.presentation import choice_card
from szu_electricity.qq_interactions import INTERACTION_INTENT, KeyboardBridge
from szu_electricity.qq_messages import MessageRetirer, SentMessage


class API:
    def __init__(self):
        self.events = []
        self.posts = []
        self._http = self
        self.count = 0
        self.posted = asyncio.Event()
        self.reject_keyboard = False
        self.fail_send = False

    async def post_group_message(self, **payload):
        return await self.post("group", payload)

    async def post_c2c_message(self, **payload):
        return await self.post("c2c", payload)

    async def post(self, scene, payload):
        import botpy.errors

        if self.reject_keyboard and "keyboard" in payload:
            raise botpy.errors.ForbiddenError("keyboard permission denied")
        if self.fail_send:
            raise OSError("offline")
        self.count += 1
        message_id = f"bot-{self.count}"
        self.posts.append(copy.deepcopy(payload))
        self.events.append(("post", message_id, scene))
        self.posted.set()
        return {"id": message_id, "ext_info": {"ref_idx": "NOT_A_DELETE_ID"}}

    async def request(self, route, **kwargs):
        self.events.append(("delete", route.url, route.method))
        return None

    async def on_interaction_result(self, interaction_id, code):
        self.events.append(("ack", interaction_id, code))


def interaction(data, *, id="click-1", scene="group", user="member-openid", peer="group-openid"):
    from botpy.interaction import Interaction

    return Interaction(
        None,
        "OUTER-" + id,
        {
            "id": id,
            "type": 11,
            "scene": scene,
            "group_member_openid": user if scene == "group" else None,
            "group_openid": peer if scene == "group" else None,
            "user_openid": user if scene == "c2c" else None,
            "data": {"resolved": {"button_data": data}},
        },
    )


def button(payload, label):
    return next(
        b
        for r in payload["keyboard"]["content"]["rows"]
        for b in r["buttons"]
        if b["render_data"]["label"] == label
    )


async def setup_dialog(plugin, monkeypatch, *, private=False, reject_keyboard=False):
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    api = API()
    api.reject_keyboard = reject_keyboard
    event = await make_qq_incoming(api, "private" if private else "group")
    event.message_str = "绑定宿舍"
    client = SimpleNamespace(api=api, intents=0, _connection=None)
    adapter = SimpleNamespace(client=client, meta=lambda: event.platform_meta)
    plugin.context.get_platform_inst = lambda _: adapter
    plugin.keyboard.attach(adapter)
    task = asyncio.create_task(plugin.bind_dorm(event))
    async with asyncio.timeout(2):
        await api.posted.wait()
    return api, event, client, task


async def click(client, api, label, index, *, private=False):
    b = button(api.posts[-1], label)
    await client.on_interaction_create(
        interaction(
            b["action"]["data"],
            id=f"click-{index}",
            scene="c2c" if private else "group",
            user="user-openid" if private else "member-openid",
            peer="group-openid",
        )
    )


async def test_single_click_binding_and_ordered_recalls(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    first = api.posts[0]
    assert first["msg_type"] == 2 and "content" not in first
    assert first["markdown"]["content"].startswith('<qqbot-at-user id="member-openid" />')
    old_data = first["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
    await client.on_interaction_create(interaction(old_data, user="other-member"))
    assert api.events[-1] == ("ack", "click-1", 4)
    assert len(api.posts) == 1
    await client.on_interaction_create(interaction(old_data, id="valid-1"))
    await plugin.retirer.drain()
    assert len(api.posts) == 2
    assert api.events.index(("post", "bot-2", "group")) < next(
        i for i, e in enumerate(api.events) if e[0] == "delete"
    )
    assert api.posts[-1]["event_id"] == "OUTER-valid-1" and "msg_id" not in api.posts[-1]
    snapshot = list(api.events)
    await client.on_interaction_create(interaction(old_data, id="valid-1"))
    assert api.events == snapshot  # same interaction is acknowledged once
    await client.on_interaction_create(interaction(old_data, id="stale"))
    assert api.events[-1] == ("ack", "stale", 3)
    await click(client, api, "粤海校区宿舍", 2)
    await click(client, api, "山茶斋", 3)
    assert "宿舍号" in api.posts[-1]["markdown"]["content"]
    response = await make_qq_incoming(api)
    response.message_str = "0601"
    await SessionAgent(plugin.context).handle_session_control_agent(response)
    await task
    await plugin.retirer.drain()
    saved = await plugin.store.get_binding(*plugin._identity(event))
    assert saved.location.roomName == "0601"
    deleted = [e[1] for e in api.events if e[0] == "delete"]
    assert len(deleted) == 4
    assert all("/v2/groups/group-openid/messages/bot-" in url for url in deleted)
    assert all("NOT_A_DELETE_ID" not in url and "ROBOT-command-token" not in url for url in deleted)
    assert not any(url.endswith("/bot-5") for url in deleted)  # keep final success


async def test_private_callback_cancel_and_recall(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch, private=True)
    await click(client, api, "取消", 1, private=True)
    await task
    await plugin.retirer.drain()
    assert await plugin.store.get_binding(*plugin._identity(event)) is None
    assert any(
        e[0] == "delete" and "/v2/users/user-openid/messages/bot-1" in e[1] for e in api.events
    )
    assert all("qqbot-at-user" not in p.get("markdown", {}).get("content", "") for p in api.posts)


async def test_keyboard_rejection_falls_back_to_typed_selection(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch, reject_keyboard=True)
    assert "keyboard" not in api.posts[0] and "markdown" in api.posts[0]
    for text in ["1", "1", "1", "0601"]:
        response = await make_qq_incoming(api)
        response.message_str = text
        await SessionAgent(plugin.context).handle_session_control_agent(response)
    await task
    assert (await plugin.store.get_binding(*plugin._identity(event))).location.roomName == "0601"
    assert all("keyboard" not in p for p in api.posts)
    await plugin.retirer.drain()


async def test_failed_next_card_keeps_old_state_and_never_reuses_revision(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    dialog = next(iter(plugin.keyboard.dialogs.values()))
    old = dialog.receipt
    data = api.posts[-1]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
    api.fail_send = True
    await client.on_interaction_create(interaction(data))
    assert dialog.receipt is old and dialog.selection.step == 0
    assert not any(e[0] == "delete" for e in api.events)
    api.fail_send = False
    await client.on_interaction_create(interaction(data, id="retry"))
    assert dialog.selection.step == 1 and dialog.revision >= 4
    assert dialog.receipt.message_id != old.message_id
    await click(client, api, "取消", 3)
    await task
    await plugin.retirer.drain()


async def test_expired_buttons_do_not_modify_binding(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    dialog = next(iter(plugin.keyboard.dialogs.values()))
    data = api.posts[-1]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
    dialog.deadline = 0
    await client.on_interaction_create(interaction(data))
    assert api.events[-1] == ("ack", "click-1", 1)
    assert dialog.selection.step == 0
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await plugin.retirer.drain()


async def test_bridge_preserves_handlers_and_requests_reconnect_only_when_needed():
    api = API()
    forwarded = []

    async def previous(event):
        forwarded.append(event)

    client = SimpleNamespace(
        api=api, intents=0, _connection=object(), on_interaction_create=previous
    )
    adapter = SimpleNamespace(
        client=client, meta=lambda: SimpleNamespace(id="qq-main", name="qq_official")
    )
    bridge = KeyboardBridge(SimpleNamespace(), logging.getLogger())
    bridge.attach(adapter)
    assert client.intents & INTERACTION_INTENT
    assert not bridge.hooks["qq-main"].ready
    event = interaction("another-plugin:data")
    await client.on_interaction_create(event)
    assert forwarded == [event] and api.events == []
    await bridge.close()
    assert client.on_interaction_create is previous
    reloaded = KeyboardBridge(SimpleNamespace(), logging.getLogger())
    reloaded.attach(adapter)
    assert not reloaded.hooks["qq-main"].ready
    await reloaded.close()
    new_client = SimpleNamespace(api=api, intents=0, _connection=None)
    adapter.client = new_client
    ready = KeyboardBridge(SimpleNamespace(), logging.getLogger())
    ready.attach(adapter)
    assert ready.hooks["qq-main"].ready
    await ready.close()
    assert not hasattr(new_client, "on_interaction_create")


@pytest.mark.parametrize("scene,path", [("group", "groups"), ("c2c", "users")])
async def test_recall_limit_and_empty_success(scene, path):
    api = API()
    now = [100.0]

    async def sleep(delay):
        now[0] += delay

    retirer = MessageRetirer(logging.getLogger(), clock=lambda: now[0], sleep=sleep)
    receipt = SentMessage("qq-main", scene, "peer", "bot-message", 0, api)
    retirer.schedule(receipt)
    retirer.schedule(receipt)
    await retirer.drain()
    assert len(api.events) == 1 and f"/v2/{path}/peer/messages/bot-message" in api.events[0][1]
    now[0] = 120
    retirer.schedule(replace(receipt, message_id="expired"))
    await retirer.drain()
    assert len(api.events) == 1
    await retirer.close()


def test_keyboard_limits_full_labels_and_permission(catalog, location):
    selection = Selection(catalog)
    selection.accept("1")
    selection.accept("1")
    view, actions = choice_card(selection, "nonce", 7, "member", keyboard=True)
    rows = view.keyboard["content"]["rows"]
    assert len(rows) <= 5 and all(len(r["buttons"]) <= 5 for r in rows)
    for row in rows:
        for b in row["buttons"]:
            assert len(b["render_data"]["label"]) <= 10
            assert b["action"]["type"] == 1
            assert b["action"]["permission"] == {"type": 0, "specify_user_ids": ["member"]}
            assert b["action"]["data"].startswith("szuh:nonce:7:")
    reuse = ReuseSelection([location])
    view, _ = choice_card(reuse, "nonce", 1, "member", keyboard=True)
    assert "复用已有绑定" in view.markdown


async def test_markdown_table_fallback_and_transport_errors(plugin, monkeypatch, location):
    from datetime import datetime

    import botpy.errors

    from szu_electricity.analytics import summarize
    from szu_electricity.models import SHANGHAI, ProviderResult, Window

    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    calls = []

    class LayoutAPI(API):
        async def post_group_message(self, **payload):
            calls.append(copy.deepcopy(payload))
            if "markdown" in payload and "| 指标 |" in payload["markdown"]["content"]:
                raise botpy.errors.ServerError("markdown table rejected")
            return await super().post_group_message(**payload)

    api = LayoutAPI()
    event = await make_qq_incoming(api)
    report = summarize(
        location, "iotun", Window.for_days(3), ProviderResult([], 88, datetime.now(SHANGHAI))
    )
    # Use the plugin's view class because the plugin is loaded as a namespace package.
    view = plugin_module.detail_view(report)
    receipt = await plugin._reply(event, view)
    assert receipt.message_id and len(calls) == 2
    assert calls[0]["msg_type"] == calls[1]["msg_type"] == 2
    assert "content" not in calls[0] and "content" not in calls[1]
    assert "**当前电量**" in calls[1]["markdown"]["content"]
    calls.clear()

    async def unavailable(**payload):
        calls.append(payload)
        raise OSError("connection reset")

    api.post_group_message = unavailable
    with pytest.raises(OSError):
        await plugin._reply(event, view)
    assert len(calls) == 1


async def test_real_sdk_dispatches_keyboard_callback(plugin, monkeypatch):
    import botpy
    from botpy.connection import ConnectionState

    api, event, _, task = await setup_dialog(plugin, monkeypatch)
    # Replace the test client with the real botpy event dispatcher, without opening a socket.
    client = object.__new__(botpy.Client)
    client.loop = asyncio.get_running_loop()
    client.intents = INTERACTION_INTENT
    client._connection = None
    client.api = api
    adapter = SimpleNamespace(client=client, meta=lambda: event.platform_meta)
    # Keep this live dialog for testing dispatch rather than platform-reload cancellation.
    hook = plugin.keyboard.hooks.pop("qq-main")
    plugin.keyboard._detach(hook)
    plugin.keyboard.attach(adapter)
    data = api.posts[-1]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
    api.posted.clear()
    state = ConnectionState(client.ws_dispatch, api)
    state.parse_interaction_create(
        {
            "id": "OUTER-sdk-click",
            "d": {
                "id": "sdk-click",
                "type": 11,
                "scene": "group",
                "group_openid": "group-openid",
                "group_member_openid": "member-openid",
                "data": {"resolved": {"button_data": data}},
            },
        }
    )
    async with asyncio.timeout(2):
        await api.posted.wait()
    assert ("ack", "sdk-click", 0) in api.events
    assert api.posts[-1]["event_id"] == "OUTER-sdk-click"
    await click(client, api, "取消", 99)
    await task
    await plugin.retirer.drain()


async def test_buttons_page_back_reuse_and_busy_guard(plugin, monkeypatch, location):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    await client.on_interaction_create(
        interaction(
            api.posts[-1]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"],
            id="campus",
        )
    )
    await click(client, api, "粤海校区宿舍", 2)
    await click(client, api, "下一页", 3)
    assert "第 2/2 页" in api.posts[-1]["markdown"]["content"]
    await click(client, api, "上一页", 4)
    await click(client, api, "返回", 5)
    assert "宿舍区域" in api.posts[-1]["markdown"]["content"]
    dialog = next(iter(plugin.keyboard.dialogs.values()))
    dialog.busy = True
    current = api.posts[-1]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
    await client.on_interaction_create(interaction(current, id="busy"))
    assert api.events[-1] == ("ack", "busy", 3)
    dialog.busy = False
    await click(client, api, "取消", 6)
    await task
    await plugin.store.bind(
        "qq-main",
        "qq-main:GroupMessage:another-group",
        "member-openid",
        "Owner",
        True,
        "qq_official",
        location,
    )
    api.posted.clear()
    event = await make_qq_incoming(api)
    event.message_str = "绑定宿舍"
    task = asyncio.create_task(plugin.bind_dorm(event))
    async with asyncio.timeout(2):
        await api.posted.wait()
    assert "复用" in api.posts[-1]["markdown"]["content"]
    await click(client, api, "复用已有绑定", 7)
    await task
    assert (
        await plugin.store.get_binding(*plugin._identity(event))
    ).location.roomName == location.roomName
    await plugin.retirer.drain()


async def test_callback_cannot_cross_peer_or_platform(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    data = api.posts[-1]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
    await client.on_interaction_create(interaction(data, id="other-peer", peer="different-group"))
    assert api.events[-1] == ("ack", "other-peer", 4)
    await plugin.keyboard.handle("another-platform", api, interaction(data, id="other-platform"))
    assert api.events[-1] == ("ack", "other-platform", 4)
    assert next(iter(plugin.keyboard.dialogs.values())).selection.step == 0
    await click(client, api, "取消", 5)
    await task
    await plugin.retirer.drain()


async def test_recall_failure_does_not_break_flow_and_rate_limit_is_per_platform():
    api = API()
    now = [100.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    async def failed_delete(route, **kwargs):
        raise PermissionError("denied")

    api._http = SimpleNamespace(request=failed_delete)
    retirer = MessageRetirer(logging.getLogger(), clock=lambda: now[0], sleep=sleep)
    for i in range(3):
        retirer.schedule(SentMessage("qq-main", "group", "peer", f"bot-{i}", 100, api))
    await retirer.drain()
    assert len(sleeps) == 2 and all(delay == 0.125 for delay in sleeps)
    await retirer.close()


async def test_markdown_can_fall_back_to_plain_and_receipt_requires_message_id(plugin, monkeypatch):
    import botpy.errors

    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    api = API()
    calls = []

    async def rejected(**payload):
        calls.append(copy.deepcopy(payload))
        if "markdown" in payload:
            raise botpy.errors.ForbiddenError("markdown not allowed")
        return {"id": "plain-result"}

    api.post_group_message = rejected
    event = await make_qq_incoming(api)
    view = plugin_module.MessageView("完整正文", "## 标题", fallback_markdown="- 简单列表")
    receipt = await plugin._reply(event, view)
    assert len(calls) == 3 and receipt.message_id == "plain-result"
    assert calls[-1]["msg_type"] == 0 and "markdown" not in calls[-1]
    assert calls[-1]["content"].startswith('<qqbot-at-user id="member-openid" />')
    assert "message_reference" in calls[-1]

    async def no_id(**payload):
        return {"ext_info": {"ref_idx": "not-a-message-id"}}

    api.post_group_message = no_id
    with pytest.raises(plugin_module.ElectricityError, match="消息 ID"):
        await plugin._reply(event, "正文")


async def test_detail_command_uses_configured_markdown_layout(plugin, monkeypatch, location):
    from datetime import datetime

    from szu_electricity.analytics import summarize
    from szu_electricity.models import SHANGHAI, ProviderResult, Window

    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    api = API()
    event = await make_qq_incoming(api)
    await plugin.store.bind(*plugin._identity(event), "Owner", True, "qq_official", location)

    async def query(location, detail=False):
        return summarize(
            location,
            "iotun",
            Window.for_days(31 if detail else 3),
            ProviderResult([], 88, datetime.now(SHANGHAI)),
        )

    plugin.service.query = query
    event.message_str = "用电 详情"
    await plugin.electricity(event)
    assert api.posts[-1]["msg_type"] == 2
    assert "| 指标 | 数值 |" in api.posts[-1]["markdown"]["content"]
    assert "content" not in api.posts[-1]
    plugin.config["detail_layout"] = "list"
    await plugin.electricity(event)
    assert "**当前电量**" in api.posts[-1]["markdown"]["content"]
    assert "| 指标 |" not in api.posts[-1]["markdown"]["content"]
    event.message_str = "用电"
    await plugin.electricity(event)
    assert api.posts[-1]["msg_type"] == 0 and "markdown" not in api.posts[-1]


async def test_unload_restores_callback_and_invalidates_pending_buttons(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    assert plugin.keyboard.dialogs and hasattr(client, "on_interaction_create")
    await plugin.terminate()
    await asyncio.gather(task, return_exceptions=True)
    assert not plugin.keyboard.dialogs
    assert not hasattr(client, "on_interaction_create")
    assert not plugin.retirer._tasks
