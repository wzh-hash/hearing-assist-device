"""物联网上报子系统。

对外只有这些名字；内部结构（连接、队列、心跳、重连）都属于实现细节。

    from doorbell.iot import IotLink, IotSettings, load, save, apply_update, Status

典型用法：

    settings = load()                       # sounds/iot.json，损坏就回退默认值
    link = IotLink(settings)
    link.on_change(lambda snap: push_to_page(snap.as_dict()))
    link.start()
    link.publish({"type": "hit", "cls": "doorbell", "tier": "L2",
                  "score": 0.8, "latency": 0.42, "ts": time.time(), "preview": False})
"""

from .errors import ConfigError, IotError, NotConfigured, explain
from .link import IotLink, paho_client_factory
from .payload import (
    CLASSES,
    FORMATS,
    MAX_BYTES,
    SHORT_CODES,
    TIERS,
    PayloadError,
    encode_event,
    event_dict,
    heartbeat_bytes,
    parse_heartbeat,
    short_code,
)
from .settings import (
    DEFAULT_CLIENT_ID,
    DEFAULT_DEVICE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    SECRET_MASK,
    IotSettings,
    apply_update,
    load,
    save,
)
from .status import IotSnapshot, Status

__all__ = [
    "CLASSES",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_DEVICE",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "FORMATS",
    "MAX_BYTES",
    "SHORT_CODES",
    "SECRET_MASK",
    "TIERS",
    "ConfigError",
    "IotError",
    "IotLink",
    "IotSettings",
    "IotSnapshot",
    "NotConfigured",
    "PayloadError",
    "Status",
    "apply_update",
    "encode_event",
    "event_dict",
    "explain",
    "heartbeat_bytes",
    "load",
    "paho_client_factory",
    "parse_heartbeat",
    "save",
    "short_code",
]
