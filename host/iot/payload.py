"""事件载荷与心跳的编码、校验（REQ-01 §5.1–§5.3）。

这里是纯函数模块：不连接、不读文件、不打印。板子侧（`board/`）按同一份定义实现，
所以任何格式变动都必须同时改两边与需求文档。
"""

from __future__ import annotations

import json
import math

#: 四类事件的内部标识
CLASSES = ("doorbell", "knock", "kettle", "fire")

#: 短码（IOT-3.5）
SHORT_CODES = {
    "doorbell": "DOOR",
    "knock": "KNOCK",
    "kettle": "KETTLE",
    "fire": "FIRE",
}

#: 告警等级
TIERS = ("L1", "L2", "L3")

#: 载荷版本（IOT-3.4）
VERSION = 1

#: 单条载荷上限（IOT-3.6）
MAX_BYTES = 1024

#: 心跳上限（§5.3）
HEARTBEAT_MAX_BYTES = 64

#: 支持的载荷形式
FORMATS = ("json", "short")


class PayloadError(ValueError):
    """载荷不满足 §5 的定义。"""


def short_code(cls: str) -> str:
    """返回类别短码；未知类别抛 PayloadError。"""
    try:
        return SHORT_CODES[cls]
    except KeyError:
        raise PayloadError(f"未知类别：{cls}") from None


def _check_event(cls: str, tier: str, score: float, latency: float, ts: float, preview: bool) -> None:
    if cls not in CLASSES:
        raise PayloadError(f"未知类别：{cls}")
    if tier not in TIERS:
        raise PayloadError(f"未知等级：{tier}")
    for name, value in (("得分", score), ("延迟", latency), ("时间戳", ts)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PayloadError(f"{name}必须是数字")
        if not math.isfinite(float(value)):
            raise PayloadError(f"{name}必须是有限数字")
    if not 0.0 <= float(score) <= 1.0:
        raise PayloadError("得分必须在 0–1 之间")
    if float(latency) < 0:
        raise PayloadError("延迟不能为负")
    if float(ts) <= 0:
        raise PayloadError("时间戳必须大于 0")
    if not isinstance(preview, bool):
        raise PayloadError("试听标记必须是布尔值")


def event_dict(
    *,
    cls: str,
    tier: str,
    score: float,
    latency: float,
    ts: float,
    preview: bool = False,
) -> dict:
    """构造结构化载荷（字段固定，多一个少一个都不行，IOT-3.4）。"""
    _check_event(cls, tier, score, latency, ts, preview)
    return {
        "v": VERSION,
        "cls": cls,
        "tier": tier,
        "score": round(float(score), 3),
        "latency": round(float(latency), 3),
        "ts": round(float(ts), 3),
        "preview": bool(preview),
    }


def dumps(obj: dict) -> bytes:
    """紧凑 JSON（UTF-8），字段顺序即构造顺序。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_event(
    *,
    cls: str,
    tier: str,
    score: float,
    latency: float,
    ts: float,
    preview: bool = False,
    fmt: str = "json",
) -> bytes:
    """按指定形式编码一条事件；两种形式语义相同（IOT-3.3）。"""
    if fmt == "short":
        return short_code(cls).encode("ascii")
    if fmt not in FORMATS:
        raise PayloadError(f"未知载荷形式：{fmt}")
    data = dumps(event_dict(cls=cls, tier=tier, score=score, latency=latency, ts=ts, preview=preview))
    if len(data) > MAX_BYTES:
        raise PayloadError(f"载荷超过 {MAX_BYTES} 字节")
    return data


def heartbeat_dict(device: str = "", ts: float = 0.0) -> dict:
    """心跳载荷：`v` 与 `hb` 必需，`dev`/`ts` 可选（§5.3）。"""
    payload: dict = {"v": VERSION, "hb": 1}
    if device:
        payload["dev"] = str(device)
    if ts:
        payload["ts"] = round(float(ts), 3)
    return payload


def heartbeat_bytes(device: str = "", ts: float = 0.0) -> bytes:
    data = dumps(heartbeat_dict(device, ts))
    if len(data) > HEARTBEAT_MAX_BYTES:
        raise PayloadError(f"心跳超过 {HEARTBEAT_MAX_BYTES} 字节")
    return data


def parse_heartbeat(data: bytes | bytearray | str) -> dict | None:
    """识别并解析心跳；不是心跳（包括短码与事件 JSON）返回 None。

    接收端只认 `hb == 1` 且 `v == 1` 的 JSON 对象（§5.3「可识别」）。
    """
    if isinstance(data, str):
        raw = data.encode("utf-8", "ignore")
    else:
        raw = bytes(data or b"")
    if not raw.lstrip().startswith(b"{"):
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("v") != VERSION or obj.get("hb") != 1:
        return None
    out: dict = {}
    if "dev" in obj and isinstance(obj["dev"], str):
        out["dev"] = obj["dev"]
    if isinstance(obj.get("ts"), (int, float)):
        out["ts"] = float(obj["ts"])
    return out


def is_short_code(data: bytes | bytearray | str) -> bool:
    """判断一段载荷是否为四类短码之一（给模拟板子与自测用）。"""
    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data or b"").decode("ascii")
        except UnicodeDecodeError:
            return False
    return text.strip().upper() in SHORT_CODES.values()
