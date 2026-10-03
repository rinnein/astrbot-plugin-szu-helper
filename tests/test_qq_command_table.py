import asyncio
import copy
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from test_astrbot import SessionAgent, make_qq_incoming, plugin_module
from test_astrbot import plugin as plugin
from test_qq_interactions import API, button, interaction, setup_dialog

from szu_electricity.conversation import Selection
from szu_electricity.presentation import choice_card, command_tag
from szu_electricity.qq_messages import MessageRetirer, UserSelectionMessage


@pytest.fixture(autouse=True)
def disable_metrics(monkeypatch):
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")


def commands(payload):
    return [
        unquote(text)
        for text in re.findall(r'<qqbot-cmd-input text="([^"]+)"', payload["markdown"]["content"])
    ]


async def incoming(api, text, id, *, private=False, age=1):
    event = await make_qq_incoming(api, "private" if private else "group")
    event.message_str = text
    event.message_obj.message_id = "not-the-delete-id"
    raw = event.message_obj.raw_message.raw_data
    raw["id"] = id
    raw["timestamp"] = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
    return event


async def submit(plugin, api, command, id, *, private=False, prefix="/"):
    event = await incoming(api, command.removeprefix(prefix), id, private=private)
    event.is_at_or_wake_command = True
    from astrbot.core.star.star_handler import star_handlers_registry

    handlers = star_handlers_registry.get_handlers_by_module_name(plugin_module.__name__)
    handler = next(h for h in handlers if h.handler_name == "select_dorm_option")
    assert all(f.filter(event, plugin.config) for f in handler.event_filters)
    await SessionAgent(plugin.context).handle_session_control_agent(event)
    assert not event.is_stopped()  # waiter must leave this for the command handler
    await plugin.select_dorm_option(event)
    return event


def test_table_and_keyboard_share_actions_and_escape_labels(catalog):
    from szu_electricity.models import Option

    s = Selection(catalog)
    s.accept("1")
    s.accept("1")
    s.catalog.areas[0].buildings[0] = Option("54", "长楼名|<qqbot-at-everyone />\n新行")
    view, actions = choice_card(s, "token", 1, "owner", keyboard=True, command_prefix="!")
    assert "| 编号 | 选项 | 操作 |" in view.markdown
    assert "&lt;qqbot" in view.markdown and "<qqbot-at-everyone" not in view.markdown
    assert "长楼名\\|" in view.markdown
    links = re.findall(
        r'<qqbot-cmd-input text="([^"]+)" show="([^"]+)" reference="true" />', view.markdown
    )
    buttons = [b for row in view.keyboard["content"]["rows"] for b in row["buttons"]]
    assert len(links) == len(buttons) == len(actions)
    assert (
        len([line for line in view.markdown.splitlines() if line.startswith("| ")]) == 20
    )  # 15 options, 3 controls, header+separator
    for i, (text, show) in enumerate(links):
        assert len(text) <= 100 and len(show) <= 100
        assert unquote(text) == f"!宿舍选项 token:1:{i}"
        assert buttons[i]["action"]["data"] == f"szuh:token:1:{i}"
    assert "qqbot-cmd-enter" not in view.markdown
    assert "<qqbot-cmd-input" in view.fallback_markdown
    with pytest.raises(Exception, match="长度限制"):
        command_tag("x" * 101, "选择")
    assert "%22%26" in command_tag('"&', "选择")


@pytest.mark.parametrize("private", [False, True])
async def test_without_keyboard_complete_via_table_and_cleanup(plugin, monkeypatch, private):
    api = API()
    event = await make_qq_incoming(api, "private" if private else "group")
    event.message_str = "绑定宿舍"
    plugin.context.get_config = lambda _: {"wake_prefix": ["!"]}
    task = asyncio.create_task(plugin.bind_dorm(event))
    await api.posted.wait()
    assert "keyboard" not in api.posts[-1]
    for i in range(3):
        command = commands(api.posts[-1])[0]
        assert command.startswith("!宿舍选项 ")
        await submit(plugin, api, command, f"selection-{i}", private=private, prefix="!")
    room = await incoming(api, "0601", "room", private=private)
    await SessionAgent(plugin.context).handle_session_control_agent(room)
    await task
    await plugin.retirer.drain()
    assert (await plugin.store.get_binding(*plugin._identity(event))).location.roomName == "0601"
    deleted = [e[1].rsplit("/", 1)[-1] for e in api.events if e[0] == "delete"]
    assert "ROBOT-command-token" not in deleted and "not-the-delete-id" not in deleted
    assert set(deleted) == {f"bot-{i}" for i in range(1, 5)} | (
        set() if private else {"selection-0", "selection-1", "selection-2", "room"}
    )
    assert api.events.index(("post", "bot-2", "c2c" if private else "group")) < next(
        i for i, e in enumerate(api.events) if e[0] == "delete"
    )


async def test_stale_foreign_and_busy_commands_never_recall_user(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    command = commands(api.posts[-1])[0]
    dialog = next(iter(plugin.keyboard.dialogs.values()))
    for case in ("sender", "session", "platform"):
        foreign = await incoming(api, command.removeprefix("/"), "foreign-" + case)
        if case == "sender":
            foreign.message_obj.sender.user_id = "other"
        elif case == "session":
            foreign.session_id = "other-session"
        else:
            foreign.platform_meta.id = "other-platform"
        await plugin.select_dorm_option(foreign)
        if case == "platform":
            foreign.platform_meta.id = "qq-main"
        assert dialog.selection.step == 0
    dialog.busy = True
    await submit(plugin, api, command, "busy")
    dialog.busy = False
    await submit(plugin, api, command, "valid")
    assert dialog.selection.step == 1
    await submit(plugin, api, command, "stale")
    await plugin.retirer.drain()
    deleted = [e[1] for e in api.events if e[0] == "delete"]
    assert any(p.endswith("/valid") for p in deleted)
    assert not any(any(x in p for x in ("foreign", "busy", "stale")) for p in deleted)
    await client.on_interaction_create(
        interaction(
            f"szuh:{dialog.token}:{dialog.revision}:"
            + next(k for k, v in dialog.actions.items() if v == "取消"),
            id="cancel",
        )
    )
    await task


@pytest.mark.parametrize("failure", ["send", "invalid"])
async def test_failed_choice_keeps_user_message(plugin, monkeypatch, failure):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    dialog = next(iter(plugin.keyboard.dialogs.values()))
    original = dialog.receipt
    if failure == "send":
        command = commands(api.posts[-1])[0]
        api.fail_send = True
        await submit(plugin, api, command, "failed-selection")
        assert dialog.receipt is original
        assert not any(e[0] == "delete" for e in api.events)
        api.fail_send = False
    else:
        invalid = await incoming(api, "invalid", "failed-selection")
        await SessionAgent(plugin.context).handle_session_control_agent(invalid)
    await plugin.retirer.drain()
    assert not any(e[0] == "delete" and e[1].endswith("/failed-selection") for e in api.events)
    command = commands(api.posts[-1])[-1]
    await submit(plugin, api, command, "cancel-selection")
    await task
    await plugin.retirer.drain()
    assert any(e[0] == "delete" and e[1].endswith("/cancel-selection") for e in api.events)


@pytest.mark.parametrize("denied", ["bot", "user"])
async def test_delete_permission_failure_independent_and_once(plugin, monkeypatch, denied):
    import botpy.errors

    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    attempts = []

    async def request(route, **kwargs):
        attempts.append(route.url)
        if ("bot-" in route.url) == (denied == "bot"):
            raise botpy.errors.ForbiddenError('{"code":40062003,"message":"no permission"}')
        return None

    api.request = request
    selected = await submit(plugin, api, commands(api.posts[-1])[0], "user-selection")
    await plugin.retirer.drain()
    assert len(attempts) == 2
    plugin.retirer.schedule_selection(selected, event)
    await plugin.retirer.drain()
    assert len(attempts) == 2
    assert next(iter(plugin.keyboard.dialogs.values())).selection.step == 1
    await submit(plugin, api, commands(api.posts[-1])[-1], "cancel")
    await task


async def test_user_recall_time_boundary_and_missing_timestamp(plugin, monkeypatch):
    api = API()
    initial = await make_qq_incoming(api)
    for age in [121, -10]:
        e = await incoming(api, "1", f"age-{age}", age=age)
        plugin.retirer.schedule_selection(e, initial)
    missing = await incoming(api, "1", "missing")
    missing.message_obj.raw_message.raw_data.pop("timestamp")
    plugin.retirer.schedule_selection(missing, initial)
    await plugin.retirer.drain()
    assert not api.events
    now = [119.0]

    async def sleep(delay):
        now[0] += delay

    retirer = MessageRetirer(logging.getLogger(), clock=lambda: now[0], sleep=sleep)
    r = UserSelectionMessage("qq-main", "peer", "incoming-raw-id", 0, api)
    retirer.schedule(r)
    await retirer.drain()
    now[0] = 120
    retirer.schedule(UserSelectionMessage("qq-main", "peer", "expired", 0, api))
    await retirer.drain()
    assert len(api.events) == 1 and api.events[0][1].endswith("/incoming-raw-id")
    await retirer.close()


async def test_table_layout_then_tag_rejection_preserves_markdown(plugin, monkeypatch, catalog):
    import botpy.errors

    api = API()
    calls = []

    async def post(**payload):
        calls.append(copy.deepcopy(payload))
        content = payload["markdown"]["content"]
        if "| 编号 |" in content:
            raise botpy.errors.ForbiddenError("markdown table rejected")
        if "<qqbot-cmd-input" in content:
            raise botpy.errors.ForbiddenError("qqbot-cmd-input rejected")
        return {"id": "plain-numbered-markdown"}

    api.post_group_message = post
    event = await make_qq_incoming(api)
    # Obtain the view through the plugin's own module namespace.
    from importlib import import_module

    module = import_module(plugin_module.__package__ + ".szu_electricity.presentation")
    view, _ = module.choice_card(module.Selection(catalog), "token", 1, "owner", keyboard=False)
    await plugin._reply(event, view)
    assert len(calls) == 3
    assert all(p["msg_type"] == 2 and "content" not in p for p in calls)
    assert "<qqbot-cmd-input" in calls[1]["markdown"]["content"]
    assert "<qqbot-cmd-input" not in calls[2]["markdown"]["content"]


async def test_nondefault_prefix_through_real_waking_stage(plugin, monkeypatch):
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.star.session_plugin_manager import SessionPluginManager
    from astrbot.core.star.star_handler import star_handlers_registry

    api = API()
    event = await make_qq_incoming(api)
    event.message_str = "绑定宿舍"
    plugin.context.get_config = lambda _: {"wake_prefix": ["!"]}
    task = asyncio.create_task(plugin.bind_dorm(event))
    await api.posted.wait()
    selected = await incoming(api, commands(api.posts[-1])[0], "prefix-choice")
    handlers = star_handlers_registry.get_handlers_by_module_name(plugin_module.__name__)
    handler = next(h for h in handlers if h.handler_name == "select_dorm_option")
    monkeypatch.setattr(
        star_handlers_registry, "get_handlers_by_event_type", lambda *args, **kwargs: [handler]
    )

    async def passthrough(event, handlers):
        return handlers

    monkeypatch.setattr(SessionPluginManager, "filter_handlers_by_session", passthrough)
    stage = object.__new__(WakingCheckStage)
    stage.ctx = SimpleNamespace(astrbot_config={"wake_prefix": ["!"], "admins_id": []})
    stage.unique_session = stage.ignore_bot_self_message = stage.ignore_at_all = False
    stage.disable_builtin_commands = stage.friend_message_needs_wake_prefix = False
    stage._umo_auto_name_recorder = SimpleNamespace(schedule=lambda _: None)
    await stage.process(selected)
    assert selected.get_extra("activated_handlers") == [handler]
    assert selected.message_str.startswith("宿舍选项 ")
    await SessionAgent(plugin.context).handle_session_control_agent(selected)
    assert not selected.is_stopped()
    await plugin.select_dorm_option(selected)
    assert next(iter(plugin.keyboard.dialogs.values())).selection.step == 1
    await submit(plugin, api, commands(api.posts[-1])[-1], "cancel", prefix="!")
    await task


async def test_reuse_and_committed_confirmation_failure_preserve_user(
    plugin, monkeypatch, location
):
    await plugin.store.bind(
        "qq-main",
        "qq-main:GroupMessage:another",
        "member-openid",
        "Owner",
        True,
        "qq_official",
        location,
    )
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    command = commands(api.posts[-1])[0]
    assert "复用已有绑定" in api.posts[-1]["markdown"]["content"]
    api.fail_send = True
    await submit(plugin, api, command, "reuse-selection")
    await task
    await plugin.retirer.drain()
    assert await plugin.store.get_binding(*plugin._identity(event)) is not None
    assert not any(e[0] == "delete" for e in api.events)


async def test_table_keyboard_and_typed_input_mix_and_no_callback_user_deletion(
    plugin, monkeypatch
):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    await submit(plugin, api, commands(api.posts[-1])[0], "campus-choice")
    data = button(api.posts[-1], "粤海校区宿舍")["action"]["data"]
    await client.on_interaction_create(interaction(data))
    # Page and return actions use the same table route as item selection.
    dialog = next(iter(plugin.keyboard.dialogs.values()))
    index = next(k for k, v in dialog.actions.items() if v == "下一页")
    await submit(plugin, api, f"/宿舍选项 {dialog.token}:{dialog.revision}:{index}", "page-choice")
    assert "第 2/2 页" in api.posts[-1]["markdown"]["content"]
    back = await incoming(api, "返回", "back-choice")
    await SessionAgent(plugin.context).handle_session_control_agent(back)
    await submit(plugin, api, commands(api.posts[-1])[-1], "cancel-choice")
    await task
    await plugin.retirer.drain()
    deleted = [e[1].rsplit("/", 1)[-1] for e in api.events if e[0] == "delete"]
    assert {"campus-choice", "page-choice", "back-choice", "cancel-choice"} <= set(deleted)
    assert "ROBOT-command-token" not in deleted


async def test_expired_command_after_reload_has_no_side_effect(plugin, monkeypatch):
    api, event, client, task = await setup_dialog(plugin, monkeypatch)
    command = commands(api.posts[-1])[0]
    await submit(plugin, api, commands(api.posts[-1])[-1], "cancel")
    await task
    await plugin.retirer.drain()
    before = list(api.events)
    # A fresh bridge has no in-memory flows, just like plugin reload.
    from importlib import import_module

    module = import_module(plugin_module.__package__ + ".szu_electricity.qq_interactions")
    fresh = module.KeyboardBridge(plugin.context, plugin.logger)
    old_event = await incoming(api, command.removeprefix("/"), "expired")
    await fresh.handle_command(old_event, plugin._argument(old_event), plugin._reply)
    assert "已失效" in api.posts[-1]["markdown"]["content"]
    assert [e for e in api.events if e[0] == "delete"] == [e for e in before if e[0] == "delete"]
    await fresh.close()
