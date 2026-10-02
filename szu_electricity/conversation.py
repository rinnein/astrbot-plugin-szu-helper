from dataclasses import dataclass
from typing import Literal

from .catalog import empty_catalog
from .models import Catalog, ElectricityError, Location


@dataclass
class ReuseSelection:
    locations: list[Location]
    page: int = 0

    @staticmethod
    def label(location: Location) -> str:
        catalog = empty_catalog()
        area = next((a.name for a in catalog.areas if a.id == location.areaId), location.areaId)
        return f"{area} · {location.buildingName} {location.roomName}"

    def prompt(self) -> str:
        if len(self.locations) == 1:
            return (
                f"发现你在其他会话绑定过宿舍：\n{self.label(self.locations[0])}\n"
                "是否直接复用到当前会话？\n"
                "1. 复用已有绑定（也可回复“是”）\n"
                "2. 重新选择宿舍（也可回复“否”）\n"
                "回复“取消”结束；请在 120 秒内回复。"
            )
        start = self.page * 15
        items = [
            f"{i + 1}. {self.label(location)}"
            for i, location in enumerate(self.locations[start : start + 15], start)
        ]
        return "\n".join(
            [
                "发现你在其他会话绑定过多个宿舍，请选择要复用的宿舍编号：",
                *items,
                "0. 重新选择宿舍",
                f"第 {self.page + 1}/{(len(self.locations) + 14) // 15} 页 · 上一页 / 下一页 / 取消",
                "请在 120 秒内回复。",
            ]
        )

    def accept(self, text: str) -> Location | Literal["new"] | None:
        text = text.strip()
        if text in ("0", "否", "重新选择", "重新选择宿舍"):
            return "new"
        if len(self.locations) == 1:
            if text in ("1", "是", "复用", "确认"):
                return self.locations[0]
            if text == "2":
                return "new"
            raise ElectricityError("请回复“是”复用，或“否”重新选择宿舍。")
        if text in ("上一页", "下一页"):
            change = -1 if text == "上一页" else 1
            self.page = min(max(0, self.page + change), (len(self.locations) - 1) // 15)
            return None
        if text.isascii() and text.isdigit() and 1 <= int(text) <= len(self.locations):
            return self.locations[int(text) - 1]
        raise ElectricityError("请回复已有宿舍的编号，或回复 0 重新选择宿舍。")


@dataclass
class Selection:
    catalog: Catalog
    step: int = 0
    campus: str = ""
    area: str = ""
    building: str = ""
    page: int = 0

    def choices(self):
        if self.step == 0:
            return self.catalog.campuses
        if self.step == 1:
            return [a for a in self.catalog.areas if a.campus_id == self.campus]
        if self.step == 2:
            return next(a for a in self.catalog.areas if a.id == self.area).buildings
        return []

    def prompt(self) -> str:
        if self.step == 3:
            return "请输入宿舍号（例如 601，保留前导零）。\n回复“返回”修改楼栋，或“取消”结束。"
        choices = self.choices()
        labels = ["校区", "宿舍区域", "楼栋"]
        start = self.page * 15
        items = [f"{i + 1}. {o.name}" for i, o in enumerate(choices[start : start + 15], start)]
        return "\n".join(
            [
                f"请选择{labels[self.step]}（回复编号）：",
                *items,
                f"第 {self.page + 1}/{max(1, (len(choices) + 14) // 15)} 页 · 上一页 / 下一页 / 返回 / 取消",
                "每步 120 秒内回复。" if choices else "此区域暂无可用选项，请返回或稍后重试。",
            ]
        )

    def accept(self, text: str) -> Location | None:
        text = text.strip()
        if text == "返回":
            self.step, self.page = max(0, self.step - 1), 0
            return None
        if self.step < 3:
            choices = self.choices()
            if text in ("上一页", "下一页"):
                change = -1 if text == "上一页" else 1
                self.page = min(max(0, self.page + change), max(0, (len(choices) - 1) // 15))
                return None
            if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= len(choices):
                raise ElectricityError("请回复列表中的有效编号。")
            selected = choices[int(text) - 1]
            if self.step == 0:
                self.campus, self.area, self.building = selected.id, "", ""
            elif self.step == 1:
                self.area, self.building = selected.id, ""
            else:
                self.building = selected.id
            self.step, self.page = self.step + 1, 0
            return None
        building = next(
            b
            for a in self.catalog.areas
            if a.id == self.area
            for b in a.buildings
            if b.id == self.building
        )
        location = Location(self.campus, self.area, building.id, building.name, text)
        if self.area == "xili-lihuo-phase2" and (
            not text.isascii() or not text.isdigit() or not 3 <= len(text) <= 5
        ):
            raise ElectricityError("丽湖二期宿舍号应为 3～5 位数字，例如 501。")
        return self.catalog.validate(location)
