import math
import re
from dataclasses import dataclass

from .models import DEFAULT_LOW_POWER_THRESHOLD, ElectricityError


@dataclass(frozen=True)
class Settings:
    source: str
    enabled: bool
    hour: int
    minute: int
    low_power_threshold: float

    @classmethod
    def parse(cls, config):
        source = config.get("data_source", "official")
        enabled = config.get("daily_check_enabled", True)
        time = config.get("daily_check_time", "08:00")
        threshold = config.get("low_power_threshold", DEFAULT_LOW_POWER_THRESHOLD)
        if source not in ("official", "iotun"):
            raise ElectricityError("data_source 必须为 official 或 iotun。")
        if not isinstance(enabled, bool):
            raise ElectricityError("daily_check_enabled 必须为布尔值。")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or threshold <= 0
        ):
            raise ElectricityError("low_power_threshold 必须为大于 0 的有限数值（单位：度）。")
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", time):
            raise ElectricityError("daily_check_time 必须使用 24 小时制 HH:mm，例如 08:00。")
        hour, minute = map(int, time.split(":"))
        return cls(source, enabled, hour, minute, float(threshold))
