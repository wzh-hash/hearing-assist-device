"""固件侧载荷协议（REQ-01 §5.1–§5.3）。

纯函数，不碰硬件、不联网，PC 上可直接单测。字段定义必须与上位机的
`doorbell/iot/payload.py` 保持一致——`tools/board_selftest.py` 会做交叉校验，
改这里就要同步改那边，否则测试会红。

固件约束：只依赖 MicroPython 内置的 `json`，不引入任何第三方库（C-3）。
"""

try:
    import json  # MicroPython 内置
except ImportError:  # pragma: no cover
    import ujson as json


VERSION = 1
CLASSES = ("doorbell", "knock", "kettle", "fire")
TIERS = ("L1", "L2", "L3")

SHORT_CODES = {
    "doorbell": "DOOR",
    "knock": "KNOCK",
    "kettle": "KETTLE",
    "fire": "FIRE",
}
SHORT_TO_CLASS = {code: cls for cls, code in SHORT_CODES.items()}

#: 短码重复抑制窗口（毫秒）：纯文本没有 ts，只能用时间去抖
SHORT_DEBOUNCE_MS = 300


def parse_event(data):
    """解析结构化载荷；不是事件（含心跳）返回 None。

    宽容策略：`tier` 缺失按 L2、`preview` 缺失按 False、`ts` 非数字按 None，
    这样上位机将来加字段也不会把板子打挂。`data` 允许 bytes 或 str——
    K10 的 `message()` 返回什么类型官方没写死，两种都得认。
    """
    if not data:
        return None
    if isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        try:
            raw = bytes(data)
        except (TypeError, ValueError):
            return None
    head = raw.lstrip()[:1]
    if head != b"{":
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return _from_obj(obj)


def _from_obj(obj):
    if not isinstance(obj, dict):
        return None
    if obj.get("v") != VERSION:
        return None
    cls = obj.get("cls")
    if cls not in CLASSES:
        return None
    tier = obj.get("tier")
    if tier not in TIERS:
        tier = "L2"
    ts = obj.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        ts = None
    score = obj.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score = None
    latency = obj.get("latency")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        latency = None
    return {
        "cls": cls,
        "tier": tier,
        "preview": bool(obj.get("preview", False)),
        "ts": ts,
        "score": score,
        "latency": latency,
        "short": False,
    }


def parse_short(data):
    """解析短码（DOOR / KNOCK / KETTLE / FIRE）；不是短码返回 None。"""
    if not data:
        return None
    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data).decode("ascii")
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
    cls = SHORT_TO_CLASS.get(text.strip().upper())
    if cls is None:
        return None
    return {"cls": cls, "tier": "L2", "preview": False, "ts": None,
            "score": None, "latency": None, "short": True}


def decode(data):
    """统一入口：先认短码，再认结构化载荷；心跳与垃圾都返回 None。"""
    event = parse_short(data)
    if event is not None:
        return event
    return parse_event(data)


def event_key(event):
    """去重键：结构化事件用 ts，短码用类别（配合毫秒时间窗）。"""
    if event.get("ts") is not None:
        return ("ts", round(float(event["ts"]), 3))
    return ("cls", event["cls"])


def heartbeat_bytes(device="", ts=0.0):
    """心跳载荷：`v` 与 `hb` 必需，`dev`/`ts` 可选，≤64 字节（§5.3）。"""
    payload = {"v": VERSION, "hb": 1}
    if device:
        payload["dev"] = device
    if ts:
        payload["ts"] = round(float(ts), 3)
    data = json.dumps(payload).encode("utf-8")
    if len(data) > 64:
        raise ValueError("心跳超过 64 字节")
    return data


def short_code(cls):
    """类别 → 短码（给日志与测试用）。"""
    return SHORT_CODES[cls]
