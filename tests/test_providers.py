from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from szu_electricity.models import ElectricityError, Location, Window
from szu_electricity.providers import MAX_BODY, IotunProvider, OfficialProvider, request


def response(text):
    return httpx.Response(
        200, content=text.encode("gb18030"), headers={"Content-Type": "text/html; charset=gb2312"}
    )


def factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_sims_gb_form_split_cookies_and_records(location):
    windows = []

    def handler(req):
        if req.method == "GET":
            return response(
                '<form action="login.do"><select name="buildingId"><option value="54">山茶斋</option></select></form>'
            )
        form = parse_qs(req.content.decode("ascii"), encoding="gb18030")
        if req.url.path.endswith("login.do"):
            assert form["buildingName"] == ["山茶斋"]
            assert form["roomName"] == ["0601"]
            return response(
                '<form action="selectList.do"><input type="hidden" name="roomId" value="42"></form>'
            )
        assert form["roomId"] == ["42"]
        windows.append((form["beginTime"][0], form["endTime"][0]))
        return response(
            '<table id="oTable"><tr><td>1</td><td>0601</td><td>4.25</td><td>12</td><td>50</td><td>2026-10-01 12:00:00</td></tr></table>'
        )

    provider = OfficialProvider(factory(handler))
    data = await provider.query(location, Window.for_days(31, date(2026, 10, 2)))
    assert windows == [("2026-09-02", "2026-09-21"), ("2026-09-22", "2026-10-02")]
    assert data.readings[0].remaining == 4.25
    windows.clear()
    await provider.query(location, Window.for_days(3, date(2026, 10, 2)))
    assert windows == [("2026-09-30", "2026-10-02")]


async def test_lihu_hidden_fields_zero_and_pagination():
    calls = []

    def handler(req):
        form = (
            parse_qs(req.content.decode("ascii"), encoding="gb18030")
            if req.method == "POST"
            else {}
        )
        calls.append(req)
        if req.method == "GET" and req.url.path == "/":
            return response('<input type="hidden" name="__VIEWSTATE" value="home">')
        if form.get("__EVENTTARGET") == ["drlouming"]:
            assert form["__VIEWSTATE"] == ["home"]
            return response('<input type="hidden" name="__VIEWSTATE" value="building">')
        if form.get("__EVENTTARGET") == ["drceng"]:
            assert form["__VIEWSTATE"] == ["building"] and form["drceng"] == ["0105"]
            return response(
                '<input type="hidden" name="__VIEWSTATE" value="rooms"><select name="drfangjian"><option value="010501">501</option></select>'
            )
        if "ImageButton1.x" in form:
            assert form["__VIEWSTATE"] == ["rooms"]
            return response(
                '<form action="usedRecord.aspx"><input type="hidden" name="__VIEWSTATE" value="landing"></form>剩余电量：<span>0.00</span>'
            )
        if req.method == "POST":
            assert form["__VIEWSTATE"] == ["landing"]
            assert form["txtstart"] == ["2026-09-30"]
        else:
            assert req.url.params["p"] == "2" and req.url.params["txtstart"] == "2026-09-30"
        return response(
            "<table><tr><td>日期</td><td>用量(度)</td></tr><tr><td>2026-10-01</td><td>2.5</td></tr></table>第1页/共2页"
        )

    location = Location("xili", "xili-lihuo-phase2", "01", "梧桐树", "501")
    data = await OfficialProvider(factory(handler)).query(
        location, Window.for_days(3, date(2026, 10, 2))
    )
    assert data.remaining == 0 and data.observed_at is not None
    assert len(data.readings) == 2 and data.readings[0].daily == 2.5
    assert len(calls) == 6


async def test_iotun_short_parameter_mapping_and_no_fake_balance(location):
    def handler(req):
        assert req.url.params["days"] == "3"
        assert req.url.params["client"] == "yuehai_sftest"
        assert req.url.params["buildingId"] == "03"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "remaining": 0,
                    "last_record": "2026-10-02",
                    "trend": [
                        {"date": "2026-09-28", "remaining": 50, "daily_used_kwh": 5},
                        {"date": "2026-10-01", "remaining": 20, "daily_used_kwh": 2},
                    ],
                },
            },
        )

    data = await IotunProvider(factory(handler)).query(
        location, Window.for_days(3, date(2026, 10, 2))
    )
    assert data.remaining == 0
    assert len(data.readings) == 1 and data.readings[0].daily == 2
    assert data.readings[0].remaining is None


async def test_http_limits_redirects_and_bad_json():
    async with factory(
        lambda r: httpx.Response(302, headers={"Location": "http://evil.invalid/"})
    )() as client:
        with pytest.raises(ElectricityError, match="非预期"):
            await request(client, "GET", "http://192.168.84.3:9090")
    async with factory(lambda r: httpx.Response(200, content=b"x" * (MAX_BODY + 1)))() as client:
        with pytest.raises(ElectricityError, match="过大"):
            await request(client, "GET", "http://192.168.84.3:9090")
    with pytest.raises(ElectricityError, match="格式异常"):
        await IotunProvider(factory(lambda r: httpx.Response(200, text="not json"))).catalog()
    with pytest.raises(ElectricityError, match="HTTP 503"):
        await IotunProvider(factory(lambda r: httpx.Response(503))).catalog()


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_compressed_response_is_decoded_exactly_once(encoding):
    import gzip
    import json
    import zlib

    payload = json.dumps(
        {
            "ok": True,
            "data": [{"group": "yuehai_sftest", "buildings": [{"id": "03", "name": "山茶斋"}]}],
        },
        ensure_ascii=False,
    ).encode()
    compressed = gzip.compress(payload) if encoding == "gzip" else zlib.compress(payload)

    def handler(req):
        return httpx.Response(
            200,
            stream=httpx.ByteStream(compressed),
            headers={
                "Content-Encoding": encoding,
                "Content-Length": str(len(compressed)),
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    async with factory(handler)() as client:
        result = await request(client, "GET", "https://www.iotun.com/api/buildings", source="iotun")
        assert result.content == payload
        assert result.json()["ok"] is True
        assert "content-encoding" not in result.headers
        assert int(result.headers["content-length"]) == len(payload)
    catalog = await IotunProvider(factory(handler)).catalog()
    assert catalog.areas[0].buildings[0].name == "山茶斋"


async def test_iotun_retries_same_endpoint_and_reports_actual_source():
    calls = []

    def transient(req):
        calls.append(str(req.url))
        if len(calls) == 1:
            raise httpx.ConnectTimeout("temporary TLS delay", request=req)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [{"group": "lihu", "buildings": [{"id": "01", "name": "梧桐树"}]}],
            },
        )

    catalog = await IotunProvider(factory(transient)).catalog()
    assert len(catalog.areas[4].buildings) == 1
    assert calls == ["https://www.iotun.com/api/buildings"] * 2

    calls.clear()

    def unavailable(req):
        calls.append(str(req.url))
        raise httpx.ConnectTimeout("offline", request=req)

    with pytest.raises(ElectricityError) as error:
        await IotunProvider(factory(unavailable)).catalog()
    assert len(calls) == 2
    assert "iotun 公网接口（www.iotun.com/api/buildings）建立连接超时" in str(error.value)
    assert "校园网" not in str(error.value)


async def test_uncompressed_body_limit_applies_to_compressed_responses():
    import gzip

    compressed = gzip.compress(b"x" * (MAX_BODY + 1))
    async with factory(
        lambda r: httpx.Response(
            200, stream=httpx.ByteStream(compressed), headers={"Content-Encoding": "gzip"}
        )
    )() as client:
        with pytest.raises(ElectricityError, match="过大"):
            await request(client, "GET", "https://www.iotun.com/api/buildings", source="iotun")


@pytest.mark.parametrize("area", ["yuehai-main", "xili-lihuo-phase2"])
async def test_iotun_live_balance_is_independent_of_old_usage_dates(monkeypatch, area):
    from datetime import datetime

    from szu_electricity import providers
    from szu_electricity.analytics import summarize
    from szu_electricity.models import SHANGHAI

    now = datetime(2026, 10, 3, 0, 15, tzinfo=SHANGHAI)
    monkeypatch.setattr(providers, "local_now", lambda: now)
    location = Location(
        "yuehai" if area == "yuehai-main" else "xili",
        area,
        "54" if area == "yuehai-main" else "01",
        "山茶斋" if area == "yuehai-main" else "梧桐树",
        "601",
    )
    data = {"remaining": 88, "last_record": "2026-09-29", "trend": []}
    provider = IotunProvider(
        factory(lambda req: httpx.Response(200, json={"ok": True, "data": data}))
    )
    window = Window.for_days(3, now.date())
    result = await provider.query(location, window)
    assert result.observed_at == now
    report = summarize(location, "iotun", window, result, now=now)
    assert report.remaining == 88 and not report.expired
    assert report.estimated_days is None


async def test_iotun_sims_retains_actual_old_meter_date():
    from datetime import datetime

    from szu_electricity.analytics import summarize
    from szu_electricity.models import SHANGHAI

    location = Location("yuehai", "yuehai-xinzhai", "7126", "风槐斋", "601")
    data = {"remaining": 88, "last_record": "2026-09-29 23:59:00", "trend": []}
    provider = IotunProvider(
        factory(lambda req: httpx.Response(200, json={"ok": True, "data": data}))
    )
    now = datetime(2026, 10, 3, 0, 15, tzinfo=SHANGHAI)
    window = Window.for_days(3, now.date())
    result = await provider.query(location, window)
    report = summarize(location, "iotun", window, result, now=now)
    assert report.expired and report.unavailable_reason == "stale"
