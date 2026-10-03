"""Run manually with an installed AstrBot; no real platform is contacted."""

# ruff: noqa: E402
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

repo = Path(__file__).resolve().parents[1]
runtime = Path(tempfile.mkdtemp(prefix="szu-loader-"))
os.environ["ASTRBOT_ROOT"] = str(runtime)
os.chdir(runtime)
sys.path.insert(0, str(runtime))
path = runtime / "data/plugins/astrbot_plugin_szu_helper"
shutil.copytree(
    repo,
    path,
    ignore=shutil.ignore_patterns(
        ".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "tests", "data"
    ),
)
(runtime / "data/config").mkdir(parents=True, exist_ok=True)
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context
from astrbot.core.log import LogManager
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    QQOfficialPlatformAdapter,
    botClient,
)
from astrbot.core.star.star import star_registry
from astrbot.core.star.star_manager import PluginManager


async def main():
    config = AstrBotConfig(str(runtime / "data/cmd_config.json"))
    context = Context(asyncio.Queue(), config, None, None, None, None, None, None, None, None, None)
    # A real QQ adapter/client with no credentials or open network connection.
    adapter = object.__new__(QQOfficialPlatformAdapter)
    adapter.config = {"id": "smoke-qq"}
    client = object.__new__(botClient)
    client.intents = 0
    client._active_websockets = []
    client._connection = SimpleNamespace(_connect=client.bot_connect)
    adapter.client = client
    context.platform_manager = SimpleNamespace(platform_insts=[adapter])
    manager = PluginManager(context, config)
    ok, err = await manager.load(specified_dir_name="astrbot_plugin_szu_helper")
    assert ok, err
    meta = next(s for s in star_registry if s.name == "astrbot_plugin_szu_helper")
    old = meta.star_cls
    assert old._ready and len(old.scheduler.get_jobs()) == 1
    assert len(meta.star_handler_full_names) == 7
    first_hook = old.keyboard.hooks["smoke-qq"]
    assert client.intents & (1 << 26)
    assert client._connection._connect is client.bot_connect
    assert not first_hook.ready
    await old.on_platform_loaded()
    assert old.keyboard.hooks["smoke-qq"] is first_hook
    if hasattr(LogManager, "get_plugin_logger"):
        import logging

        assert old.logger.name == "astrbot.plugin.astrbot_plugin_szu_helper"
        assert old.service.logger is old.monitor.logger is old.logger
        assert old.service.provider.logger is old.logger
        LogManager.set_plugin_log_level(meta.name, "DEBUG")
        assert old.service.logger.isEnabledFor(logging.DEBUG)
        LogManager.set_plugin_log_level(meta.name, "WARNING")
        assert not old.service.logger.isEnabledFor(logging.INFO)
        LogManager.set_plugin_log_level(meta.name, None)
    old.config["daily_check_time"] = "21:30"
    old.config["low_power_threshold"] = 7.5
    old.config["detail_layout"] = "list"
    old.config.save_config()
    ok, err = await manager.reload("astrbot_plugin_szu_helper")
    assert ok, err
    new = next(s for s in star_registry if s.name == "astrbot_plugin_szu_helper").star_cls
    await asyncio.sleep(0)
    assert new is not old and not old.scheduler.running and old.store.db is None
    assert "21" in str(new.scheduler.get_jobs()[0].trigger)
    assert new.monitor.threshold == 7.5 and new.config["detail_layout"] == "list"
    assert not first_hook.active
    assert client.on_interaction_create is new.keyboard.hooks["smoke-qq"].wrapper
    await new.terminate()
    await asyncio.sleep(0)
    assert all(
        name not in vars(client)
        for name in (
            "on_interaction_create",
            "bot_connect",
            "on_ready",
            "on_resumed",
        )
    )
    assert client._connection._connect == client.bot_connect
    print("REAL_PLUGIN_MANAGER_LOAD_CONFIG_RELOAD_UNLOAD_OK", runtime)


asyncio.run(main())
