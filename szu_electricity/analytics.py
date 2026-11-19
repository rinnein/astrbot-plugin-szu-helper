from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from .models import (
    DEFAULT_LOW_POWER_THRESHOLD,
    SHANGHAI,
    YUAN_PER_KWH,
    Location,
    ProviderResult,
    Reading,
    Recharge,
    Report,
    Window,
    number,
    timestamp,
)

WEEKLY_DAYS = 7
MONTHLY_DAYS = 31
# At least one full day of coverage is required for an estimate.
MIN_PREDICTION_DAYS = 1.0


def observed_anchor(at: datetime) -> datetime:
    """Date-only rows parse to midnight but are 23:59 end-of-day snapshots.

    Cumulative portals publish the previous day at 23:59; anchoring the
    balance to midnight would age it by a whole extra day.
    """
    if at.time() == time(0, 0):
        return at.replace(hour=23, minute=59)
    return at


def normalize(rows: list[Reading]) -> list[Reading]:
    """Sort, deduplicate per day, and diff cumulative meters.

    Direct daily usage (Lihu/iotun) is preserved. Cumulative readings (SIMS)
    require consecutive dates; gaps and meter resets are not daily
    measurements. The row timestamp survives as the balance anchor.
    """
    days: dict[date, Reading] = {}
    for row in sorted(rows, key=lambda r: r.at):
        days[row.at.date()] = row
    result = list(days.values())
    for i, row in enumerate(result):
        if row.cumulative is None:
            continue
        daily = None
        if i and result[i - 1].at.date() + timedelta(days=1) == row.at.date():
            before, after = number(result[i - 1].cumulative), number(row.cumulative)
            if before is not None and after is not None and after - before >= 0:
                daily = after - before
        result[i] = replace(row, daily=daily)
    return result


def window_average(rows: list[Reading], today: date, days: int) -> tuple[float | None, float]:
    """Covered-day weighted daily average over the last `days` calendar days.

    Cumulative readings (SIMS) spread each multi-day delta over the days it
    covers, clipped to the window so the sample still contributes its rate.
    Direct daily usage (Lihu) counts one day per row — missing days are not
    filled with zeros, which would understate usage and delay warnings.
    """
    start = today - timedelta(days=days - 1)
    total = weight = 0.0
    previous: tuple[date, float] | None = None
    for row in rows:
        day = row.at.date()
        cumulative = number(row.cumulative)
        if cumulative is not None:
            if previous is not None:
                previous_date, previous_total = previous
                covered = (day - previous_date).days
                if covered >= 1:
                    delta = cumulative - previous_total
                    if delta >= 0:
                        first = day - timedelta(days=covered - 1)
                        overlap = (min(day, today) - max(first, start)).days + 1
                        if overlap > 0:
                            total += delta * overlap / covered
                            weight += overlap
            previous = (day, cumulative)
        else:
            daily = number(row.daily)
            if daily is not None and daily >= 0 and start <= day <= today:
                total += daily
                weight += 1.0
    average = total / weight if weight >= MIN_PREDICTION_DAYS and weight > 0 else None
    return average, weight


def estimate(rows: list[Reading], today: date) -> tuple[float | None, float | None, float | None]:
    """Weekly and monthly window averages, and their blend.

    The short window tracks changes, the long one dampens one-off spikes;
    either window falls back to the other when it has no data.
    """
    weekly, _ = window_average(rows, today, WEEKLY_DAYS)
    monthly, _ = window_average(rows, today, MONTHLY_DAYS)
    available = [value for value in (weekly, monthly) if value is not None]
    blended = (
        (available[0] + available[1]) / 2
        if len(available) == 2
        else (available[0] if available else None)
    )
    return weekly, monthly, blended


@dataclass(frozen=True)
class BalanceState:
    credits: float
    elapsed_used: float
    effective_remaining: float
    estimated_days_left: float | None


def evaluate_balance(
    remaining: float,
    credits: float,
    daily_average: float | None,
    observed_at: datetime,
    now: datetime,
) -> BalanceState:
    """Turn the balance at the reading instant into the balance right now.

    Recharge records come from a live source but readings can lag a full day
    (SIMS only publishes the previous day's 23:59 snapshot), so:
    effective = reading + recharges after it − usage estimated at the daily
    average since the reading. The gap is never capped: when the portal stops
    publishing, the projection keeps advancing over the whole elapsed time.

    The balance **may go negative** — the school allows overdraft and the
    meter settlement delay can show negative balances itself. Only the
    estimated days clamp to zero (there are no negative days left).
    """
    elapsed = max(0.0, (now - observed_at).total_seconds() / 86400)
    elapsed_used = (
        daily_average * elapsed if daily_average is not None and daily_average > 0 else 0.0
    )
    effective = remaining + credits - elapsed_used
    days = None
    if daily_average is not None and math.isfinite(daily_average) and daily_average > 0:
        days = max(0.0, effective / daily_average)
    return BalanceState(credits, elapsed_used, effective, days)


def recharge_kwh(recharge: Recharge) -> float | None:
    """Portal kilowatt-hours, or the amount converted at the fixed tariff."""
    value = recharge.kwh
    if value is None and recharge.yuan is not None:
        value = recharge.yuan / YUAN_PER_KWH
    if value is None or not 0 < value < float("inf"):
        return None
    return value


def credits_after(recharges: list[Recharge], observed_at: datetime) -> float:
    """Recharged energy counted strictly after the reading instant.

    Date-only recharge rows are end-of-day data: a recharge on the same day
    as the reading is already inside that day's snapshot, later days count.
    """
    observed_date = observed_at.date()
    total = 0.0
    for recharge in recharges:
        kwh = recharge_kwh(recharge)
        if kwh is None:
            continue
        text = recharge.time.strip()
        when = timestamp(text)
        if when is None:
            continue
        if ":" in text:
            counted = when > observed_at
        else:
            counted = when.date() > observed_date
        if counted:
            total += kwh
    return total


def warning_reason(
    balance: BalanceState | None, threshold: float = DEFAULT_LOW_POWER_THRESHOLD
) -> str | None:
    """Warning text; no warning without a balance.

    Stale readings never silence the warning: the effective balance already
    subtracts usage since the reading at the daily average, so the projection
    itself is the most trustworthy signal when the portal stops publishing.
    Expiry is a display note for the caller to render.
    """
    if balance is None:
        return None
    if balance.effective_remaining < 0:
        return f"宿舍电量已透支 {-balance.effective_remaining:.1f} 度，请及时充值。"
    if balance.effective_remaining < threshold:
        return f"宿舍剩余电量低于 {threshold:g} 度，请及时充值。"
    if balance.estimated_days_left is not None and balance.estimated_days_left < 1.0:
        return "预计可用电量不足 1 天，请及时充值。"
    return None


def summarize(
    location: Location,
    source: str,
    window: Window,
    data: ProviderResult,
    recharges: list[Recharge] | tuple = (),
    *,
    threshold: float = DEFAULT_LOW_POWER_THRESHOLD,
    now: datetime | None = None,
) -> Report:
    """Build the report from the full cached history.

    The prediction windows span further back than the query window, so the
    caller feeds every stored reading; `window` only trims the display stats.
    """
    # The end-of-day default keeps historical/statistical calls deterministic
    # for their explicit window; live callers supply the actual clock.
    now = now or datetime.combine(window.end, time.max, tzinfo=SHANGHAI)
    today = now.date()
    rows = normalize(data.readings)
    weekly, monthly, blended = estimate(rows, today)

    remaining = number(data.remaining)
    observed = data.observed_at if remaining is not None else None
    if remaining is None:
        for row in reversed(rows):
            if number(row.remaining) is not None:
                remaining, observed = number(row.remaining), row.at
                break
    elif observed is None:
        # A summary may omit its timestamp even though the same measured balance
        # and timestamp are present in the records. Never borrow a usage-only date.
        observed = next(
            (row.at for row in reversed(rows) if number(row.remaining) == remaining), None
        )

    unavailable_reason = None
    if remaining is None:
        unavailable_reason = "missing_balance"
    elif observed is None:
        unavailable_reason = "missing_timestamp"

    balance = None
    if remaining is not None and observed is not None:
        anchor = observed_anchor(observed)
        balance = evaluate_balance(
            remaining, credits_after(recharges, anchor), blended, anchor, now
        )
        expired = not today - timedelta(days=1) <= anchor.date() <= today
    else:
        expired = True

    in_window = [row for row in rows if window.begin <= row.at.date() <= window.end]
    used = [value for row in in_window if (value := number(row.daily)) is not None and value >= 0]
    total = sum(used) if used else None
    return Report(
        location,
        source,
        window,
        remaining,
        observed,
        balance.credits if balance else 0.0,
        balance.elapsed_used if balance else 0.0,
        balance.effective_remaining if balance else None,
        weekly,
        monthly,
        blended,
        balance.estimated_days_left if balance else None,
        total,
        total / len(used) if used else None,
        len(used),
        expired,
        warning_reason(balance, threshold),
        unavailable_reason,
    )


def fmt(value: float | None) -> str:
    return "暂无数据" if value is None else f"{value:.2f}"


def render(report: Report, detail: bool = False) -> str:
    effective = report.effective_remaining
    balance = effective * YUAN_PER_KWH if effective is not None else None
    estimate_text = (
        "暂无法估算" if report.estimated_days is None else f"约 {report.estimated_days:.1f} 天"
    )
    lines = [
        f"有效余额：{fmt(effective)} 度（折算 {fmt(balance)} 元）",
        f"预计可用：{estimate_text}",
    ]
    if detail:
        lines = [
            f"{report.location.buildingName} {report.location.roomName}",
            f"读数电量：{fmt(report.remaining)} 度（读数时间 "
            f"{report.observed_at.strftime('%Y-%m-%d %H:%M') if report.observed_at else '暂无'}）",
            f"观测后充值：+{fmt(report.recharge_credit)} 度 · 观测至今估算用电：-{fmt(report.elapsed_used)} 度",
            lines[0],
            f"近 7 天日均：{fmt(report.weekly_average)} 度/天 · 近 31 天日均：{fmt(report.monthly_average)} 度/天",
            f"预测日均：{fmt(report.prediction_average)} 度/天",
            lines[1],
            f"周期用电：{fmt(report.total_used)} 度",
            f"统计周期：{report.window.begin} 至 {report.window.end}（{report.valid_days} 天有效用量）",
            f"预计天数按近 7 天与 31 天覆盖加权日均估算；固定换算 {YUAN_PER_KWH} 元/度。",
            f"读数日期：{report.observed_at.date() if report.observed_at else '暂无'} · 来源：{'学校官方' if report.source == 'official' else 'iotun.com'}",
        ]
    if report.warning_reason:
        lines.append(report.warning_reason)
    elif report.unavailable_reason == "missing_balance":
        lines.append("数据源未返回剩余电量，暂不能预测或判断低电量。")
    elif report.unavailable_reason == "missing_timestamp":
        lines.append("余额缺少有效的读数时间，暂不能预测或判断低电量。")
    elif report.estimated_days is None and not report.expired:
        lines.append("近 7 天与 31 天的有效用量不足，暂无法估算；有效余额仍可用于低电量提醒。")
    if report.expired and report.unavailable_reason is None:
        lines.append("读数已过期，以上余额按日均用量推算至当前时刻，仍照常判断低电量。")
    if report.refresh_error:
        lines.append(f"实时查询失败（{report.refresh_error}），以下为缓存数据。")
    return "\n".join(lines)
