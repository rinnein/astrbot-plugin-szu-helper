from datetime import datetime, time, timedelta

from .models import (
    MAX_READING_AGE,
    RATE,
    SHANGHAI,
    Location,
    ProviderResult,
    Reading,
    Report,
    Window,
    number,
)


def summarize(
    location: Location,
    source: str,
    window: Window,
    data: ProviderResult,
    *,
    now: datetime | None = None,
) -> Report:
    # Service callers supply the actual local clock. The end-of-day default
    # keeps historical/statistical calls deterministic for their explicit window.
    now = now or datetime.combine(window.end, time.max, tzinfo=SHANGHAI)
    days: dict = {}
    for row in sorted(data.readings, key=lambda r: r.at):
        if window.begin <= row.at.date() <= window.end:
            days[row.at.date()] = row
    rows: list[Reading] = list(days.values())
    usage = []
    for i, row in enumerate(rows):
        daily = number(row.daily)
        if row.cumulative is not None:
            daily = None
            if i and rows[i - 1].at.date() + timedelta(days=1) == row.at.date():
                before, after = number(rows[i - 1].cumulative), number(row.cumulative)
                if before is not None and after is not None:
                    daily = after - before
        if daily is not None and daily >= 0:
            usage.append((row, daily))
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
    elif observed > now:
        unavailable_reason = "future_timestamp"
    elif now - observed > MAX_READING_AGE:
        unavailable_reason = "stale"
    expired = unavailable_reason is not None
    start = window.end - timedelta(days=2)
    recent = [
        v
        for row, v in usage
        if row.at.date() >= start and (row.cumulative is None or row.at.date() > start)
    ]
    prediction = sum(recent) / len(recent) if recent else None
    estimated = None
    if not expired and remaining is not None and prediction is not None and prediction > 0:
        estimated = max(0, remaining) / prediction
    total = sum(v for _, v in usage) if usage else None
    return Report(
        location,
        source,
        window,
        remaining,
        observed,
        expired,
        total,
        total / len(usage) if usage else None,
        len(usage),
        estimated,
        unavailable_reason,
    )


def fmt(value: float | None) -> str:
    return "暂无数据" if value is None else f"{value:.2f}"


def render(report: Report, detail: bool = False) -> str:
    balance = report.remaining * RATE if report.remaining is not None else None
    estimate = (
        "暂无法估算" if report.estimated_days is None else f"约 {report.estimated_days:.1f} 天"
    )
    lines = [
        f"剩余电量：{fmt(report.remaining)} 度（折算余额 {fmt(balance)} 元）",
        f"预计可用：{estimate}",
    ]
    if detail:
        lines = [
            f"{report.location.buildingName} {report.location.roomName}",
            lines[0],
            f"日均用电：{fmt(report.daily_average)} 度/天",
            lines[1],
            f"周期用电：{fmt(report.total_used)} 度",
            f"统计周期：{report.window.begin} 至 {report.window.end}（{report.valid_days} 天有效用量）",
            "预计天数按最近 3 天有效用量估算；固定换算 0.61 元/度。",
            f"读数日期：{report.observed_at.date() if report.observed_at else '暂无'} · 来源：{'学校官方' if report.source == 'official' else 'iotun.com'}",
        ]
    if report.expired:
        if report.unavailable_reason == "missing_balance":
            lines.append("数据源未返回剩余电量，暂不能预测或判断低电量。")
        elif report.unavailable_reason == "missing_timestamp":
            lines.append("余额缺少有效的读数时间，暂不能预测或判断低电量。")
        elif report.unavailable_reason == "future_timestamp":
            lines.append("数据源的读数时间晚于当前时间，暂不能预测或判断低电量。")
        else:
            observed = (
                report.observed_at.strftime("%Y-%m-%d %H:%M") if report.observed_at else "未知"
            )
            lines.append(f"余额读数已过期（{observed}，有效期 48 小时），暂不能预测或判断低电量。")
    elif report.estimated_days is None:
        lines.append("近 3 天有效用量不足或日均为零，暂无法估算；剩余电量仍可用于低电量提醒。")
    return "\n".join(lines)
