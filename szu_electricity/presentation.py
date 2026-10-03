"""Platform-neutral content plus QQ Markdown/keyboard presentation."""

from urllib.parse import quote

from .analytics import fmt, render
from .conversation import ReuseSelection, Selection
from .models import RATE, ElectricityError, Report
from .qq_messages import MessageView, escape_markdown


def command_tag(command: str, label: str) -> str:
    text, show = quote(command, safe=""), quote(label, safe="")
    if len(text) > 100 or len(show) > 100:
        raise ElectricityError("选项指令超过 QQ 长度限制，请缩短机器人唤醒前缀。")
    return f'<qqbot-cmd-input text="{text}" show="{show}" reference="true" />'


def choice_card(
    selection,
    token: str,
    revision: int,
    owner: str,
    *,
    keyboard: bool,
    error="",
    command_prefix="/",
):
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
    # Both entry points use the same indexed actions, even without a keyboard.
    items = options + controls
    actions = {str(i): value for i, (_, value) in enumerate(items)}
    links = [
        command_tag(
            f"{command_prefix}宿舍选项 {token}:{revision}:{i}",
            "选择" if i < len(options) else label,
        )
        for i, (label, _) in enumerate(items)
    ]
    if isinstance(selection, ReuseSelection) and len(selection.locations) == 1:
        heading = "发现已有绑定：" + selection.label(selection.locations[0])
    else:
        heading = selection.prompt().splitlines()[0]
    intro = "## 绑定宿舍\n\n" + escape_markdown(heading)
    if error:
        intro += "\n\n" + escape_markdown(error)
    footer = "点击填入输入框，再发送；也可手输编号。每步 120 秒内回复。"
    page = next(
        (
            line.split(" · ")[0]
            for line in selection.prompt().splitlines()
            if line.startswith("第 ")
        ),
        "",
    )
    footer = (page + "\n\n" if page else "") + footer
    table = "| 编号 | 选项 | 操作 |\n| --- | --- | --- |\n" + "\n".join(
        f"| {escape_markdown(value) if i < len(options) else '—'} | {escape_markdown(label).replace(chr(10), ' ').replace(chr(13), ' ')} | {links[i]} |"
        for i, (label, value) in enumerate(items)
    )
    listing = "\n\n".join(
        f"{escape_markdown(value)}. {escape_markdown(label)} {links[i]}"
        for i, (label, value) in enumerate(items)
    )
    rows = []
    if keyboard:

        def button(index):
            label, _ = items[index]
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
                    "unsupport_tips": "请使用表格指令或回复编号",
                },
            }

        for start in range(0, len(options), 5):
            rows.append(
                {"buttons": [button(i) for i in range(start, min(start + 5, len(options)))]}
            )
        rows.append({"buttons": [button(i) for i in range(len(options), len(items))]})
    return MessageView(
        text,
        intro + "\n\n" + table + "\n\n" + footer,
        {"content": {"rows": rows}} if rows else None,
        intro + "\n\n" + listing + "\n\n" + footer,
    ), actions


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
