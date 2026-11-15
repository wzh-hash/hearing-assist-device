"""上报状态与对外快照（IOT-2.1、IOT-2.3、IOT-5.2）。

`IotSnapshot` 是页面唯一需要读的结构；它在 `IotLink.snapshot()` 里被复制出来，
所以调用方拿到的是某一瞬间的一致视图，不需要知道内部加锁细节。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from enum import Enum


class Status(str, Enum):
    """上报状态。前五种是需求要求的（IOT-2.1），`DISABLED` 表示总开关关闭。"""

    DISABLED = "已关闭"
    UNCONFIGURED = "未配置"
    CONNECTING = "连接中"
    CONNECTED = "已连接"
    RECONNECTING = "重连中"
    FAILED = "失败"


@dataclass
class IotSnapshot:
    """某个瞬间的对外状态。

    `board_online` 为三态：
    - `None`：未知（没配状态主题、还没收到过心跳、或刚重连还在重新计时）
    - `True`：120 秒内收到过心跳
    - `False`：超过 120 秒没有心跳
    """

    status: Status = Status.DISABLED
    last_error: str = ""
    last_sent_at: float | None = None
    sent: int = 0
    dropped: int = 0
    queue_depth: int = 0
    board_online: bool | None = None
    last_heartbeat_at: float | None = None
    connected_at: float | None = None

    def as_dict(self) -> dict:
        """给 JSON/SSE 用的形式：状态用中文值，时间额外给 HH:MM:SS 文本。"""
        data = asdict(self)
        data["status"] = self.status.value
        data["last_sent_text"] = _hms(self.last_sent_at)
        data["last_heartbeat_text"] = _hms(self.last_heartbeat_at)
        return data


def _hms(timestamp: float | None) -> str:
    if not timestamp:
        return ""
    try:
        return time.strftime("%H:%M:%S", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return ""
