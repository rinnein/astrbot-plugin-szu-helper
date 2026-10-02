import logging
from dataclasses import replace
from datetime import datetime, timedelta

from test_cache import Source

from szu_electricity.models import SHANGHAI, ProviderResult
from szu_electricity.service import ElectricityService


async def test_service_uses_injected_plugin_logger_without_exposing_location(
    caplog, store, location
):
    location = replace(location, roomName="PRIVATE_ROOM_TOKEN")
    logger = logging.getLogger("astrbot.plugin.szu-log-test")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    source = Source()
    service = ElectricityService(source, store, logger=logger)
    await service.catalog()
    await service.query(location)
    await service.query(location)
    messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name == logger.name
    )
    assert "source=official" in messages and "observed_at=" in messages
    assert "用电缓存命中" in messages
    assert location.roomName not in messages and location.buildingName not in messages
    assert location.key not in messages
    assert service.logger is logger
    await service.close()


async def test_unusable_reading_logs_actionable_reason(caplog, store, location):
    location = replace(location, roomName="PRIVATE_ROOM_TOKEN")
    logger = logging.getLogger("astrbot.plugin.szu-stale-test")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    source = Source()

    async def query(location, window):
        return ProviderResult([], 88, datetime.now(SHANGHAI) - timedelta(hours=49))

    source.query = query
    service = ElectricityService(source, store, logger=logger)
    report = await service.query(location)
    assert report.expired
    messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name == logger.name
    )
    assert "reason=stale" in messages and "observed_at=" in messages
    assert location.roomName not in messages
    await service.close()
