from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .catalog import SIMS_CLIENTS, empty_catalog, iotun_catalog, iotun_target
from .models import (
    SHANGHAI,
    Catalog,
    ElectricityError,
    Location,
    Option,
    ProviderResult,
    Reading,
    Window,
    building_name,
    number,
    timestamp,
)

SIMS_BASE = "http://192.168.84.3:9090"
LIHU_BASE = "http://172.25.100.105:8010/"
IOTUN_BASE = "https://www.iotun.com"
MAX_BODY = 2 * 1024 * 1024


def new_client(*, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=timeout or httpx.Timeout(12, connect=3),
        headers={"User-Agent": "astrbot-plugin-szu-helper/0.1.0"},
    )


def new_iotun_client() -> httpx.AsyncClient:
    # Public HTTPS includes DNS and TLS; the campus portal's 3 s connect budget
    # was also being applied here and could abort otherwise healthy connections.
    return new_client(timeout=httpx.Timeout(30, connect=10))


def same_origin(base: str, action: str) -> str:
    url = urljoin(base, action)
    a, b = urlsplit(base), urlsplit(url)
    if (a.scheme, a.hostname, a.port) != (b.scheme, b.hostname, b.port) or b.username:
        raise ElectricityError("电费门户返回了非预期地址。")
    return url


async def request(
    client: httpx.AsyncClient, method: str, url: str, *, source: str = "official", **kwargs
) -> httpx.Response:
    origin = url
    endpoint = urlsplit(origin)
    label = (
        f"iotun 公网接口（{endpoint.hostname}{endpoint.path}）"
        if source == "iotun"
        else "学校官方电费接口"
    )
    try:
        for _ in range(6):
            async with client.stream(method, url, **kwargs) as response:
                if response.is_redirect:
                    url = same_origin(
                        origin, urljoin(str(response.url), response.headers["location"])
                    )
                    # A query POST often redirects to an ASP.NET records page.
                    if response.status_code == 303 or (
                        response.status_code in (301, 302) and method == "POST"
                    ):
                        method, kwargs = "GET", {}
                    else:
                        kwargs.pop("params", None)
                    continue
                response.raise_for_status()
                content = bytearray()
                async for block in response.aiter_bytes():
                    content.extend(block)
                    if len(content) > MAX_BODY:
                        raise ElectricityError("电费接口响应过大。")
                # aiter_bytes() has already decompressed gzip/deflate. Keeping
                # the wire encoding header would make Response decode it again.
                headers = httpx.Headers(response.headers)
                for name in ("content-encoding", "content-length", "transfer-encoding"):
                    headers.pop(name, None)
                return httpx.Response(
                    response.status_code,
                    headers=headers,
                    content=bytes(content),
                    request=response.request,
                )
        raise ElectricityError("电费门户重定向次数过多。")
    except httpx.HTTPStatusError as exc:
        raise ElectricityError(
            f"{label}返回 HTTP {exc.response.status_code}，请稍后重试。"
        ) from exc
    except httpx.RequestError as exc:
        if isinstance(exc, httpx.DecodingError):
            reason = "响应解压失败"
        elif isinstance(exc, httpx.ConnectTimeout):
            reason = "建立连接超时"
        elif isinstance(exc, httpx.ReadTimeout):
            reason = "等待响应超时"
        elif isinstance(exc, httpx.TimeoutException):
            reason = "请求超时"
        else:
            reason = f"连接失败（{type(exc).__name__}）"
        hint = (
            "请检查 AstrBot 所在主机到 www.iotun.com 的公网连通性。"
            if source == "iotun"
            else "官方数据源需要校园网可达，请检查部署网络或在后台切换数据源。"
        )
        raise ElectricityError(f"{label}{reason}；{hint}") from exc


def html(response: httpx.Response) -> BeautifulSoup:
    declared = re.search(
        r"charset\s*=\s*[\"']?([\w-]+)", response.headers.get("content-type", ""), re.I
    )
    if not declared:
        declared = re.search(
            r"charset\s*=\s*[\"']?([\w-]+)", response.content[:2048].decode("ascii", "ignore"), re.I
        )
    charset = declared.group(1).lower() if declared else "gb18030"
    if charset in ("gb2312", "gbk"):
        charset = "gb18030"
    try:
        text = response.content.decode(charset)
    except (LookupError, UnicodeError):
        text = response.content.decode("gb18030", errors="replace")
    return BeautifulSoup(text, "html.parser")


def options(page: BeautifulSoup, name: str) -> list[Option]:
    select = page.find("select", attrs={"name": name})
    if not select:
        raise ElectricityError("电费门户未返回预期的宿舍选项。")
    result = []
    for option in select.find_all("option"):
        value, label = str(option.get("value", "")).strip(), building_name(option.get_text())
        if value and label and label not in ("请选择", "楼栋"):
            result.append(Option(value, label))
    return result


def fields(page: BeautifulSoup) -> dict[str, str]:
    return {
        str(e["name"]): str(e.get("value", "")) for e in page.select("input[type=hidden][name]")
    }


def action(page: BeautifulSoup, base: str, default: str) -> str:
    form = page.find("form", action=True)
    return same_origin(base, str(form["action"]) if form else default)


async def post(
    client: httpx.AsyncClient, url: str, page: BeautifulSoup, values: dict
) -> httpx.Response:
    payload = fields(page) | values
    return await request(
        client,
        "POST",
        url,
        content=urlencode(payload, encoding="gb18030").encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": url},
    )


def parse_sims(page: BeautifulSoup, room: str) -> list[Reading]:
    table = page.select_one("#oTable")
    if table is None:
        raise ElectricityError("无法识别 SIMS 用电记录表格。")
    result = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 6 or (at := timestamp(cells[5])) is None:
            continue
        if cells[1] and cells[1] != room:
            raise ElectricityError("电费接口返回的宿舍号不匹配。")
        result.append(Reading(at, number(cells[2]), number(cells[3])))
    return result


def parse_lihu(page: BeautifulSoup) -> list[Reading]:
    for table in page.find_all("table"):
        rows = [
            [c.get_text(strip=True) for c in tr.find_all(["td", "th"], recursive=False)]
            for tr in table.find_all("tr")
        ]
        for pos, row in enumerate(rows):
            labels = [re.sub(r"\s+", "", c).replace("（", "(").replace("）", ")") for c in row]
            if "日期" not in labels or "用量(度)" not in labels:
                continue
            di, ui = labels.index("日期"), labels.index("用量(度)")
            result = []
            for cells in rows[pos + 1 :]:
                if len(cells) > max(di, ui) and (at := timestamp(cells[di])) is not None:
                    result.append(Reading(at, daily=number(cells[ui])))
            return result
    raise ElectricityError("无法识别丽湖每日用电表格。")


def lihu_remaining(page: BeautifulSoup) -> float | None:
    match = re.search(r"剩余电量\s*[：:]\s*([-+\d.,]+)", page.get_text(" ", strip=True))
    return number(match.group(1)) if match else None


class OfficialProvider:
    name = "official"

    def __init__(self, client_factory: Callable = new_client):
        self.client_factory = client_factory

    async def catalog(self) -> Catalog:
        catalog = empty_catalog()
        # Sequential station selection: these portals store active state in cookies.
        async with self.client_factory() as client:
            for area in catalog.areas:
                if area.id in SIMS_CLIENTS:
                    response = await request(
                        client,
                        "GET",
                        f"{SIMS_BASE}/cgcSims/login.do",
                        params={"task": "station", "client": SIMS_CLIENTS[area.id]},
                    )
                    area.buildings = options(html(response), "buildingId")
                else:
                    area.buildings = options(
                        html(await request(client, "GET", LIHU_BASE)), "drlouming"
                    )
        return catalog

    async def query(self, location: Location, window: Window) -> ProviderResult:
        rows = []
        balance, observed = None, None
        for chunk in window.chunks():
            # Each operation owns its cookie jar; no shared room/station state.
            async with self.client_factory() as client:
                if location.areaId == "xili-lihuo-phase2":
                    result = await self._lihu(client, location, chunk)
                else:
                    result = await self._sims(client, location, chunk)
                rows.extend(result.readings)
                if result.remaining is not None:
                    balance, observed = result.remaining, result.observed_at
        return ProviderResult(rows, balance, observed)

    async def _sims(
        self, client: httpx.AsyncClient, location: Location, window: Window
    ) -> ProviderResult:
        client_ip = SIMS_CLIENTS.get(location.areaId)
        if client_ip is None:
            raise ElectricityError("未知的 SIMS 宿舍区域。")
        url = f"{SIMS_BASE}/cgcSims/login.do"
        station = html(
            await request(client, "GET", url, params={"task": "station", "client": client_ip})
        )
        response = await post(
            client,
            action(station, url, url),
            station,
            {
                "client": client_ip,
                "buildingId": location.buildingId,
                "buildingName": location.buildingName,
                "roomName": location.roomName,
                "select": " 查询 ",
            },
        )
        room = html(response)
        hidden = fields(room)
        if not hidden.get("roomId"):
            raise ElectricityError("未找到该宿舍，请检查楼栋和宿舍号。")
        query_url = action(room, str(response.url), "/cgcSims/selectList.do")
        page = html(
            await post(
                client,
                query_url,
                room,
                {
                    "hiddenType": "",
                    "isHost": "0",
                    "beginTime": str(window.begin),
                    "endTime": str(window.end),
                    "type": "2",
                    "client": client_ip,
                    "roomId": hidden["roomId"],
                    "roomName": location.roomName,
                    "building": hidden.get("building", location.buildingName),
                },
            )
        )
        return ProviderResult(parse_sims(page, location.roomName))

    async def _lihu(
        self, client: httpx.AsyncClient, location: Location, window: Window
    ) -> ProviderResult:
        digits = location.roomName
        if not re.fullmatch(r"[0-9]{3,5}", digits):
            raise ElectricityError("丽湖二期宿舍号应为包含楼层的 3～5 位数字，例如 501。")
        floor = location.buildingId + digits[:-2].zfill(2)
        response = await request(client, "GET", LIHU_BASE)
        home_url, home = str(response.url), html(response)
        building = html(
            await post(
                client,
                home_url,
                home,
                {
                    "__EVENTTARGET": "drlouming",
                    "__EVENTARGUMENT": "",
                    "drlouming": location.buildingId,
                },
            )
        )
        rooms = html(
            await post(
                client,
                home_url,
                building,
                {
                    "__EVENTTARGET": "drceng",
                    "__EVENTARGUMENT": "",
                    "drlouming": location.buildingId,
                    "drceng": floor,
                },
            )
        )
        matches = [o for o in options(rooms, "drfangjian") if o.name.endswith(digits)]
        if len(matches) != 1:
            raise ElectricityError("未找到唯一匹配的丽湖宿舍，请检查宿舍号。")
        response = await post(
            client,
            home_url,
            rooms,
            {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "drlouming": location.buildingId,
                "drceng": floor,
                "drfangjian": matches[0].id,
                "radio": "usedR",
                "ImageButton1.x": "20",
                "ImageButton1.y": "10",
            },
        )
        landing = html(response)
        url = action(landing, str(response.url), urljoin(LIHU_BASE, "usedRecord.aspx"))
        first = html(
            await post(
                client,
                url,
                landing,
                {
                    "txtstart": str(window.begin),
                    "txtend": str(window.end),
                    "btnser": "查询",
                },
            )
        )
        match = re.search(r"共\s*(\d+)\s*页", first.get_text(" ", strip=True))
        count = int(match.group(1)) if match else 1
        if not 1 <= count <= 100:
            raise ElectricityError("丽湖接口分页数量异常。")
        rows = parse_lihu(first)
        for n in range(2, count + 1):
            page = html(
                await request(
                    client,
                    "GET",
                    url,
                    params={
                        "p": str(n),
                        "txtstart": str(window.begin),
                        "txtend": str(window.end),
                    },
                    headers={"Referer": url},
                )
            )
            rows.extend(parse_lihu(page))
        remaining = lihu_remaining(landing)
        if remaining is None:
            remaining = lihu_remaining(first)
        return ProviderResult(
            rows, remaining, datetime.now(SHANGHAI) if remaining is not None else None
        )


class IotunProvider:
    name = "iotun"

    def __init__(self, client_factory: Callable = new_iotun_client):
        self.client_factory = client_factory

    async def _get(self, path: str, params: dict | None = None):
        # Retry this same public endpoint once for transient network/gateway
        # failures. No request falls back to a school endpoint or another source.
        for attempt in range(2):
            try:
                async with self.client_factory() as client:
                    response = await request(
                        client, "GET", IOTUN_BASE + path, source=self.name, params=params
                    )
                break
            except ElectricityError as exc:
                cause = exc.__cause__
                transient = isinstance(
                    cause, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
                ) or (
                    isinstance(cause, httpx.HTTPStatusError)
                    and cause.response.status_code in (502, 503, 504)
                )
                if attempt or not transient:
                    raise
                await asyncio.sleep(0.5)
        try:
            value = response.json()
            if not isinstance(value, dict) or value.get("ok") is not True:
                if isinstance(value, dict) and value.get("error_code") == "ROOM_NOT_FOUND":
                    raise ElectricityError("iotun 未找到该宿舍，请检查宿舍配置。")
                raise ElectricityError("iotun 查询失败，请稍后重试。")
            return value["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ElectricityError("iotun 返回的数据格式异常。") from exc

    async def catalog(self) -> Catalog:
        return iotun_catalog(await self._get("/api/buildings"))

    async def query(self, location: Location, window: Window) -> ProviderResult:
        group, bid = iotun_target(location)
        data = await self._get(
            "/api/status",
            {
                "client": group,
                "buildingId": bid,
                "buildingName": location.buildingName,
                "roomName": location.roomName,
                "days": str(window.days),
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("trend", []), list):
            raise ElectricityError("iotun 返回的用电数据格式异常。")
        rows = []
        direct = location.areaId == "xili-lihuo-phase2" or group == "yuehai_sftest"
        for row in data.get("trend", []):
            if not isinstance(row, dict):
                raise ElectricityError("iotun 返回的读数格式异常。")
            at = timestamp(row.get("date"))
            if at is None or not window.begin <= at.date() <= window.end:
                continue
            rows.append(
                Reading(
                    at,
                    None if direct else number(row.get("remaining")),
                    None if direct else number(row.get("total_used_kwh")),
                    number(row.get("daily_used_kwh")) if direct else None,
                )
            )
        # Never use reconstructed historical apartment balances as observations.
        return ProviderResult(
            rows, number(data.get("remaining")), timestamp(data.get("last_record"))
        )
