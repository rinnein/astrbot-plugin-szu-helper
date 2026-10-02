"""Platform-neutral content plus QQ Markdown/keyboard presentation."""

from .analytics import fmt, render
from .conversation import ReuseSelection, Selection
from .models import RATE, Report
from .qq_messages import MessageView, escape_markdown


def choice_card(selection, token: str, revision: int, owner: str, *, keyboard: bool, error=""):
    options = []
    controls = []
    if isinstance(selection, ReuseSelection):
        if len(selection.locations) == 1:
            options = [("复用已有绑定", "1"), ("重新选择宿舍", "2")]
        else:
            start = selection.page * 15
            options = [
                (selection.label(loc), str(i + 1))
                for i, loc in enumerate(selection.locations[start : start + 15], start)
            ]
            if selection.page:
                controls.append(("上一页", "上一页"))
            if start + 15 < len(selection.locations):
                controls.append(("下一页", "下一页"))
            controls.append(("重新选择", "0"))
    elif isinstance(selection, Selection):
        if selection.step < 3:
            choices = selection.choices()
            start = selection.page * 15
            options = [
                (o.name, str(i + 1)) for i, o in enumerate(choices[start : start + 15], start)
            ]
            if selection.page:
                controls.append(("上一页", "上一页"))
            if start + 15 < len(choices):
                controls.append(("下一页", "下一页"))
        if selection.step:
            controls.append(("返回", "返回"))
    controls.append(("取消", "取消"))
    text = selection.prompt()
    if error:
        text = error + "\n" + text
    markdown = "## 绑定宿舍\n\n" + "\n\n".join(escape_markdown(line) for line in text.splitlines())
    actions = {}
    rows = []
    if keyboard:

        def button(label, value):
            index = str(len(actions))
            actions[index] = value
            return {
                "id": f"szu-{revision}-{index}",
                "render_data": {
                    "label": label if len(label) <= 10 else label[:9] + "…",
                    "visited_label": label if len(label) <= 10 else label[:9] + "…",
                    "style": 1,
                },
                "action": {
                    "type": 1,
                    "permission": {"type": 0, "specify_user_ids": [owner]},
                    "data": f"szuh:{token}:{revision}:{index}",
                    "unsupport_tips": "请回复文字编号选择",
                },
            }

        for start in range(0, len(options), 5):
            rows.append(
                {"buttons": [button(label, value) for label, value in options[start : start + 5]]}
            )
        rows.append({"buttons": [button(label, value) for label, value in controls]})
    return MessageView(text, markdown, {"content": {"rows": rows}} if rows else None), actions


def detail_view(report: Report, layout="table") -> MessageView:
    rows = [
        ("当前电量", fmt(report.remaining) + " 度"),
        (
            "折算余额",
            fmt(report.remaining * RATE if report.remaining is not None else None) + " 元",
        ),
        ("日均用电", fmt(report.daily_average) + " 度/天"),
        (
            "预计可用",
            "暂无法估算" if report.estimated_days is None else f"约 {report.estimated_days:.1f} 天",
        ),
        ("周期用电", fmt(report.total_used) + " 度"),
    ]
    title = "## 用电详情\n\n" + escape_markdown(
        f"{report.location.buildingName} {report.location.roomName}"
    )
    listing = "\n".join(f"- **{label}**：{escape_markdown(value)}" for label, value in rows)
    table = "| 指标 | 数值 |\n| --- | --- |\n" + "\n".join(
        f"| {label} | {escape_markdown(value)} |" for label, value in rows
    )
    # Keep the authoritative date/window/warning text from the plain renderer.
    footer = "\n\n".join(escape_markdown(line) for line in render(report, True).splitlines()[5:])
    fallback = title + "\n\n" + listing + "\n\n" + footer
    markdown = title + "\n\n" + table + "\n\n" + footer if layout == "table" else fallback
    return MessageView(
        render(report, True), markdown, fallback_markdown=fallback if layout == "table" else None
    )
