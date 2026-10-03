"""Integration tests with real AstrBot classes; network delivery is captured."""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

_runtime = tempfile.TemporaryDirectory(prefix="szu-astrbot-test-")
os.environ["ASTRBOT_ROOT"] = _runtime.name
pytest.importorskip("astrbot")
root = Path(__file__).resolve().parents[1]
package = types.ModuleType("szu_integration_plugin")
package.__path__ = [str(root)]
sys.modules[package.__name__] = package
plugin_module = importlib.import_module("szu_integration_plugin.main")

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType, PlatformMetadata

try:
    from astrbot.builtin_stars.session_controller.main import Main as SessionAgent
except ModuleNotFoundError:
    from astrbot.builtin_stars.astrbot.main import Main as SessionAgent
from astrbot.core.utils.session_waiter import USER_SESSIONS

from szu_electricity.analytics import summarize
from szu_electricity.models import SHANGHAI, Binding, ProviderResult, Window
from szu_electricity.sharing import encode


def platform_stub(platform_id, name="aiocqhttp", proactive=True):
    metadata = PlatformMetadata(name, "Test", id=platform_id, support_proactive_message=proactive)
    return SimpleNamespace(meta=lambda: metadata)


class Event(AstrMessageEvent):
    def __init__(self, text, sender="1", group="group1", platform="testbot"):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        msg.sender = MessageMember(user_id=sender, nickname="User" + sender)
        msg.group_id = group
        msg.message = []
        msg.message_id = "incoming-message-id"
        super().__init__(
            text, msg, PlatformMetadata("aiocqhttp", "Test", id=platform), group or sender
        )
        self.sent = []
        self.is_at_or_wake_command = True

    async def send(self, message):
        self.sent.append(message)

    def output(self):
        return "\n".join(
            c.text for m in self.sent for c in m.chain if isinstance(c, plugin_module.Plain)
        )


@pytest.fixture
async def plugin(tmp_path, monkeypatch, catalog):
    monkeypatch.setattr(plugin_module, "get_astrbot_data_path", lambda: str(tmp_path))
    config = AstrBotConfig(
        str(tmp_path / "config.json"), schema=json.loads((root / "_conf_schema.json").read_text())
    )
    context = SimpleNamespace(get_platform_inst=platform_stub)
    p = plugin_module.SzuHelperPlugin(context, config)
    await p.initialize()

    async def get_catalog():
        return catalog

    p.service.catalog = get_catalog
    yield p
    await p.terminate()
    await asyncio.sleep(0)


async def started(plugin, event):
    task = asyncio.create_task(plugin.bind_dorm(event))
    for _ in range(30):
        if event.sent:
            return task
        await asyncio.sleep(0.001)
    raise AssertionError("No selection prompt")


async def reply(plugin, text, sender="1", group="group1"):
    event = Event(text, sender, group)
    await SessionAgent(plugin.context).handle_session_control_agent(event)
    return event


async def test_real_session_isolation_completion_and_export(plugin):
    a, b = Event("绑定宿舍"), Event("绑定宿舍", "2")
    ta, tb = await started(plugin, a), await started(plugin, b)
    outsider = await reply(plugin, "1", group="other-group")
    assert not outsider.is_stopped()
    for text in ["1", "1", "1", "0601"]:
        event = await reply(plugin, text)
    await ta
    assert "已绑定" in event.output()
    assert not tb.done()
    assert (
        await plugin.store.get_binding("testbot", a.unified_msg_origin, "1")
    ).location.roomName == "0601"
    out = Event("导出宿舍")
    await plugin.export_dorm(out)
    assert plugin_module.decode(out.output()).roomName == "0601"
    await reply(plugin, "取消", "2")
    await tb
    assert await plugin.store.get_binding("testbot", b.unified_msg_origin, "2") is None


async def test_real_rebind_command_passes_session_agent(plugin, location):
    event = Event("绑定宿舍")
    old = await started(plugin, event)
    command = await reply(plugin, "绑定宿舍 " + encode(location))
    assert not command.is_stopped()  # actual built-in agent must not swallow it
    await plugin.bind_dorm(command)
    await old
    assert "已绑定" in command.output()
    assert not plugin._flows
    assert not any(k.startswith("szu-helper:") for k in USER_SESSIONS)


async def test_real_timeout_and_cancel_preserve_binding(plugin, location):
    original = Event("绑定宿舍 " + encode(location))
    await plugin.bind_dorm(original)
    event = Event("绑定宿舍")
    task = await started(plugin, event)
    session = USER_SESSIONS[plugin_module.SenderSessionFilter().filter(event)]
    session.session_controller.keep(timeout=0.01, reset_timeout=True)
    await task
    assert "超时" in event.output()
    assert (
        await plugin.store.get_binding("testbot", event.unified_msg_origin, "1")
    ).location.roomName == location.roomName
    invalid = Event("绑定宿舍 bad")
    await plugin.bind_dorm(invalid)
    assert (
        await plugin.store.get_binding("testbot", event.unified_msg_origin, "1")
    ).location.roomName == location.roomName


async def test_scheduler_schema_reload_and_stop(plugin, tmp_path):
    jobs = plugin.scheduler.get_jobs()
    hour = plugin_module.Settings.parse(plugin.config).hour
    assert len(jobs) == 1 and f"hour='{hour}'" in str(jobs[0].trigger)
    assert jobs[0].max_instances == 1
    task = await started(plugin, Event("绑定宿舍"))
    await plugin.terminate()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not plugin.scheduler.running and not plugin._flows
    plugin.config["daily_check_time"] = "21:30"
    plugin.config["low_power_threshold"] = 7.5
    plugin.config.save_config()
    config = AstrBotConfig(
        plugin.config.config_path, schema=json.loads((root / "_conf_schema.json").read_text())
    )
    other = plugin_module.SzuHelperPlugin(plugin.context, config)
    await other.initialize()
    try:
        assert "hour='21'" in str(other.scheduler.get_jobs()[0].trigger)
        assert other.monitor.threshold == 7.5
    finally:
        await other.terminate()


async def test_message_mentions_and_unsupported_platform(plugin, location):
    from datetime import datetime

    sent = []

    async def send(origin, chain):
        sent.append((origin, chain))
        return True

    plugin.context.send_message = send
    plugin.monitor.threshold = 7.5
    binding = Binding(1, "bot", "bot:GroupMessage:group", "42", "User", True, "aiocqhttp", location)
    report = summarize(
        location, "official", Window.for_days(3), ProviderResult([], 4, datetime.now(SHANGHAI))
    )
    assert await plugin._notify([binding], report)
    assert str(sent[0][1].chain[0].qq) == "42"
    assert "低于 7.5 度" in sent[0][1].chain[-1].text
    from dataclasses import replace

    plugin.context.get_platform_inst = lambda platform_id: platform_stub(
        platform_id, "qq_official", False
    )
    with pytest.raises(plugin_module.ElectricityError, match="适配器声明不支持"):
        await plugin._notify([replace(binding, platform_name="qq_official")], report)
    assert len(sent) == 1


async def test_rebind_while_catalog_is_loading(plugin, location, catalog):
    from dataclasses import replace

    entered = asyncio.Event()
    calls = 0

    async def delayed_catalog():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        return catalog

    plugin.service.catalog = delayed_catalog
    first = Event("绑定宿舍 " + encode(location))
    task = asyncio.create_task(plugin.bind_dorm(first))
    await entered.wait()
    replacement = replace(location, roomName="0801")
    second = Event("绑定宿舍 " + encode(replacement))
    await plugin.bind_dorm(second)
    await task
    assert (
        await plugin.store.get_binding("testbot", second.unified_msg_origin, "1")
    ).location.roomName == "0801"
    assert not plugin._flows


async def test_manual_alert_admin_only_and_schedule_unchanged(plugin, location):
    from datetime import datetime

    from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    handlers = star_handlers_registry.get_handlers_by_module_name(plugin_module.__name__)
    handler = next(h for h in handlers if h.handler_name == "send_low_power_alert")
    permissions = [f for f in handler.event_filters if isinstance(f, PermissionTypeFilter)]
    assert permissions and permissions[0].permission_type == PermissionType.ADMIN
    member = Event("发送低电量预警")
    assert not permissions[0].filter(member, plugin.config)
    calls = []

    async def query(location, detail=False):
        calls.append(detail)
        return summarize(
            location,
            "official",
            Window.for_days(3),
            ProviderResult([], 4, datetime.now(SHANGHAI)),
        )

    plugin.service.query = query
    sent = []

    async def send(origin, chain):
        sent.append((origin, chain))
        return True

    plugin.context.send_message = send
    await plugin.store.bind(
        "testbot", member.unified_msg_origin, "2", "User2", True, "aiocqhttp", location
    )
    await plugin.send_low_power_alert(member)
    assert "仅 AstrBot 管理员" in member.output() and not calls and not sent
    job = plugin.scheduler.get_jobs()[0]
    next_run = job.next_run_time
    admin = Event("发送低电量预警")
    admin.role = "admin"
    assert permissions[0].filter(admin, plugin.config)
    await plugin.send_low_power_alert(admin)
    assert calls == [False] and len(sent) == 1
    assert "已发送 1 条预警" in admin.output()
    assert job.next_run_time == next_run
    assert await plugin.store._all("SELECT * FROM dorm_state") == []
    assert await plugin.store._all("SELECT * FROM deliveries") == []
    # The command still works when the daily job is disabled/absent.
    plugin.scheduler.remove_all_jobs()
    await plugin.send_low_power_alert(admin)
    assert len(sent) == 2 and plugin.scheduler.get_jobs() == []


async def test_manual_alert_command_bypasses_active_binding_session(plugin):
    event = Event("绑定宿舍")
    task = await started(plugin, event)
    admin = await reply(plugin, "发送低电量预警")
    assert not admin.is_stopped()
    admin.role = "admin"
    await plugin.send_low_power_alert(admin)
    assert "检查 0 间宿舍" in admin.output()
    assert not task.done()
    await reply(plugin, "取消")
    await task


async def test_all_network_features_follow_selected_source(tmp_path, monkeypatch, location):
    import gzip
    from datetime import datetime
    from urllib.parse import parse_qs

    import httpx

    from szu_electricity.providers import IotunProvider, OfficialProvider

    requests = []

    def handler(req):
        requests.append((req.url.host, req.url.path, dict(req.url.params)))
        if req.url.host == "www.iotun.com":
            if req.url.path == "/api/buildings":
                data = [{"group": "yuehai_sftest", "buildings": [{"id": "03", "name": "山茶斋"}]}]
            else:
                assert req.url.path == "/api/status"
                assert req.url.params["client"] == "yuehai_sftest"
                date = str(datetime.now(SHANGHAI).date())
                data = {
                    "remaining": 4,
                    "last_record": date,
                    "trend": [{"date": date, "daily_used_kwh": 2}],
                }
            compressed = gzip.compress(json.dumps({"ok": True, "data": data}).encode())
            return httpx.Response(
                200,
                stream=httpx.ByteStream(compressed),
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            )
        if req.url.host == "172.25.100.105":
            text = '<select name="drlouming"><option value="01">梧桐树</option></select>'
        elif req.method == "GET":
            text = '<form action="login.do"><select name="buildingId"><option value="54">官方楼栋</option></select></form>'
        elif req.url.path.endswith("login.do"):
            form = parse_qs(req.content.decode(), encoding="gb18030")
            assert form["buildingId"] == ["54"]
            text = (
                '<form action="selectList.do"><input type="hidden" name="roomId" value="42"></form>'
            )
        else:
            date = str(datetime.now(SHANGHAI).date())
            text = f'<table id="oTable"><tr><td>1</td><td>{location.roomName}</td><td>4</td><td>12</td><td>50</td><td>{date}</td></tr></table>'
        return httpx.Response(
            200,
            content=text.encode("gb18030"),
            headers={"Content-Type": "text/html; charset=gb2312"},
        )

    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        plugin_module, "OfficialProvider", lambda **kwargs: OfficialProvider(factory, **kwargs)
    )
    monkeypatch.setattr(
        plugin_module, "IotunProvider", lambda **kwargs: IotunProvider(factory, **kwargs)
    )
    monkeypatch.setattr(plugin_module, "get_astrbot_data_path", lambda: str(tmp_path))
    config = AstrBotConfig(
        str(tmp_path / "config.json"), schema=json.loads((root / "_conf_schema.json").read_text())
    )
    sent = []

    async def send(origin, chain):
        sent.append((origin, chain))
        return True

    context = SimpleNamespace(send_message=send, get_platform_inst=platform_stub)
    plugin = plugin_module.SzuHelperPlugin(context, config)
    await plugin.initialize()
    try:
        assert (await plugin.service.catalog()).areas[0].buildings[0].name == "官方楼栋"
        requests.clear()
        # Mutate the actual config without rebuilding the service or monitors.
        config["data_source"] = "iotun"
        event = Event("绑定宿舍")
        task = await started(plugin, event)
        assert "请选择校区" in event.output()
        await reply(plugin, "取消")
        await task
        assert (await plugin.service.catalog()).areas[0].buildings[0].name == "山茶斋"
        imported = Event("绑定宿舍 " + encode(location))
        await plugin.bind_dorm(imported)
        assert "已绑定" in imported.output()
        count = len(requests)
        await plugin.export_dorm(Event("导出宿舍"))
        assert len(requests) == count  # share export is entirely local
        for command in ["用电", "用电 详情"]:
            event = Event(command)
            await plugin.electricity(event)
            assert "剩余电量" in event.output()
            if "详情" in command:
                assert "iotun.com" in event.output()
        await plugin.monitor.run()
        admin = Event("发送低电量预警")
        admin.role = "admin"
        await plugin.send_low_power_alert(admin)
        assert len(sent) == 2
        assert all(host == "www.iotun.com" for host, _, _ in requests)
        assert [params["days"] for _, path, params in requests if path == "/api/status"] == [
            "3",
            "31",
        ]
        assert await plugin.store.catalog("official") is not None
        assert await plugin.store.catalog("iotun") is not None
        requests.clear()
        config["data_source"] = "official"
        assert (await plugin.service.catalog()).areas[0].buildings[0].name == "官方楼栋"
        await plugin.electricity(Event("用电"))
        assert requests and all(host == "192.168.84.3" for host, _, _ in requests)
        requests.clear()
        await plugin.unbind_dorm(Event("解绑宿舍"))
        assert requests == []
    finally:
        await plugin.terminate()
        await asyncio.sleep(0)


async def bind_in_session(
    plugin, location, *, group="source-group", sender="1", platform="testbot"
):
    event = Event("绑定宿舍", sender=sender, group=group, platform=platform)
    await plugin.store.bind(
        *plugin._identity(event), event.get_sender_name(), bool(group), "aiocqhttp", location
    )
    return event


async def test_reuse_confirmation_is_local_and_preserves_source(plugin, location):
    from dataclasses import replace
    from datetime import datetime

    source = await bind_in_session(plugin, location)
    previous = replace(location, roomName="0801")
    target = await bind_in_session(plugin, previous, group="group1")
    source_binding = await plugin.store.get_binding(*plugin._identity(source))
    report = summarize(
        location, "official", Window.for_days(3), ProviderResult([], 4, datetime.now(SHANGHAI))
    )
    episode = await plugin.store.observe(report, 5)

    async def sent(_):
        return True

    await plugin.store.deliver(
        location.key, episode, source.unified_msg_origin, [source_binding.id], sent
    )
    deliveries = [tuple(row) for row in await plugin.store._all("SELECT * FROM deliveries")]

    async def no_network():
        raise AssertionError("Reusing a binding must not load the catalog")

    plugin.service.catalog = no_network

    task = await started(plugin, target)
    assert "是否直接复用" in target.output() and "山茶斋 0601" in target.output()
    assert source.unified_msg_origin not in target.output()
    assert asdict((await plugin.store.get_binding(*plugin._identity(target))).location) == asdict(
        previous
    )
    outsider = await reply(plugin, "是", sender="2")
    assert not outsider.is_stopped()
    assert asdict((await plugin.store.get_binding(*plugin._identity(target))).location) == asdict(
        previous
    )
    accepted = await reply(plugin, "是")
    await task
    assert "已绑定" in accepted.output()
    assert asdict((await plugin.store.get_binding(*plugin._identity(target))).location) == asdict(
        location
    )
    assert await plugin.store.get_binding(*plugin._identity(source)) == source_binding
    assert [tuple(row) for row in await plugin.store._all("SELECT * FROM deliveries")] == deliveries


@pytest.mark.parametrize("source", ["current-session", "other-user", "other-platform"])
async def test_reuse_excludes_unrelated_bindings(plugin, location, source):
    if source == "current-session":
        await bind_in_session(plugin, location, group="group1")
    elif source == "other-user":
        await bind_in_session(plugin, location, sender="2")
    else:
        await bind_in_session(plugin, location, platform="other-bot")
    event = Event("绑定宿舍")
    task = await started(plugin, event)
    assert "请选择校区" in event.output() and "复用" not in event.output()
    await reply(plugin, "取消")
    await task


async def test_reuse_multiple_dorms_deduplicates_and_selects(plugin, location):
    from dataclasses import replace

    await bind_in_session(plugin, location, group="source1")
    second = replace(location, roomName="0801")
    await bind_in_session(plugin, second, group="source2")
    await bind_in_session(plugin, location, group="source3")
    event = Event("绑定宿舍")
    task = await started(plugin, event)
    assert "多个宿舍" in event.output()
    assert event.output().count("0601") == event.output().count("0801") == 1
    invalid = await reply(plugin, "99")
    assert "请回复已有宿舍的编号" in invalid.output()
    assert await plugin.store.get_binding(*plugin._identity(event)) is None
    await reply(plugin, "2")
    await task
    assert asdict((await plugin.store.get_binding(*plugin._identity(event))).location) == asdict(
        second
    )


async def test_reuse_decline_loads_current_source_then_selects(plugin, location, catalog):
    source = await bind_in_session(plugin, location, group="")  # private -> group
    calls = []

    async def current_catalog():
        calls.append(plugin.service.provider.name)
        return catalog

    plugin.service.catalog = current_catalog
    event = Event("绑定宿舍")
    task = await started(plugin, event)
    assert not calls
    plugin.config["data_source"] = "iotun"
    declined = await reply(plugin, "否")
    assert "请选择校区" in declined.output() and calls == ["iotun"]
    for text in ["1", "1", "1", "0901"]:
        await reply(plugin, text)
    await task
    assert (await plugin.store.get_binding(*plugin._identity(event))).location.roomName == "0901"
    assert asdict((await plugin.store.get_binding(*plugin._identity(source))).location) == asdict(
        location
    )


@pytest.mark.parametrize("action", ["取消", "timeout"])
async def test_reuse_cancel_and_timeout_preserve_current_binding(plugin, location, action):
    from dataclasses import replace

    await bind_in_session(plugin, location)
    old = replace(location, roomName="0801")
    target = await bind_in_session(plugin, old, group="group1")
    task = await started(plugin, target)
    if action == "timeout":
        key = plugin_module.SenderSessionFilter().filter(target)
        USER_SESSIONS[key].session_controller.keep(timeout=0.01, reset_timeout=True)
    else:
        await reply(plugin, action)
    await task
    assert asdict((await plugin.store.get_binding(*plugin._identity(target))).location) == asdict(
        old
    )
    assert not plugin._flows


async def test_explicit_import_replaces_reuse_prompt(plugin, location):
    from dataclasses import replace

    await bind_in_session(plugin, location)
    event = Event("绑定宿舍")
    waiting = await started(plugin, event)
    imported = replace(location, roomName="0901")
    command = await reply(plugin, "绑定宿舍 " + encode(imported))
    assert not command.is_stopped()
    await plugin.bind_dorm(command)
    await waiting
    assert "已绑定" in command.output() and "复用" not in command.output()
    assert asdict((await plugin.store.get_binding(*plugin._identity(command))).location) == asdict(
        imported
    )


async def test_reuse_decline_can_recover_from_catalog_error(plugin, location):
    await bind_in_session(plugin, location)

    async def unavailable():
        raise plugin_module.ElectricityError("iotun 暂不可用")

    plugin.service.catalog = unavailable
    event = Event("绑定宿舍")
    task = await started(plugin, event)
    declined = await reply(plugin, "否")
    assert "iotun 暂不可用" in declined.output() and "是否直接复用" in declined.output()
    assert await plugin.store.get_binding(*plugin._identity(event)) is None
    await reply(plugin, "是")
    await task
    assert asdict((await plugin.store.get_binding(*plugin._identity(event))).location) == asdict(
        location
    )


def mentioned_ids(message):
    return [
        str(component.qq) for component in message.chain if isinstance(component, plugin_module.At)
    ]


async def test_query_and_error_replies_mention_only_initiator(plugin, location):
    await bind_in_session(plugin, location, group="group1", sender="1")
    await bind_in_session(plugin, location, group="group1", sender="2")
    from datetime import datetime

    async def query(location, detail=False):
        return summarize(
            location, "official", Window.for_days(3), ProviderResult([], 4, datetime.now(SHANGHAI))
        )

    plugin.service.query = query
    for sender in ["1", "2"]:
        event = Event("用电", sender=sender)
        await plugin.electricity(event)
        assert mentioned_ids(event.sent[-1]) == [sender]
        assert "剩余电量" in event.output()
    unbound = Event("用电", sender="3")
    await plugin.electricity(unbound)
    assert mentioned_ids(unbound.sent[-1]) == ["3"]
    assert "尚未绑定" in unbound.output()


async def test_binding_prompts_and_share_reply_mention_initiator(plugin):
    event = Event("绑定宿舍")
    task = await started(plugin, event)
    assert mentioned_ids(event.sent[0]) == ["1"]
    for text in ["1", "1", "1", "0601"]:
        response = await reply(plugin, text)
        assert all(mentioned_ids(message) == ["1"] for message in response.sent)
    await task
    exported = Event("导出宿舍")
    await plugin.export_dorm(exported)
    assert mentioned_ids(exported.sent[0]) == ["1"]
    assert plugin_module.decode(exported.sent[0].chain[-1].text).roomName == "0601"


async def test_batch_alerts_mention_owners_but_receipts_mention_admin(plugin, location):
    from datetime import datetime

    for sender, group in [("1", "group1"), ("2", "group1"), ("3", "group2")]:
        await bind_in_session(plugin, location, group=group, sender=sender)

    async def query(location, detail=False):
        return summarize(
            location, "official", Window.for_days(3), ProviderResult([], 4, datetime.now(SHANGHAI))
        )

    plugin.service.query = query
    sent = []

    async def send(origin, chain):
        sent.append((origin, mentioned_ids(chain)))
        return True

    plugin.context.send_message = send
    admin = Event("发送低电量预警", sender="9")
    admin.role = "admin"
    await plugin.send_low_power_alert(admin)
    assert sent == [
        ("testbot:GroupMessage:group1", ["1", "2"]),
        ("testbot:GroupMessage:group2", ["3"]),
    ]
    assert all(mentioned_ids(message) == ["9"] for message in admin.sent)
    sent.clear()
    await plugin.monitor.run()
    assert sent == [
        ("testbot:GroupMessage:group1", ["1", "2"]),
        ("testbot:GroupMessage:group2", ["3"]),
    ]


async def test_private_reply_has_no_group_mention(plugin):
    event = Event("用电", group="")
    await plugin.electricity(event)
    assert mentioned_ids(event.sent[0]) == []
    assert "尚未绑定" in event.output()


async def test_qqofficial_428_real_adapter_sends_group_and_private_alerts(
    plugin, location, monkeypatch
):
    from datetime import datetime

    from astrbot.api.star import Context
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
        QQOfficialPlatformAdapter,
    )

    if not hasattr(QQOfficialPlatformAdapter, "_send_by_session_common"):
        pytest.skip("This real-adapter test requires AstrBot 4.28.2 or newer")
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    calls = []

    class QQAPI:
        async def post_group_message(self, **payload):
            calls.append(("group", payload))
            return {"id": f"group-message-{len(calls)}"}

        async def post_c2c_message(self, **payload):
            calls.append(("private", payload))
            return {"id": f"private-message-{len(calls)}"}

    api = QQAPI()
    adapter = object.__new__(QQOfficialPlatformAdapter)
    adapter.config = {"id": "qq-main"}
    adapter.client = SimpleNamespace(api=api)
    adapter.use_markdown_default = False  # Plugin Markdown must override the adapter default.
    adapter._session_last_message_id = {}
    adapter._session_scene = {"group-openid": "group"}
    adapter._allow_group_proactive_send = True
    context = object.__new__(Context)
    context.platform_manager = SimpleNamespace(platform_insts=[adapter])
    context.astrbot_config_mgr = SimpleNamespace(get_conf=lambda _: {})
    plugin.context = context
    assert adapter.meta().support_proactive_message is True
    for user, origin, is_group in [
        ("member-one", "qq-main:GroupMessage:group-openid", True),
        ("member-two", "qq-main:GroupMessage:group-openid", True),
        ("user-openid", "qq-main:FriendMessage:user-openid", False),
    ]:
        await plugin.store.bind("qq-main", origin, user, user, is_group, "qq_official", location)

    async def query(location, detail=False):
        return summarize(
            location, "official", Window.for_days(3), ProviderResult([], 4, datetime.now(SHANGHAI))
        )

    plugin.service.query = query
    result = await plugin.monitor.run_manual()
    assert result.checked_dorms == result.low_dorms == 1
    assert result.sent_messages == 2 and result.send_failures == 0
    assert result.send_errors == []
    assert [kind for kind, _ in calls] == ["group", "private"]
    assert calls[0][1]["group_openid"] == "group-openid"
    assert "<@" not in calls[0][1]["markdown"]["content"]
    assert '<qqbot-at-user id="member-one" />' in calls[0][1]["markdown"]["content"]
    assert '<qqbot-at-user id="member-two" />' in calls[0][1]["markdown"]["content"]
    assert all(payload["msg_type"] == 2 and "content" not in payload for _, payload in calls)
    assert "msg_id" not in calls[0][1]  # true proactive group send, no cached incoming message
    assert all("低于 5 度" in payload["markdown"]["content"] for _, payload in calls)
    assert await plugin.store._all("SELECT * FROM deliveries") == []
    await plugin.monitor.run()
    assert len(calls) == 4
    assert len(await plugin.store._all("SELECT * FROM deliveries")) == 3


async def test_qqofficial_missing_context_is_not_reported_as_sent(plugin, location):
    from datetime import datetime

    adapter = platform_stub("testbot", "qq_official", True)
    adapter._session_scene = {}
    adapter._session_last_message_id = {}
    adapter._allow_group_proactive_send = True
    plugin.context.get_platform_inst = lambda _: adapter

    async def must_not_send(*args):
        raise AssertionError("Missing QQ delivery context must not be counted as successful")

    plugin.context.send_message = must_not_send
    await bind_in_session(plugin, location, group="group1")

    async def query(location, detail=False):
        return summarize(
            location, "official", Window.for_days(3), ProviderResult([], 4, datetime.now(SHANGHAI))
        )

    plugin.service.query = query
    result = await plugin.monitor.run_manual()
    assert result.low_dorms == 1 and result.sent_messages == 0 and result.send_failures == 1
    assert "目标会话上下文缺失" in result.message()
    assert await plugin.store._all("SELECT * FROM deliveries") == []


async def test_missing_delivery_platform_is_explained(plugin, location):
    from datetime import datetime

    plugin.context.get_platform_inst = lambda _: None
    await bind_in_session(plugin, location)

    async def query(location, detail=False):
        return summarize(
            location, "official", Window.for_days(3), ProviderResult([], 4, datetime.now(SHANGHAI))
        )

    plugin.service.query = query
    result = await plugin.monitor.run_manual()
    assert result.send_failures == 1 and "平台实例已不可用" in result.message()


async def make_qq_incoming(api, scene="group", quote_index="REFIDX_this-message=="):
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
        PatchedC2CMessage,
        PatchedGroupMessage,
        QQOfficialPlatformAdapter,
    )

    data = {
        "id": "ROBOT-command-token",
        "author": {
            "id": "author-id",
            "member_openid": "member-openid",
            "user_openid": "user-openid",
            "username": "DISPLAY_NAME_NOT_A_MENTION",
        },
        "group_openid": "group-openid",
        "content": "/用电",
        "message_scene": {
            "ext": ["ref_msg_idx=REFIDX_previous-message==", "auth_token=DO_NOT_FORWARD_THIS_TOKEN"]
        },
    }
    if quote_index:
        data["message_scene"]["ext"].append("msg_idx=" + quote_index)
    source = (PatchedGroupMessage if scene == "group" else PatchedC2CMessage)(api, "event-id", data)
    message_type = MessageType.GROUP_MESSAGE if scene == "group" else MessageType.FRIEND_MESSAGE
    message = await QQOfficialPlatformAdapter._parse_from_qqofficial(source, message_type)
    session = "group-openid" if scene == "group" else "user-openid"
    if scene == "group":
        message.group_id = session
    return QQOfficialMessageEvent(
        "用电",
        message,
        PlatformMetadata("qq_official", "QQ", id="qq-main"),
        session,
        SimpleNamespace(api=api),
    )


@pytest.mark.parametrize("scene", ["group", "private"])
async def test_qq_reply_quotes_own_message_without_fake_group_mentions(plugin, monkeypatch, scene):
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    calls = []

    class API:
        async def post_group_message(self, **payload):
            calls.append(payload)
            return {"id": "response-1"}

        async def post_c2c_message(self, **payload):
            calls.append(payload)
            return {"id": "response-1"}

    event = await make_qq_incoming(API(), scene)
    await plugin._reply(event, "剩余电量 88 度")
    await plugin._reply(event, "第二条回复")
    assert len(calls) == 2
    assert all(p["message_reference"] == {"message_id": "REFIDX_this-message=="} for p in calls)
    assert all(p["msg_id"] == "ROBOT-command-token" for p in calls)
    assert calls[0]["msg_seq"] != calls[1]["msg_seq"]
    if scene == "group":
        assert all(p["msg_type"] == 2 and "content" not in p for p in calls)
        assert (
            calls[0]["markdown"]["content"]
            == '<qqbot-at-user id="member-openid" />\n剩余电量 88 度'
        )
        assert calls[0]["group_openid"] == "group-openid"
    else:
        assert all(p["msg_type"] == 0 for p in calls)
        assert calls[0]["content"] == "剩余电量 88 度"
        assert calls[0]["openid"] == "user-openid"
    serialized = json.dumps(calls)
    assert "DISPLAY_NAME_NOT_A_MENTION" not in serialized
    assert "previous-message" not in serialized and "DO_NOT_FORWARD_THIS_TOKEN" not in serialized
    assert event._has_send_oper


async def test_qq_missing_quote_index_keeps_safe_text_without_fake_mention(plugin, monkeypatch):
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    calls = []

    class API:
        async def post_group_message(self, **payload):
            calls.append(payload)
            return {"id": "response-1"}

    event = await make_qq_incoming(API(), quote_index=None)
    await plugin._reply(event, "正文中的 <@other-id> & 字符")
    assert "message_reference" not in calls[0]
    assert (
        calls[0]["markdown"]["content"]
        == '<qqbot-at-user id="member-openid" />\n正文中的 &lt;@other\\-id&gt; &amp; 字符'
    )


async def test_qq_expired_passive_reply_preserves_quote_without_uid_prefix(plugin, monkeypatch):
    import botpy.errors

    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    calls = []

    class API:
        async def post_group_message(self, **payload):
            calls.append(payload)
            if len(calls) == 1:
                raise botpy.errors.ForbiddenError("expired msg_id")
            return {"id": "response-1"}

    event = await make_qq_incoming(API())
    await plugin._reply(event, "回复")
    assert len(calls) == 2 and "msg_id" not in calls[1]
    assert calls[1]["message_reference"] == {"message_id": "REFIDX_this-message=="}
    assert calls[1]["markdown"]["content"] == '<qqbot-at-user id="member-openid" />\n回复'


async def test_generic_reply_quotes_incoming_command_not_its_reference(plugin):
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    event = Event("用电")
    event.message_obj.message_id = "this-command"
    event.message_obj.message = [plugin_module.Reply(id="earlier-message")]
    await plugin.electricity(event)
    chain = event.sent[0].chain
    assert isinstance(chain[0], plugin_module.Reply)
    assert chain[0].id == "this-command"
    assert mentioned_ids(event.sent[0]) == ["1"]
    payload = await AiocqhttpMessageEvent._parse_onebot_json(event.sent[0])
    assert payload[0] == {"type": "reply", "data": {"id": "this-command"}}
    assert payload[1] == {"type": "at", "data": {"qq": "1"}}


async def test_qq_group_does_not_print_reported_openid_format(plugin, monkeypatch):
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    calls = []

    class API:
        async def post_group_message(self, **payload):
            calls.append(payload)
            return {"id": "response-1"}

    event = await make_qq_incoming(API())
    opaque_id = "595BD8" + "A" * 20 + "51A276"
    event.message_obj.sender.user_id = opaque_id
    await plugin._reply(event, "用电查询结果")
    assert calls[0]["markdown"]["content"] == f'<qqbot-at-user id="{opaque_id}" />\n用电查询结果'
    assert "<@" not in calls[0]["markdown"]["content"]
    assert calls[0]["message_reference"] == {"message_id": "REFIDX_this-message=="}
    assert (
        plugin._mention("qq_official", True, opaque_id, "用户名", qq_scene="group")[0].text
        == f'<qqbot-at-user id="{opaque_id}" />\n'
    )
    assert (
        plugin._mention("qq_official", True, opaque_id, "用户名")[0].text
        == f'<qqbot-at-user id="{opaque_id}" />\n'
    )


async def test_qq_channel_keeps_documented_channel_mentions(plugin, monkeypatch):
    import botpy.message
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )

    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    calls = []

    class API:
        async def post_message(self, **payload):
            calls.append(payload)
            return {"id": "response-1"}

    api = API()
    raw = botpy.message.Message(
        api,
        None,
        {
            "id": "channel-command",
            "channel_id": "123",
            "author": {"id": "456", "username": "姓名"},
            "content": "用电",
        },
    )
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.group_id = "123"
    message.sender = MessageMember(user_id="456", nickname="姓名")
    message.message_id = "channel-command"
    message.raw_message = raw
    event = QQOfficialMessageEvent(
        "用电",
        message,
        PlatformMetadata("qq_official", "QQ", id="qq-main"),
        "123",
        SimpleNamespace(api=api),
    )
    await plugin._reply(event, "查询结果")
    assert calls[0]["markdown"]["content"] == '<qqbot-at-user id="456" />\n查询结果'
    assert calls[0]["message_reference"] == {"message_id": "channel-command"}
    assert "msg_type" not in calls[0]
    assert (
        plugin._mention("qq_official", True, "456", "姓名", qq_scene="channel")[0].text
        == '<qqbot-at-user id="456" />\n'
    )
