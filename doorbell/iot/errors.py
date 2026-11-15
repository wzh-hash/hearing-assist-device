"""失败原因的中文化（IOT-2.5、IOT-2.6）。

约定：**面向用户的字符串里不出现异常类名、errno、MQTT 原因码**。
原始异常只进 `logging.debug`，由排障时按需打开。
"""

from __future__ import annotations

import socket

try:  # ssl 在标准库里，导入失败只会发生在裁剪过的运行环境
    import ssl as _ssl
except Exception:  # pragma: no cover
    _ssl = None


class IotError(Exception):
    """IoT 子系统的错误基类；消息必须已经是可读中文。"""


class ConfigError(IotError):
    """配置缺失或非法。"""


class NotConfigured(ConfigError):
    """缺少 Iot_id / Iot_pwd / 事件主题。"""


#: MQTT 3.1.1 CONNACK 返回码 → 中文
_CONNACK = {
    1: "服务器不接受这个协议版本",
    2: "客户端标识被服务器拒绝，请换一个本机客户端标识",
    3: "服务器不可用，请稍后再试",
    4: "账号或密码错误（Iot_id / Iot_pwd）",
    5: "没有连接权限，请检查 Iot_id / Iot_pwd",
}


def reason_value(reason_code: object | None) -> int | None:
    """从 paho 的 ReasonCode、整数或 None 里取出整数值。"""
    if reason_code is None:
        return None
    value = getattr(reason_code, "value", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(reason_code, int) and not isinstance(reason_code, bool):
        return reason_code
    return None


def is_failure(reason_code: object | None) -> bool:
    """判断连接结果是否失败；兼容 paho v1（整数）与 v2（ReasonCode）。"""
    flag = getattr(reason_code, "is_failure", None)
    if isinstance(flag, bool):
        return flag
    value = reason_value(reason_code)
    return bool(value)


def explain(exc: BaseException | None = None, reason_code: object | None = None) -> str:
    """把异常或 CONNACK 原因码翻成一句可读中文。"""
    if reason_code is not None:
        value = reason_value(reason_code)
        if value:
            return _CONNACK.get(value, "服务器拒绝连接，请检查账号、客户端标识与网络")
        return ""

    if exc is None:
        return "连接失败（原因未知）"

    if isinstance(exc, ImportError):
        return "缺少 MQTT 组件：请确认程序目录里的 lib 文件夹随程序一起拷贝"
    if _ssl is not None and isinstance(exc, _ssl.SSLError):
        return "服务器要求加密连接，当前只支持明文 1883"
    if isinstance(exc, socket.gaierror):
        return "无法解析服务器地址，请检查接入地址"
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return "无法连接服务器：地址或端口不对，或被网络拦截"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "连接服务器超时：网络不通或平台无响应"
    if isinstance(exc, OSError):
        return "连接失败：网络不可达"
    return "连接失败（网络或平台异常）"
