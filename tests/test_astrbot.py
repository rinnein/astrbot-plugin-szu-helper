"""Integration tests with real AstrBot classes; network delivery is captured."""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
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
from astrbot.builtin_stars.session_controller.main import Main as SessionAgent
from astrbot.core.utils.session_waiter import USER_SESSIONS

from szu_electricity.analytics import summarize
from szu_electricity.models import SHANGHAI, Binding, ProviderResult, Window
from szu_electricity.sharing import encode


class Event(AstrMessageEvent):
    def __init__(self, text, sender="1", group="group1", platform="testbot"):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        msg.sender = MessageMember(user_id=sender, nickname="User" + sender)
        msg.group_id = group
        msg.message = []
        super().__init__(
            text, msg, PlatformMetadata("aiocqhttp", "Test", id=platform), group or sender
        )
        self.sent = []
        self.is_at_or_wake_command = True

    async def send(self, message):
        self.sent.append(message)

    def output(self):
        return "\n".join(c.text for m in self.sent for c in m.chain if hasattr(c, "text"))


@pytest.fixture
async def plugin(tmp_path, monkeypatch, catalog):
    monkeypatch.setattr(plugin_module, "get_astrbot_data_path", lambda: str(tmp_path))
    config = AstrBotConfig(
        str(tmp_path / "config.json"), schema=json.loads((root / "_conf_schema.json").read_text())
    )
    context = SimpleNamespace()
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
    assert len(jobs) == 1 and "hour='8'" in str(jobs[0].trigger)
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

    assert not await plugin._notify([replace(binding, platform_name="qq_official")], report)
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
