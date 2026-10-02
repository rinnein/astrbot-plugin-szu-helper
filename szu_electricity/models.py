from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
RATE = 0.61
DEFAULT_LOW_POWER_THRESHOLD = 5.0
CACHE_TTL_SECONDS = 2 * 60 * 60
MAX_READING_AGE = timedelta(hours=48)


class ElectricityError(Exception):
    """A safe, user-facing electricity service error."""


def today() -> date:
    return local_now().date()


def local_now() -> datetime:
    return datetime.now(SHANGHAI)


def number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(str(value).strip().replace(",", ""))
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def timestamp(value: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("/", "-"))
        return dt.replace(tzinfo=SHANGHAI) if dt.tzinfo is None else dt.astimezone(SHANGHAI)
    except (ValueError, TypeError):
        return None


def building_name(value: str) -> str:
    return " ".join(value.split()).rstrip("#").strip()


@dataclass(frozen=True)
class Location:
    # Camel-case field names are the existing Rust/TypeScript share contract.
    campusId: str
    areaId: str
    buildingId: str
    buildingName: str
    roomName: str

    def __post_init__(self):
        for v in asdict(self).values():
            if (
                not isinstance(v, str)
                or not v.strip()
                or len(v.encode("utf-8")) > 128
                or any(unicodedata.category(c).startswith("C") for c in v)
            ):
                raise ElectricityError("宿舍配置字段无效。")
        if len(self.roomName.encode("utf-8")) > 32:
            raise ElectricityError("宿舍号过长。")
        object.__setattr__(self, "roomName", self.roomName.strip())

    @property
    def key(self) -> str:
        # Never merge identical building IDs in different campuses/areas.
        return json.dumps(
            [self.campusId, self.areaId, self.buildingId, self.roomName],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, value: object) -> Location:
        if not isinstance(value, dict):
            raise ElectricityError("宿舍配置内容无效。")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ElectricityError("宿舍配置字段不完整或包含未知字段。") from exc


@dataclass(frozen=True)
class Option:
    id: str
    name: str


@dataclass
class Area:
    id: str
    name: str
    campus_id: str
    buildings: list[Option] = field(default_factory=list)


@dataclass
class Catalog:
    campuses: list[Option]
    areas: list[Area]

    def validate(self, location: Location) -> Location:
        for area in self.areas:
            if area.id != location.areaId or area.campus_id != location.campusId:
                continue
            for b in area.buildings:
                if b.id == location.buildingId and b.name == building_name(location.buildingName):
                    return Location(location.campusId, area.id, b.id, b.name, location.roomName)
        raise ElectricityError("校区、宿舍区域或楼栋与当前宿舍选项不匹配。")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> Catalog:
        return cls(
            [Option(**c) for c in value["campuses"]],
            [
                Area(**{**a, "buildings": [Option(**b) for b in a["buildings"]]})
                for a in value["areas"]
            ],
        )


@dataclass(frozen=True)
class Window:
    begin: date
    end: date

    @classmethod
    def for_days(cls, days: int, end: date | None = None) -> Window:
        end = end or today()
        return cls(end - timedelta(days=days - 1), end)

    @property
    def days(self) -> int:
        return (self.end - self.begin).days + 1

    def chunks(self, limit: int = 20) -> list[Window]:
        result = []
        start = self.begin
        while start <= self.end:
            end = min(start + timedelta(days=limit - 1), self.end)
            result.append(Window(start, end))
            start = end + timedelta(days=1)
        return result


@dataclass(frozen=True)
class Reading:
    at: datetime
    remaining: float | None = None
    cumulative: float | None = None
    daily: float | None = None


@dataclass
class ProviderResult:
    readings: list[Reading]
    remaining: float | None = None
    observed_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "readings": [{**asdict(row), "at": row.at.isoformat()} for row in self.readings],
            "remaining": self.remaining,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }

    @classmethod
    def from_dict(cls, value: dict) -> ProviderResult:
        return cls(
            [
                Reading(**{**row, "at": datetime.fromisoformat(row["at"])})
                for row in value["readings"]
            ],
            value["remaining"],
            datetime.fromisoformat(value["observed_at"]) if value["observed_at"] else None,
        )


@dataclass(frozen=True)
class Report:
    location: Location
    source: str
    window: Window
    remaining: float | None
    observed_at: datetime | None
    expired: bool
    total_used: float | None
    daily_average: float | None
    valid_days: int
    estimated_days: float | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class Binding:
    id: int
    platform_id: str
    origin: str
    sender_id: str
    sender_name: str
    is_group: bool
    platform_name: str
    location: Location
