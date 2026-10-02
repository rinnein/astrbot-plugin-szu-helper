from datetime import date, datetime, timedelta

import pybase16384
import pytest

from szu_electricity.analytics import render, summarize
from szu_electricity.catalog import SFTEST_TO_SIMS, iotun_catalog, iotun_target
from szu_electricity.config import Settings
from szu_electricity.conversation import Selection
from szu_electricity.models import (
    SHANGHAI,
    ElectricityError,
    Location,
    ProviderResult,
    Reading,
    Window,
)
from szu_electricity.sharing import decode, encode


def at(day):
    return datetime(2026, 10, day, tzinfo=SHANGHAI)


def test_share_roundtrip_and_rust_vector(location):
    # Base16384 Rust 0.1.0's published byte-level compatibility vector.
    assert pybase16384.encode_to_string(b"12345678") == "婌焳廔萷尀㴁"
    code = encode(location)
    assert decode(" base16384: " + code + "\n") == location
    assert decode(code).roomName == "0601"
    assert b"sender" not in pybase16384.decode_from_string(code)
    assert encode(decode(code)) == code


@pytest.mark.parametrize(
    "code", ["", "abc", "㴁", "一㴆", "a" * 9000, "一" * 3000, "一㴁", "一一㴂", "㴁一", "😀"]
)
def test_bad_shares(code):
    with pytest.raises(ElectricityError):
        decode(code)


@pytest.mark.parametrize(
    "data",
    [
        b'{"version":2,"location":{}}',
        b'{"version":true,"location":{}}',
        b"[]",
        b"null",
        b'{"version":1,"location":{},"url":"bad"}',
    ],
)
def test_bad_share_content(data):
    with pytest.raises(ElectricityError):
        decode(pybase16384.encode_to_string(data))


def test_catalog_mapping_and_isolated_ids():
    data = [
        {
            "group": "yuehai_sftest",
            "buildings": [{"id": k, "name": "name" + k} for k in SFTEST_TO_SIMS],
        },
        {"group": "yuehai_north", "buildings": [{"id": "6875", "name": "乔森阁"}]},
        {"group": "yuehai_south", "buildings": [{"id": "6875", "name": "春笛"}]},
        {"group": "yuehai_newzhai", "buildings": [{"id": "7126", "name": "风槐斋"}]},
        {
            "group": "lihu",
            "buildings": [{"id": "10057", "name": "A栋风信子"}, {"id": "01", "name": "梧桐树#"}],
        },
    ]
    c = iotun_catalog(data)
    assert len(c.areas) == 5
    for source, target in SFTEST_TO_SIMS.items():
        location = c.validate(Location("yuehai", "yuehai-main", target, "name" + source, "101"))
        assert iotun_target(location) == ("yuehai_sftest", source)
    assert c.areas[4].buildings[0].name == "梧桐树"
    assert c.areas[0].buildings[-1].id == c.areas[2].buildings[0].id
    assert iotun_target(Location("xili", "xili-lihuo-phase2", "01", "梧桐树", "501")) == (
        "lihu",
        "01",
    )


def test_selection_paging_back_and_preserves_zeros(catalog):
    s = Selection(catalog)
    assert "北校区" in s.prompt()
    s.accept("1")
    s.accept("1")
    s.accept("下一页")
    assert "第 2/2 页" in s.prompt()
    s.accept("上一页")
    s.accept("1")
    with pytest.raises(ElectricityError):
        s.accept(" ")
    assert s.accept("0601").roomName == "0601"
    s.accept("返回")
    assert s.step == 2
    with pytest.raises(ElectricityError):
        s.accept("999")


def test_windows_and_stats(location):
    window = Window.for_days(31, date(2026, 10, 3))
    chunks = window.chunks()
    assert [w.days for w in chunks] == [20, 11]
    assert chunks[0].end + timedelta(days=1) == chunks[1].begin
    data = ProviderResult([Reading(at(1), 8, 10), Reading(at(2), 6, 12), Reading(at(3), 4, 14)])
    short = summarize(location, "official", Window.for_days(3, date(2026, 10, 3)), data)
    long = summarize(location, "official", window, data)
    assert short.estimated_days == long.estimated_days == 2
    assert short.daily_average == 2 and short.valid_days == 2
    assert "2.44 元" in render(short)
    assert "有效用量" in render(long, True)


def test_gaps_resets_duplicate_stale_and_zero(location):
    window = Window.for_days(31, date(2026, 10, 6))
    rows = [
        Reading(at(1), 10, 10),
        Reading(at(2), 9, 11),
        Reading(at(2).replace(hour=20), 7, 13),
        Reading(at(4), 6, 14),
        Reading(at(5), 4, 2),
        Reading(at(6), 4, 2),
    ]
    r = summarize(location, "official", window, ProviderResult(rows))
    assert r.valid_days == 2 and r.total_used == 3
    assert r.estimated_days is None
    r = summarize(location, "official", window, ProviderResult(rows[:2]))
    assert r.expired and "过期" in render(r)
    r = summarize(location, "iotun", window, ProviderResult([Reading(at(6), daily=3)], 0, at(6)))
    assert r.remaining == 0 and r.estimated_days == 0 and r.total_used == 3
    r = summarize(
        location, "iotun", window, ProviderResult([Reading(at(6), daily=float("nan"))], 4, at(6))
    )
    assert r.total_used is None and r.estimated_days is None


@pytest.mark.parametrize("time", ["24:00", "8:00", "12:60", "-1:00", "08:00\n", " 08:00"])
def test_config_rejects_invalid_time(time):
    with pytest.raises(ElectricityError):
        Settings.parse({"daily_check_time": time})


def test_config_defaults():
    assert Settings.parse({}) == Settings("official", True, 8, 0, 5.0)
    assert Settings.parse({"daily_check_time": "23:59"}).minute == 59


def test_full_share_vector_verified_by_rust():
    import json
    from pathlib import Path

    fixture = json.loads((Path(__file__).parent / "fixtures/share-v1.json").read_text())
    location = Location.from_dict(fixture["location"])
    assert encode(location) == fixture["code"]
    assert decode(fixture["code"]) == location


@pytest.mark.parametrize("threshold", [0, -1, True, "7.5", None, float("nan"), float("inf")])
def test_config_rejects_invalid_threshold(threshold):
    with pytest.raises(ElectricityError, match="low_power_threshold"):
        Settings.parse({"low_power_threshold": threshold})


def test_config_accepts_decimal_threshold():
    assert Settings.parse({"low_power_threshold": 7.5}).low_power_threshold == 7.5


def test_reference_project_sharing_vectors():
    import json
    from pathlib import Path

    from szu_electricity.catalog import empty_catalog
    from szu_electricity.models import Option

    proof = json.loads((Path(__file__).parent / "fixtures/reference-share-v1.json").read_text())
    assert len(proof["vectors"]) == 12
    catalog = empty_catalog()
    for vector in proof["vectors"]:
        expected = Location.from_dict(vector["location"])
        area = next(a for a in catalog.areas if a.id == expected.areaId)
        option = Option(expected.buildingId, expected.buildingName)
        if option not in area.buildings:
            area.buildings.append(option)
        assert decode(vector["code"]) == expected
        assert encode(expected) == vector["code"]
        assert catalog.validate(decode(vector["code"])) == expected
    assert all(area.buildings for area in catalog.areas)


def test_reuse_options_paginate_without_accepting_ambiguous_yes(location):
    from dataclasses import replace

    from szu_electricity.conversation import ReuseSelection

    choices = [replace(location, roomName=str(n)) for n in range(100, 116)]
    selection = ReuseSelection(choices)
    assert "第 1/2 页" in selection.prompt()
    assert selection.accept("下一页") is None
    assert "第 2/2 页" in selection.prompt() and "16." in selection.prompt()
    assert selection.accept("16") == choices[15]
    with pytest.raises(ElectricityError):
        selection.accept("是")
    assert selection.accept("0") == "new"
