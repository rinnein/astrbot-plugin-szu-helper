from .models import Area, Catalog, ElectricityError, Location, Option, building_name

SIMS_CLIENTS = {
    "yuehai-main": "192.168.84.1",
    "yuehai-xinzhai": "192.168.84.87",
    "canghai-main": "192.168.84.110",
    "xili-main": "172.21.101.11",
}
GROUP_AREAS = {
    "yuehai_north": "yuehai-main",
    "yuehai_newzhai": "yuehai-xinzhai",
    "yuehai_south": "canghai-main",
    "lihu": "xili-main",
}
SFTEST_TO_SIMS = dict(
    zip(
        [f"{n:02}" for n in range(1, 10)],
        ["57", "56", "54", "55", "58", "59", "61", "64", "63"],
        strict=True,
    )
)
SIMS_TO_SFTEST = {v: k for k, v in SFTEST_TO_SIMS.items()}


def empty_catalog() -> Catalog:
    return Catalog(
        [
            Option("yuehai", "北校区（粤海校区）"),
            Option("canghai", "南校区（沧海校区）"),
            Option("xili", "西丽校区（丽湖校区）"),
        ],
        [
            Area("yuehai-main", "粤海校区宿舍", "yuehai"),
            Area("yuehai-xinzhai", "深大新斋区", "yuehai"),
            Area("canghai-main", "沧海校区宿舍", "canghai"),
            Area("xili-main", "丽湖校区宿舍", "xili"),
            Area("xili-lihuo-phase2", "丽湖二期", "xili"),
        ],
    )


def iotun_catalog(data: object) -> Catalog:
    if not isinstance(data, list):
        raise ElectricityError("iotun 宿舍目录格式异常。")
    catalog = empty_catalog()
    areas = {a.id: a for a in catalog.areas}
    for campus in data:
        if not isinstance(campus, dict):
            raise ElectricityError("iotun 宿舍目录格式异常。")
        group = campus.get("group") or campus.get("client")
        if group in SIMS_CLIENTS.values():
            group = next(g for g, a in GROUP_AREAS.items() if SIMS_CLIENTS[a] == group)
        for b in campus.get("buildings", []):
            bid = str(b.get("id", ""))
            name = building_name(str(b.get("name", "")))
            if not bid or not name:
                continue
            if group == "yuehai_sftest":
                if bid not in SFTEST_TO_SIMS:
                    continue
                area_id, bid = "yuehai-main", SFTEST_TO_SIMS[bid]
            elif group == "lihu" and bid in {f"{n:02}" for n in range(1, 7)}:
                area_id = "xili-lihuo-phase2"
            else:
                area_id = GROUP_AREAS.get(group)
            if area_id:
                target = areas[area_id].buildings
                if not any(x.id == bid for x in target):
                    target.append(Option(bid, name))
    if not any(a.buildings for a in catalog.areas):
        raise ElectricityError("iotun 未返回可识别的宿舍目录。")
    return catalog


def iotun_target(location: Location) -> tuple[str, str]:
    if location.areaId == "yuehai-main" and location.buildingId in SIMS_TO_SFTEST:
        return "yuehai_sftest", SIMS_TO_SFTEST[location.buildingId]
    if location.areaId == "xili-lihuo-phase2":
        return "lihu", location.buildingId
    for group, area in GROUP_AREAS.items():
        if area == location.areaId:
            return group, location.buildingId
    raise ElectricityError("未知的宿舍区域。")
