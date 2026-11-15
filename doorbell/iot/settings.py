"""IoT 配置的读写、校验与掩码（IOT-1.2–IOT-1.5、D-03）。

落盘位置：`sounds/iot.json`（已被 `.gitignore` 忽略）。文件里含 `Iot_pwd`，
所以本模块**从不把整个配置对象写进日志**：`IotSettings.__repr__` 已做掩码。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from .. import paths
from .errors import ConfigError

log = logging.getLogger(__name__)

DEFAULT_HOST = "iot.dfrobot.com.cn"
DEFAULT_PORT = 1883
DEFAULT_CLIENT_ID = "doorbell-pc-01"
DEFAULT_DEVICE = "k10-01"

#: 页面与日志里统一用它表示"密码已设置"
SECRET_MASK = "******"


@dataclass(frozen=True)
class IotSettings:
    """一次上报配置。不可变：修改走 `replace()` / `apply_update()`。"""

    enabled: bool = False
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    iot_id: str = ""
    iot_pwd: str = ""
    event_topic: str = ""
    status_topic: str = ""
    client_id: str = DEFAULT_CLIENT_ID
    qos: int = 1
    publish_ignored: bool = False
    device_name: str = DEFAULT_DEVICE
    format: str = "json"

    # ---------- 派生属性 ----------

    @property
    def configured(self) -> bool:
        """三个必填项齐了才算已配置（IOT-1.5）。"""
        return bool(self.iot_id.strip() and self.iot_pwd.strip() and self.event_topic.strip())

    def __repr__(self) -> str:  # 防止 dataclass 默认 repr 把密码打进日志
        masked_pwd = SECRET_MASK if self.iot_pwd else ""
        return (
            f"IotSettings(enabled={self.enabled}, host={self.host!r}, port={self.port}, "
            f"iot_id={self.iot_id!r}, iot_pwd={masked_pwd!r}, event_topic={self.event_topic!r}, "
            f"status_topic={self.status_topic!r}, client_id={self.client_id!r}, qos={self.qos}, "
            f"format={self.format!r})"
        )

    # ---------- 序列化 ----------

    def to_dict(self) -> dict[str, Any]:
        """完整落盘形式（含密码）。只在 `save()` 内部使用。"""
        return asdict(self)

    def masked(self) -> dict[str, Any]:
        """给页面/日志看的掩码形式：密码只留占位符。"""
        data = asdict(self)
        data["iot_pwd"] = SECRET_MASK if self.iot_pwd else ""
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IotSettings":
        """宽容读取：未知键忽略、类型尽量转换、缺字段用默认值（IOT-9.9）。"""
        if not isinstance(raw, dict):
            raise ConfigError("配置格式不对：顶层必须是对象")

        def as_bool(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "开")
            return default

        def as_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def as_text(value: Any, default: str) -> str:
            return default if value is None else str(value)

        known = {field.name for field in fields(cls)}
        clean = {key: value for key, value in raw.items() if key in known}
        if "enabled" in clean:
            clean["enabled"] = as_bool(clean["enabled"], cls.enabled)
        if "publish_ignored" in clean:
            clean["publish_ignored"] = as_bool(clean["publish_ignored"], cls.publish_ignored)
        for key in ("port", "qos"):
            if key in clean:
                clean[key] = as_int(clean[key], getattr(cls, key))
        for key in ("host", "iot_id", "iot_pwd", "event_topic", "status_topic",
                    "client_id", "device_name", "format"):
            if key in clean:
                clean[key] = as_text(clean[key], getattr(cls, key))
        return cls(**clean)

    # ---------- 校验 ----------

    def validated(self) -> "IotSettings":
        """把明显写错的配置挡在保存/连接之前；不因"没填完"报错（那由 IOT-1.5 管）。"""
        host = (self.host or "").strip()
        if not host:
            raise ConfigError("接入地址不能为空")
        if any(ch.isspace() for ch in host):
            raise ConfigError("接入地址不能包含空格")

        try:
            port = int(self.port)
        except (TypeError, ValueError):
            raise ConfigError("端口必须是数字") from None
        if not 1 <= port <= 65535:
            raise ConfigError("端口必须在 1–65535 之间")

        try:
            qos = int(self.qos)
        except (TypeError, ValueError):
            raise ConfigError("服务质量等级必须是数字") from None
        if qos not in (0, 1):
            raise ConfigError("服务质量等级只能是 0 或 1")

        if self.format not in ("json", "short"):
            raise ConfigError("载荷格式只能是 json 或 short")

        client_id = (self.client_id or "").strip()
        if not client_id:
            raise ConfigError("本机客户端标识不能为空")
        if any(ch.isspace() for ch in client_id):
            raise ConfigError("本机客户端标识不能包含空格")
        if len(client_id) > 64:
            raise ConfigError("本机客户端标识太长（最多 64 个字符）")

        event_topic = (self.event_topic or "").strip()
        status_topic = (self.status_topic or "").strip()
        for name, topic in (("事件主题", event_topic), ("状态主题", status_topic)):
            if not topic:
                continue
            if any(ch.isspace() for ch in topic):
                raise ConfigError(f"{name}不能包含空格")
            if "/" in topic:
                raise ConfigError(f"{name}不能包含 /：请填平台分配的设备编码（IOT-4.2）")
        if event_topic and status_topic and event_topic == status_topic:
            raise ConfigError("事件主题与状态主题不能相同（IOT-4.4）")

        return replace(
            self,
            host=host,
            port=port,
            qos=qos,
            client_id=client_id,
            iot_id=(self.iot_id or "").strip(),
            event_topic=event_topic,
            status_topic=status_topic,
            device_name=(self.device_name or "").strip(),
        )


# ---------- 读写 ----------

def config_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path) if path is not None else paths.IOT_CONFIG


def load(path: str | os.PathLike | None = None) -> IotSettings:
    """读配置；文件不存在或损坏时回退到默认值，不抛异常（IOT-1.5）。"""
    file = config_path(path)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return IotSettings()
    except (OSError, ValueError) as exc:
        log.warning("IoT 配置读取失败，已回退默认值：%s", exc)
        return IotSettings()
    try:
        return IotSettings.from_dict(raw)
    except ConfigError as exc:
        log.warning("IoT 配置内容不可用，已回退默认值：%s", exc)
        return IotSettings()


def save(settings: IotSettings, path: str | os.PathLike | None = None) -> Path:
    """原子写入；保留文件里未知的键，避免旧版本配置被顺手清掉。"""
    file = config_path(path)
    file.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    try:
        loaded = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, ValueError):
        existing = {}

    merged = {**existing, **settings.to_dict()}
    fd, tmp_name = tempfile.mkstemp(dir=str(file.parent), prefix=".iot-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, file)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return file


def apply_update(current: IotSettings, patch: dict[str, Any]) -> IotSettings:
    """页面保存：只接受已知字段；`iot_pwd` 传空表示"不修改"（IOT-1.4）。"""
    if not isinstance(patch, dict):
        raise ConfigError("更新内容必须是对象")
    clean = {key: value for key, value in patch.items() if value is not None}
    if "iot_pwd" in clean and str(clean["iot_pwd"]).strip() == "":
        clean.pop("iot_pwd")
    merged = {**current.to_dict(), **clean}
    return IotSettings.from_dict(merged).validated()
