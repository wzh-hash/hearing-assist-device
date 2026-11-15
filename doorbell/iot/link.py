"""物联网上报子系统：一条深接口，背后是连接、重连、队列、心跳与状态。

调用方只需要知道这些：

    link = IotLink(settings)          # settings 由 settings.load() 读出
    link.on_change(fn)                # fn(IotSnapshot)：状态变了就回调
    link.start()                      # 幂等；总开关关闭 / 未配置时什么都不做
    link.apply(new_settings)          # 改配置；必要时重连，累计计数不清零
    link.publish(event) -> bool       # 非阻塞；入队返回 True，丢弃返回 False
    link.test() -> dict               # {"ok": bool, "reason": str}，不影响当前连接
    link.snapshot() -> IotSnapshot    # 某个瞬间的一致视图
    link.stop()                       # 停线程、断连接、清队列

接口里写明的约束（调用方可以依赖）：

- `publish` 不阻塞识别线程；断线/队列满时丢弃并计数，**恢复后不补发**（IOT-3.8、IOT-3.11）
- 永不使用 retain；QoS 取配置（IOT-3.9、IOT-3.10）
- 重连间隔 1→16 秒，由客户端适配器配置（IOT-2.2）
- 板子在线：收到心跳置真，120 秒无心跳置假；本机连接断开期间**冻结**（IOT-5.2、IOT-5.4）
- 只上报 `type == "hit"`；`ignore` 仅在 `publish_ignored` 打开时上报（IOT-3.1、IOT-3.2）
- 内部任何异常都只进日志与 `last_error`，绝不冒到调用方（NFR-IOT-3）
- 日志与错误消息里不出现 `Iot_pwd`（IOT-1.4）
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from dataclasses import replace
from typing import Any, Callable, Protocol

from . import errors, payload
from .settings import IotSettings
from .status import IotSnapshot, Status

log = logging.getLogger(__name__)

#: 队列深度：溢出即丢弃（IOT-3.14）
QUEUE_SIZE = 8
#: 相邻两条发布的最小间隔（秒）
MIN_GAP = 0.3
#: 心跳超时判离线（秒）
HEARTBEAT_TIMEOUT = 120.0
#: 「测试连接」的等待上限（秒）
TEST_TIMEOUT = 6.0
#: MQTT keepalive（秒）
KEEPALIVE = 60


class ClientLike(Protocol):
    """适配器协议：paho 的客户端满足它，测试里的假客户端也满足它。"""

    on_connect: Any
    on_disconnect: Any
    on_message: Any

    def username_pw_set(self, username: str, password: str) -> None: ...
    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 16) -> None: ...
    def connect_async(self, host: str, port: int, keepalive: int = 60) -> None: ...
    def loop_start(self) -> None: ...
    def loop_stop(self) -> None: ...
    def disconnect(self) -> None: ...
    def publish(self, topic: str, payload: bytes | None = ..., qos: int = ..., retain: bool = ...) -> Any: ...
    def subscribe(self, topic: str, qos: int = ...) -> Any: ...


def paho_client_factory(settings: IotSettings, *, client_id: str) -> ClientLike:
    """默认适配器：paho-mqtt 2.x。延迟导入，离线自测不需要装 paho。

    `lib/` 是仓库自带的离线依赖目录（不进版本库），所以找不到 paho 时先把它加进
    `sys.path` 再试一次——保证"拷贝目录即可运行"在 IoT 这条链路上也成立。
    """
    try:
        import paho.mqtt.client as mqtt  # noqa: PLC0415
    except ImportError:
        import sys

        from .. import paths

        lib = str(paths.ROOT / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import paho.mqtt.client as mqtt  # noqa: PLC0415

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
    )
    client.username_pw_set(settings.iot_id, settings.iot_pwd)
    return client


class IotLink:
    """上报子系统。线程模型：一个工作线程 + 客户端自己的网络线程。"""

    def __init__(
        self,
        settings: IotSettings | None = None,
        *,
        client_factory: Callable[..., ClientLike] | None = None,
        client_id_suffix: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        queue_size: int = QUEUE_SIZE,
        min_gap: float = MIN_GAP,
        heartbeat_timeout: float = HEARTBEAT_TIMEOUT,
        test_timeout: float = TEST_TIMEOUT,
    ) -> None:
        self._settings = settings or IotSettings()
        self._factory = client_factory or paho_client_factory
        self._suffix = client_id_suffix or (lambda: str(random.randint(1000, 9999)))
        self._clock = clock
        self._wall = wall
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._min_gap = min_gap
        self._hb_timeout = heartbeat_timeout
        self._test_timeout = test_timeout

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closing = False
        self._worker: threading.Thread | None = None
        self._client: ClientLike | None = None
        self._snap = IotSnapshot()
        self._listeners: list[Callable[[IotSnapshot], None]] = []
        self._last_send = float("-inf")
        self._hb_since: float | None = None

    # ---------- 只读 ----------

    @property
    def settings(self) -> IotSettings:
        return self._settings

    def snapshot(self) -> IotSnapshot:
        with self._lock:
            snap = replace(self._snap)
            snap.queue_depth = self._q.qsize()
            return snap

    def on_change(self, listener: Callable[[IotSnapshot], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """幂等启动。关闭或未配置时只更新状态，不建立任何连接（IOT-1.1）。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if not self._settings.enabled:
                self._set_status(Status.DISABLED)
                return
            if not self._settings.configured:
                self._set_status(
                    Status.UNCONFIGURED,
                    "缺少 Iot_id / Iot_pwd / 事件主题",
                )
                return
            self._closing = False
            self._stop.clear()
            self._set_status(Status.CONNECTING)
            self._worker = threading.Thread(target=self._run, name="doorbell-iot", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        with self._lock:
            self._closing = True
            self._stop.set()
            worker, self._worker = self._worker, None
            if worker is None or not worker.is_alive():
                self._drain_queue_locked()
                self._set_status(Status.DISABLED)
                return
        if worker is not threading.current_thread():
            worker.join(timeout=5)
        with self._lock:
            self._drain_queue_locked()
            self._set_status(Status.DISABLED)

    def apply(self, settings: IotSettings) -> None:
        """换配置：先停机再按新配置启动；累计发送/丢弃计数保留（IOT-2.3）。"""
        settings = settings.validated()
        self.stop()
        with self._lock:
            self._settings = settings
            self._snap.last_error = ""
            self._snap.last_heartbeat_at = None
            self._snap.board_online = None
            self._hb_since = None
        if settings.enabled:
            self.start()

    # ---------- 上报 ----------

    def publish(self, event: Any) -> bool:
        """把一次判定交给上报队列。永不抛异常、永不阻塞识别线程。"""
        try:
            if isinstance(event, dict):
                kind = event.get("type")
                if kind == "trigger":
                    return False
                if kind == "ignore" and not self._settings.publish_ignored:
                    return False

            with self._lock:
                status = self._snap.status
                if status in (Status.DISABLED, Status.UNCONFIGURED):
                    return False
                ready = status is Status.CONNECTED and self._client is not None
            if not ready:
                self._drop()
                return False

            try:
                data = self._encode(event)
            except Exception:
                log.debug("事件载荷不可用，已丢弃", exc_info=True)
                self._drop()
                return False

            try:
                self._q.put_nowait(data)
            except queue.Full:
                self._drop()
                return False
            self._notify()
            return True
        except Exception:  # 兜底：上报绝不能影响识别（NFR-IOT-3）
            log.exception("上报入口出现异常（已忽略）")
            return False

    # ---------- 连通性自检 ----------

    def test(self) -> dict[str, Any]:
        """独立客户端测一次连通性，不干扰正在工作的连接（IOT-2.4）。"""
        settings = self._settings
        if not settings.configured:
            return {"ok": False, "reason": "未配置：请先填写 Iot_id、Iot_pwd 与事件主题"}

        done = threading.Event()
        result: dict[str, Any] = {"ok": False, "reason": f"连接服务器超时（{int(self._test_timeout)} 秒内没有回应）"}
        clients: list[ClientLike] = []

        def on_connect(client, userdata, flags, reason_code, properties=None):
            ok = not errors.is_failure(reason_code)
            result["ok"] = ok
            result["reason"] = "" if ok else errors.explain(reason_code=reason_code)
            done.set()

        def on_disconnect(client, userdata, flags, reason_code, properties=None):
            if not done.is_set():
                result["reason"] = (
                    errors.explain(reason_code=reason_code)
                    if errors.is_failure(reason_code)
                    else "连接被服务器断开"
                )
                done.set()

        try:
            client = self._factory(settings, client_id=f"{settings.client_id}-test-{self._suffix()}")
            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            clients.append(client)
            client.connect_async(settings.host, settings.port, keepalive=10)
            client.loop_start()
            done.wait(self._test_timeout)
        except Exception as exc:
            result = {"ok": False, "reason": errors.explain(exc)}
        finally:
            for client in clients:
                for action in (client.loop_stop, client.disconnect):
                    try:
                        action()
                    except Exception:
                        log.debug("清理测试连接时出错", exc_info=True)
        return result

    # ---------- 工作线程 ----------

    def _run(self) -> None:
        settings = self._settings
        try:
            client = self._factory(settings, client_id=settings.client_id)
        except Exception as exc:
            message = errors.explain(exc)
            log.warning("上报组件创建失败：%s", message)
            with self._lock:
                self._set_status(Status.FAILED, message)
            return

        try:
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            try:
                # IOT-2.2：1 秒起逐次翻倍、上限 16 秒。这是链路策略，由本模块负责，
                # 不依赖具体适配器自己设成什么。
                client.reconnect_delay_set(1, 16)
            except Exception:
                log.debug("客户端不支持设置重连退避，沿用其默认值", exc_info=True)
            with self._lock:
                self._client = client
            client.connect_async(settings.host, settings.port, keepalive=KEEPALIVE)
            client.loop_start()

            while not self._stop.is_set():
                try:
                    data = self._q.get(timeout=0.25)
                except queue.Empty:
                    data = None
                if data is not None:
                    self._send(client, data)
                self._tick_heartbeat()
        except Exception as exc:  # 工作线程任何异常都不得冒泡
            message = errors.explain(exc)
            log.warning("上报线程异常：%s", message)
            with self._lock:
                self._set_status(Status.FAILED, message)
        finally:
            for action in (client.loop_stop, client.disconnect):
                try:
                    action()
                except Exception:
                    log.debug("清理上报客户端时出错", exc_info=True)
            with self._lock:
                if self._client is client:
                    self._client = None

    def _send(self, client: ClientLike, data: bytes) -> None:
        settings = self._settings
        gap = self._min_gap - (self._clock() - self._last_send)
        if gap > 0 and self._stop.wait(gap):
            return
        try:
            info = client.publish(settings.event_topic, data, qos=settings.qos, retain=False)
            code = getattr(info, "rc", 0)
            if code not in (0, None):
                raise OSError(f"publish rc={code}")
        except Exception as exc:
            message = errors.explain(exc)
            log.warning("上报失败：%s", message)
            with self._lock:
                self._snap.dropped += 1
                self._snap.last_error = message
            self._notify()
            return
        with self._lock:
            self._snap.sent += 1
            self._snap.last_sent_at = self._wall()
            self._last_send = self._clock()
        self._notify()

    def _encode(self, event: Any) -> bytes:
        if isinstance(event, (bytes, bytearray)):
            return bytes(event)
        if not isinstance(event, dict):
            raise payload.PayloadError("事件必须是字典或已编码的字节")
        return payload.encode_event(
            cls=str(event.get("cls") or ""),
            tier=str(event.get("tier") or "L2"),
            score=float(event.get("score") or 0.0),
            latency=float(event.get("latency") or 0.0),
            ts=float(event.get("ts") or self._wall()),
            demo=bool(event.get("demo", False)),
            fmt=self._settings.format,
        )

    def _tick_heartbeat(self) -> None:
        with self._lock:
            if self._snap.status is not Status.CONNECTED:  # 断开期间冻结（IOT-5.4）
                return
            if not self._settings.status_topic:
                return
            now = self._wall()
            last = self._snap.last_heartbeat_at
            reference = max(last or 0.0, self._hb_since or 0.0)
            if reference <= 0:
                return
            changed = False
            if now - reference > self._hb_timeout:
                if self._snap.board_online is not False:
                    self._snap.board_online = False
                    changed = True
            elif last is not None and now - last <= self._hb_timeout:
                if self._snap.board_online is not True:
                    self._snap.board_online = True
                    changed = True
            if changed:
                self._notify()

    # ---------- 客户端回调（paho v2 签名，假客户端同样遵守） ----------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        try:
            if errors.is_failure(reason_code):
                message = errors.explain(reason_code=reason_code)
                log.warning("上报连接被拒绝：%s", message)
                with self._lock:
                    self._set_status(Status.FAILED, message)
                return
            settings = self._settings
            with self._lock:
                self._snap.connected_at = self._wall()
                self._hb_since = self._wall()
                self._set_status(Status.CONNECTED)
            if settings.status_topic:
                try:
                    client.subscribe(settings.status_topic, qos=0)
                except Exception:
                    log.warning("订阅状态主题失败（事件上报不受影响）", exc_info=True)
            log.info("上报已连接：%s:%s", settings.host, settings.port)
        except Exception:
            log.exception("处理连接回调时出错（已忽略）")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        try:
            with self._lock:
                if self._closing:
                    return
                message = errors.explain(reason_code=reason_code) if errors.is_failure(reason_code) else ""
                self._hb_since = None
                self._drain_queue_locked()
                self._set_status(Status.RECONNECTING, message or "连接已断开，正在重连")
            log.warning("上报连接断开，等待自动重连")
        except Exception:
            log.exception("处理断线回调时出错（已忽略）")

    def _on_message(self, client, userdata, message) -> None:
        try:
            heartbeat = payload.parse_heartbeat(getattr(message, "payload", b"") or b"")
            if heartbeat is None:
                log.debug("状态主题收到无法识别的消息，已忽略")
                return
            with self._lock:
                self._snap.last_heartbeat_at = self._wall()
                self._snap.board_online = True
            log.debug("收到板子心跳：%s", heartbeat.get("dev") or "未命名设备")
            self._notify()
        except Exception:
            log.exception("处理心跳时出错（已忽略）")

    # ---------- 内部工具 ----------

    def _drain_queue(self) -> int:
        count = 0
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return count
            count += 1

    def _drain_queue_locked(self) -> None:
        self._snap.dropped += self._drain_queue()

    def _drop(self) -> None:
        with self._lock:
            self._snap.dropped += 1
        self._notify()

    def _set_status(self, status: Status, error: str | None = None) -> None:
        self._snap.status = status
        if error is not None:
            self._snap.last_error = error
        self._notify()

    def _notify(self) -> None:
        with self._lock:
            snap = replace(self._snap)
            listeners = list(self._listeners)
        snap.queue_depth = self._q.qsize()
        for listener in listeners:
            try:
                listener(snap)
            except Exception:  # 回调出错不得影响上报
                log.exception("状态回调出错（已忽略）")
