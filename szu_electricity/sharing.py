import json
from dataclasses import asdict

import pybase16384

from .models import ElectricityError, Location


def encode(location: Location) -> str:
    data = json.dumps(
        {"version": 1, "location": asdict(location)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return pybase16384.encode_to_string(data)


def decode(text: str) -> Location:
    text = text.strip()
    if text.startswith("base16384:"):
        text = text[len("base16384:") :].strip()
    if not text or len(text.encode("utf-8")) > 8192:
        raise ElectricityError("配置码为空或过长。")
    # Validate before entering the native decoder, including short padding inputs.
    padding = ord(text[-1]) - 0x3D00
    body = text
    if 0 <= padding <= 6:
        if padding == 0:
            raise ElectricityError("配置码填充无效。")
        chars = (padding * 8 + 13) // 14
        if len(text) < chars + 1 or (len(text) - chars - 1) % 4:
            raise ElectricityError("配置码长度无效。")
        body = text[:-1]
    elif len(text) % 4:
        raise ElectricityError("配置码长度无效。")
    if any(not 0x4E00 <= ord(c) <= 0x8DFF for c in body):
        raise ElectricityError("配置码含有无效字符。")
    try:
        raw = pybase16384.decode_from_string(text)
        if pybase16384.encode_to_string(raw) != text:
            raise ValueError("noncanonical encoding")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"version", "location"}:
            raise ValueError("unexpected fields")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ElectricityError("不支持此宿舍配置版本。")
        return Location.from_dict(value["location"])
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ElectricityError("配置码内容无效。") from exc
