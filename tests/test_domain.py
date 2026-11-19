from datetime import date, datetime, timedelta

import pybase16384
import pytest

from szu_electricity.analytics import render, summarize
from szu_electricity.catalog import SFTEST_TO_SIMS, iotun_catalog, iotun_target
from szu_electricity.config import Settings
from szu_electricity.conversation import Selection
from szu_electricity.models import (
    DEFAULT_LOW_POWER_THRESHOLD,
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
    # The end-of-day clock ages the 23:59-anchored balance by one minute of usage.
    assert short.estimated_days == pytest.approx(2, abs=1e-2)
    assert long.estimated_days == pytest.approx(2, abs=1e-2)
    assert short.daily_average == 2 and short.valid_days == 2
    assert "2.80 元" in render(short)
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
    # The 10-02→10-04 delta covers two days and is spread over both; meter
    # resets and zero deltas never invent usage, but the covered-day average
    # still supports an estimate instead of silencing it.
    assert r.prediction_average == 1.0
    assert r.estimated_days == pytest.approx(4.0, abs=1e-2)
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


def test_midnight_does_not_expire_a_24_hour_old_balance(location):
    now = datetime(2026, 10, 3, 0, 15, tzinfo=SHANGHAI)
    # Yesterday's 23:59 snapshot stays fresh across midnight (calendar-day rule).
    observed = datetime(2026, 10, 2, 23, 59, tzinfo=SHANGHAI)
    data = ProviderResult([Reading(observed, remaining=88, cumulative=100)])
    report = summarize(location, "iotun", Window.for_days(3, now.date()), data, now=now)
    assert not report.expired and report.unavailable_reason is None
    assert report.remaining == 88 and report.estimated_days is None
    assert "仍可用于低电量提醒" in render(report)
    assert "已过期" not in render(report)


@pytest.mark.parametrize("days_ago,expired", [(1, False), (2, True), (-1, True)])
def test_balance_freshness_follows_calendar_days(location, days_ago, expired):
    now = datetime(2026, 10, 3, 12, tzinfo=SHANGHAI)
    observed = now - timedelta(days=days_ago)
    report = summarize(
        location,
        "official",
        Window.for_days(3, now.date()),
        ProviderResult([], 4, observed),
        now=now,
    )
    assert report.expired is expired


def test_missing_summary_timestamp_uses_only_matching_measured_balance(location):
    now = datetime(2026, 10, 3, 12, tzinfo=SHANGHAI)
    window = Window.for_days(3, now.date())
    observed = now - timedelta(hours=2)
    data = ProviderResult([Reading(observed, remaining=4)], 4, None)
    report = summarize(location, "iotun", window, data, now=now)
    assert report.observed_at == observed and not report.expired
    data = ProviderResult([Reading(observed, remaining=5)], 4, None)
    assert (
        summarize(location, "iotun", window, data, now=now).unavailable_reason
        == "missing_timestamp"
    )
    data = ProviderResult([Reading(observed, daily=2)], 4, None)
    assert (
        summarize(location, "iotun", window, data, now=now).unavailable_reason
        == "missing_timestamp"
    )
    data = ProviderResult([Reading(observed, daily=2)], None, observed)
    assert (
        summarize(location, "iotun", window, data, now=now).unavailable_reason == "missing_balance"
    )


def day(text):
    return date.fromisoformat(text)


def at_time(text):
    return datetime.fromisoformat(text).replace(tzinfo=SHANGHAI)


def test_multiday_deltas_spread_over_coverage_and_clip_to_the_window(location):
    from szu_electricity.analytics import estimate, normalize

    # 25 days at 1 kWh, then 5 days at 3 kWh: the 15 kWh delta covers 5 days.
    rows = normalize(
        [
            Reading(at_time("2025-12-06 00:00"), cumulative=0),
            Reading(at_time("2025-12-31 00:00"), cumulative=25),
            Reading(at_time("2026-01-05 00:00"), cumulative=40),
        ]
    )
    weekly, monthly, blended = estimate(rows, day("2026-01-05"))
    assert weekly == pytest.approx(17 / 7)
    assert monthly == pytest.approx(40 / 30)
    assert blended == pytest.approx((17 / 7 + 40 / 30) / 2)


def test_baseline_outside_window_counts_only_its_overlap(location):
    from szu_electricity.analytics import normalize, window_average

    rows = normalize(
        [
            Reading(at_time("2025-12-20 00:00"), cumulative=0),
            Reading(at_time("2026-01-05 00:00"), cumulative=30),
        ]
    )
    average, covered = window_average(rows, day("2026-01-05"), 7)
    assert covered == 7.0
    assert average == pytest.approx(30 / 16)


def test_recharges_after_the_reading_use_end_of_day_semantics(location):
    from szu_electricity.analytics import credits_after, evaluate_balance, recharge_kwh
    from szu_electricity.models import Recharge

    observed = at_time("2026-01-04 23:59:01")
    credits = credits_after(
        [
            Recharge("2026-01-03 10:00:00", kwh=99),
            # Date-only rows are end-of-day data: the same day is already
            # inside that day's snapshot, a later day is credited.
            Recharge("2026-01-04", kwh=5),
            Recharge("2026-01-05 07:00:00", kwh=20),
        ],
        observed,
    )
    assert credits == pytest.approx(20.0)
    # Missing kWh falls back to the fixed tariff.
    assert recharge_kwh(Recharge("2026-01-05", yuan=69.979)) == pytest.approx(100.0)
    state = evaluate_balance(10.0, credits, 4.0, observed, at_time("2026-01-05 11:59:01"))
    assert state.elapsed_used == pytest.approx(2.0)
    assert state.effective_remaining == pytest.approx(28.0)
    assert state.estimated_days_left == pytest.approx(7.0)


def test_stale_readings_project_over_the_whole_gap_and_allow_overdraft(location):
    from szu_electricity.analytics import evaluate_balance

    state = evaluate_balance(
        100.0, 0.0, 4.0, at_time("2026-01-01 00:00:00"), at_time("2026-01-05 00:00:00")
    )
    # 四天的缺口按实际经过时间扣减，而不是只扣一天。
    assert state.elapsed_used == pytest.approx(16.0)
    assert state.effective_remaining == pytest.approx(84.0)
    assert state.estimated_days_left == pytest.approx(21.0)
    overdraft = evaluate_balance(
        10.0, 0.0, 4.0, at_time("2026-01-01 00:00:00"), at_time("2026-01-05 00:00:00")
    )
    assert overdraft.effective_remaining == pytest.approx(-6.0)
    assert overdraft.estimated_days_left == 0.0


def test_warning_reasons_follow_the_effective_balance(location):
    from szu_electricity.analytics import summarize
    from szu_electricity.models import Recharge

    window = Window.for_days(31, date(2026, 1, 5))
    rows = [
        Reading(at_time("2026-01-01 00:00"), cumulative=0),
        Reading(at_time("2026-01-02 00:00"), cumulative=1),
        Reading(at_time("2026-01-03 00:00"), cumulative=2),
        Reading(at_time("2026-01-04 00:00"), cumulative=12),
    ]

    def run(
        remaining, recharges=(), threshold=DEFAULT_LOW_POWER_THRESHOLD, observed="2026-01-05 08:00"
    ):
        return summarize(
            location,
            "official",
            window,
            ProviderResult(rows, remaining, at_time(observed)),
            recharges,
            threshold=threshold,
            now=at_time("2026-01-05 08:00"),
        )

    healthy = run(9.0)
    assert healthy.weekly_average == 4.0 and healthy.prediction_average == 4.0
    assert healthy.estimated_days > 2.0 and healthy.warning_reason is None
    assert "低于 5 度" in run(4.99).warning_reason
    assert "低于 7.5 度" in run(7.4, threshold=7.5).warning_reason
    overdraft = run(-2.0)
    assert "透支 2.0 度" in overdraft.warning_reason
    assert overdraft.estimated_days == 0.0
    # A recharge after the reading lifts the balance out of the warning band.
    assert run(1.0, [Recharge("2026-01-05 09:00:00", kwh=50)]).warning_reason is None
    # Stale observations never silence the warning; expiry is a display note.
    stale = run(1.0, observed="2026-01-01 00:00:00")
    assert stale.expired and "透支" in stale.warning_reason
    assert stale.estimated_days is not None
    # 不足 1 天：日均远大于余额。
    fast_burn = summarize(
        location,
        "official",
        window,
        ProviderResult(
            [
                Reading(at_time("2026-01-04 00:00"), cumulative=0),
                Reading(at_time("2026-01-05 00:00"), cumulative=30),
            ],
            2.0,
            at_time("2026-01-05 08:00"),
        ),
        (),
        threshold=5.0,
        now=at_time("2026-01-05 08:00"),
    )
    assert fast_burn.prediction_average == 30.0
    # 有效余额 2.0 度先触发低电量分支；「不足 1 天」在余额不低但天数不足时出现。
    assert "低于 5 度" in fast_burn.warning_reason
