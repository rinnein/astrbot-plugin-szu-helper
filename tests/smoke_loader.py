"""Run manually with an installed AstrBot; no real platform is contacted."""

# ruff: noqa: E402
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

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
from astrbot.core.star.star import star_registry
from astrbot.core.star.star_manager import PluginManager


async def main():
    config = AstrBotConfig(str(runtime / "data/cmd_config.json"))
    context = Context(asyncio.Queue(), config, None, None, None, None, None, None, None, None, None)
    manager = PluginManager(context, config)
    ok, err = await manager.load(specified_dir_name="astrbot_plugin_szu_helper")
    assert ok, err
    meta = next(s for s in star_registry if s.name == "astrbot_plugin_szu_helper")
    old = meta.star_cls
    assert old._ready and len(old.scheduler.get_jobs()) == 1
    assert len(meta.star_handler_full_names) == 5
    old.config["daily_check_time"] = "21:30"
    old.config["low_power_threshold"] = 7.5
    old.config.save_config()
    ok, err = await manager.reload("astrbot_plugin_szu_helper")
    assert ok, err
    new = next(s for s in star_registry if s.name == "astrbot_plugin_szu_helper").star_cls
    await asyncio.sleep(0)
    assert new is not old and not old.scheduler.running and old.store.db is None
    assert "21" in str(new.scheduler.get_jobs()[0].trigger)
    assert new.monitor.threshold == 7.5
    await new.terminate()
    await asyncio.sleep(0)
    print("REAL_PLUGIN_MANAGER_LOAD_CONFIG_RELOAD_UNLOAD_OK", runtime)


asyncio.run(main())
