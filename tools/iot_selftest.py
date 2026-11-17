"""IoT 子系统离线自测：不需要麦克风、不需要外网、不需要 paho。

    python -X utf8 tools/iot_selftest.py

覆盖：载荷格式、配置读写与掩码、开关与未配置、连接、QoS/retain、断线丢弃与不补发、
队列溢出、心跳与在线判定、断开期间冻结、测试连接、改配置重连、凭证不进日志。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from doorbell.iot import payload  # noqa: E402
from doorbell.iot.errors import ConfigError  # noqa: E402
from doorbell.iot.link import IotLink  # noqa: E402
from doorbell.iot.settings import (  # noqa: E402
    IotSettings,
    apply_update,
    load,
    save,
)
from doorbell.iot.status import Status  # noqa: E402

FAILURES: list[str] = []


def section(title: str) -> None:
    print(f"\n== {title} ==")


def check(name: str, condition: object, detail: object = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def wait_for(probe, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return True
        time.sleep(0.01)
    return bool(probe())


@contextlib.contextmanager
def workspace_temp():
    """临时目录放在仓库根：`sounds/` 与系统 %TEMP% 在本机沙箱里不可写。"""
    path = ROOT / ".iot-selftest"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def base_settings(**overrides) -> IotSettings:
    values = dict(
        enabled=True,
        host="iot.dfrobot.com.cn",
        port=1883,
        iot_id="iot-id-1",
        iot_pwd="secret-pwd",
        event_topic="evt123",
        status_topic="hb456",
        client_id="doorbell-pc-01",
        qos=1,
    )
    values.update(overrides)
    return IotSettings(**values)


EVENT = {
    "type": "hit",
    "cls": "doorbell",
    "tier": "L2",
    "score": 0.8,
    "latency": 0.42,
    "ts": 1763265600.0,
    "preview": False,
}


# ---------------------------------------------------------------- 假客户端


class FakeReason:
    def __init__(self, value: int = 0) -> None:
        self.value = value
        self.is_failure = bool(value)


class FakeClient:
    """满足 link.ClientLike 的最小实现；所有回调都是同步触发的，便于断言。"""

    auto_connect = True
    connect_failure = 0
    publish_rc = 0

    def __init__(self, settings: IotSettings, *, client_id: str) -> None:
        self.settings = settings
        self.client_id = client_id
        self.published: list[dict] = []
        self.subscribed: list[tuple] = []
        self.connect_calls: list[tuple] = []
        self.reconnect: tuple | None = None
        self.credentials: tuple | None = None
        self.loop_started = False
        self.disconnected = False
        self.gate: threading.Event | None = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    # -- 适配器表面 --
    def username_pw_set(self, username: str, password: str) -> None:
        self.credentials = (username, password)

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 16) -> None:
        self.reconnect = (min_delay, max_delay)

    def connect_async(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connect_calls.append((host, port, keepalive))
        if self.auto_connect:
            self._connect(self.connect_failure)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_started = False

    def disconnect(self) -> None:
        self.disconnected = True

    def publish(self, topic, payload=None, qos=0, retain=False):
        if self.gate is not None and not self.gate.is_set():
            self.gate.wait(2.0)
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return SimpleNamespace(rc=self.publish_rc)

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))
        return (0, 1)

    # -- 测试驱动 --
    def _connect(self, rc: int = 0) -> None:
        if self.on_connect:
            self.on_connect(self, None, {}, FakeReason(rc), None)

    def _disconnect(self, rc: int = 0) -> None:
        if self.on_disconnect:
            self.on_disconnect(self, None, None, FakeReason(rc), None)

    def _message(self, data: bytes) -> None:
        if self.on_message:
            self.on_message(self, None, SimpleNamespace(payload=data, topic="t"))


class Factory:
    def __init__(self, **attrs) -> None:
        self.attrs = attrs
        self.clients: list[FakeClient] = []

    def __call__(self, settings: IotSettings, *, client_id: str) -> FakeClient:
        client = FakeClient(settings, client_id=client_id)
        for key, value in self.attrs.items():
            setattr(client, key, value)
        self.clients.append(client)
        return client


class FakeWall:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------- 用例


def test_payload() -> None:
    section("载荷")
    encoded = payload.encode_event(
        cls="doorbell", tier="L2", score=0.8, latency=0.42, ts=1763265600.123, preview=False
    )
    obj = json.loads(encoded)
    check("字段固定为七个", set(obj) == {"v", "cls", "tier", "score", "latency", "ts", "preview"}, sorted(obj))
    check("不超过 1KB", len(encoded) <= payload.MAX_BYTES, len(encoded))
    check("短码 FIRE", payload.encode_event(cls="fire", tier="L3", score=0.9, latency=0.4, ts=1.0, fmt="short") == b"FIRE")

    bad_cases = [
        ("未知类别", dict(cls="nope", tier="L2", score=0.5, latency=0.1, ts=1.0)),
        ("未知等级", dict(cls="doorbell", tier="L9", score=0.5, latency=0.1, ts=1.0)),
        ("得分越界", dict(cls="doorbell", tier="L2", score=1.5, latency=0.1, ts=1.0)),
        ("延迟为负", dict(cls="doorbell", tier="L2", score=0.5, latency=-1.0, ts=1.0)),
        ("时间戳为零", dict(cls="doorbell", tier="L2", score=0.5, latency=0.1, ts=0.0)),
    ]
    for name, kwargs in bad_cases:
        try:
            payload.event_dict(**kwargs)
            check(name + "被拒", False)
        except payload.PayloadError:
            check(name + "被拒", True)

    heartbeat = payload.heartbeat_bytes("k10-01", 1763265600.0)
    check("心跳 ≤64B", len(heartbeat) <= payload.HEARTBEAT_MAX_BYTES, len(heartbeat))
    check("心跳可解析", (payload.parse_heartbeat(heartbeat) or {}).get("dev") == "k10-01")
    check("短码不是心跳", payload.parse_heartbeat(b"DOOR") is None)
    check("事件不是心跳", payload.parse_heartbeat(encoded) is None)
    check("坏 JSON 不是心跳", payload.parse_heartbeat(b"{oops") is None)
    check("短码识别", payload.is_short_code(b" knock ") and not payload.is_short_code(b"music"))


def test_settings() -> None:
    section("配置")
    defaults = IotSettings()
    check("默认地址与端口", defaults.host == "iot.dfrobot.com.cn" and defaults.port == 1883)
    check("默认关闭", defaults.enabled is False)
    check("默认未配置", not defaults.configured)

    with workspace_temp() as tmp:
        file = Path(tmp) / "iot.json"
        current = base_settings()
        save(current, file)
        loaded = load(file)
        check("保存/读取往返", loaded.event_topic == "evt123" and loaded.iot_pwd == "secret-pwd")
        check("落盘是本机文件（含密码）", json.loads(file.read_text("utf-8"))["iot_pwd"] == "secret-pwd")
        check("repr 不含密码", "secret-pwd" not in repr(loaded))
        check("掩码形式不含密码", "secret-pwd" not in json.dumps(loaded.masked(), ensure_ascii=False))
        check("掩码占位符", loaded.masked()["iot_pwd"] == "******")

        updated = apply_update(loaded, {"event_topic": "evt999", "iot_pwd": ""})
        check("空密码表示不修改", updated.iot_pwd == "secret-pwd" and updated.event_topic == "evt999")
        check("未知字段被忽略", apply_update(loaded, {"bogus": 1}).event_topic == "evt123")

        for name, patch in (
            ("两主题相同被拒", {"status_topic": "evt123"}),
            ("自造路径被拒", {"event_topic": "preview/alert"}),
            ("空地址被拒", {"host": "  "}),
            ("QoS 越界被拒", {"qos": 2}),
        ):
            try:
                apply_update(loaded, patch)
                check(name, False)
            except ConfigError:
                check(name, True)

        file.write_text("{ broken", encoding="utf-8")
        fallback = load(file)
        check("坏配置回退默认", fallback.host == "iot.dfrobot.com.cn" and not fallback.configured)

        file.write_text(json.dumps({"future_key": 1}), encoding="utf-8")
        save(base_settings(), file)
        check("未知键保留", json.loads(file.read_text("utf-8")).get("future_key") == 1)
        check("无临时文件残留", not list(Path(tmp).glob(".iot-*")))


def test_switch_and_unconfigured() -> None:
    section("开关与未配置")
    factory = Factory()
    link = IotLink(IotSettings(enabled=False), client_factory=factory)
    link.start()
    check("关闭时不建连", factory.clients == [] and link.snapshot().status is Status.DISABLED)
    check("关闭时 publish 不计数", link.publish(EVENT) is False and link.snapshot().dropped == 0)
    link.stop()

    factory = Factory()
    link = IotLink(IotSettings(enabled=True, iot_id="only-id"), client_factory=factory)
    link.start()
    check("缺主题→未配置", factory.clients == [] and link.snapshot().status is Status.UNCONFIGURED)
    check("未配置有可读原因", "事件主题" in link.snapshot().last_error, link.snapshot().last_error)
    link.stop()


def test_connect_and_publish() -> None:
    section("连接与上报")
    factory = Factory()
    link = IotLink(base_settings(), client_factory=factory)
    link.start()
    check("已连接", wait_for(lambda: link.snapshot().status is Status.CONNECTED), link.snapshot().status)

    client = factory.clients[0]
    check("重连间隔 1→16", client.reconnect == (1, 16), client.reconnect)
    check("连接地址", client.connect_calls[-1][:2] == ("iot.dfrobot.com.cn", 1883), client.connect_calls)
    check("订阅状态主题", wait_for(lambda: client.subscribed == [("hb456", 0)]), client.subscribed)

    check("命中入队", link.publish(EVENT) is True)
    check("已发出", wait_for(lambda: len(client.published) == 1), len(client.published))
    sent = client.published[0]
    check("事件主题正确", sent["topic"] == "evt123", sent["topic"])
    check("QoS 1", sent["qos"] == 1)
    check("retain=False", sent["retain"] is False)
    check("载荷类别", json.loads(sent["payload"])["cls"] == "doorbell")
    check("发送计数", wait_for(lambda: link.snapshot().sent == 1), link.snapshot().sent)
    check("最后发送时间", link.snapshot().last_sent_at is not None)

    ignored = dict(EVENT, type="ignore")
    check("忽略默认不上报", link.publish(ignored) is False)
    check("忽略不计数", link.snapshot().dropped == 0, link.snapshot().dropped)
    check("触发器不上报", link.publish(dict(EVENT, type="trigger")) is False)

    link.stop()
    check("停止后状态", link.snapshot().status is Status.DISABLED)


def test_disconnect_drop() -> None:
    section("断线丢弃与不补发")
    factory = Factory()
    link = IotLink(base_settings(), client_factory=factory)
    link.start()
    client = factory.clients[0]
    wait_for(lambda: link.snapshot().status is Status.CONNECTED)

    client._disconnect(0)
    check("转为重连中", wait_for(lambda: link.snapshot().status is Status.RECONNECTING), link.snapshot().status)
    before = len(client.published)
    dropped_before = link.snapshot().dropped
    check("断线时丢弃", link.publish(EVENT) is False)
    check("丢弃计数增加", link.snapshot().dropped == dropped_before + 1, link.snapshot().dropped)

    client._connect(0)
    check("恢复已连接", wait_for(lambda: link.snapshot().status is Status.CONNECTED))
    time.sleep(0.25)
    check("恢复后不补发", len(client.published) == before, len(client.published) - before)
    link.stop()


def test_queue_overflow() -> None:
    section("队列溢出")
    factory = Factory()
    link = IotLink(base_settings(), client_factory=factory, queue_size=1, min_gap=0.0)
    link.start()
    client = factory.clients[0]
    wait_for(lambda: link.snapshot().status is Status.CONNECTED)

    client.gate = threading.Event()
    check("第一条入队", link.publish(EVENT) is True)
    check("工作线程取走首条", wait_for(lambda: link.snapshot().queue_depth == 0))
    check("第二条入队", link.publish(EVENT) is True)
    check("第三条被拒", link.publish(EVENT) is False)
    check("丢弃计数", link.snapshot().dropped >= 1, link.snapshot().dropped)
    client.gate.set()
    link.stop()


def test_heartbeat() -> None:
    section("心跳与板子在线")
    wall = FakeWall()
    factory = Factory()
    link = IotLink(base_settings(), client_factory=factory, wall=wall, heartbeat_timeout=10.0)
    link.start()
    client = factory.clients[0]
    wait_for(lambda: link.snapshot().status is Status.CONNECTED)
    check("尚未收到心跳→未知", link.snapshot().board_online is None, link.snapshot().board_online)

    client._message(payload.heartbeat_bytes("k10-01", wall.now))
    check("收到心跳→在线", link.snapshot().board_online is True)

    wall.now += 11.0
    check("超过 120 秒→离线", wait_for(lambda: link.snapshot().board_online is False), link.snapshot().board_online)

    client._message(payload.heartbeat_bytes("k10-01", wall.now))
    check("心跳恢复→在线", link.snapshot().board_online is True)

    client._disconnect(0)
    check("断线后冻结", wait_for(lambda: link.snapshot().status is Status.RECONNECTING) and link.snapshot().board_online is True)
    wall.now += 500.0
    time.sleep(0.4)
    check("断线期间不翻离线", link.snapshot().board_online is True, link.snapshot().board_online)

    client._connect(0)
    check("重连后仍在线", wait_for(lambda: link.snapshot().status is Status.CONNECTED) and link.snapshot().board_online is True)
    wall.now += 11.0
    check("重连后重新计时→离线", wait_for(lambda: link.snapshot().board_online is False), link.snapshot().board_online)
    link.stop()


def test_connection_test() -> None:
    section("测试连接")
    factory = Factory()
    link = IotLink(base_settings(), client_factory=factory)
    result = link.test()
    check("成功", result["ok"] is True, result)
    check("独立客户端标识", factory.clients[0].client_id.startswith("doorbell-pc-01-test-"), factory.clients[0].client_id)
    check("不影响主连接", link.snapshot().status is Status.DISABLED, link.snapshot().status)

    factory = Factory(connect_failure=4)
    link = IotLink(base_settings(), client_factory=factory)
    result = link.test()
    check("密码错误可读", result["ok"] is False and "账号或密码错误" in result["reason"], result)

    factory = Factory()
    link = IotLink(IotSettings(), client_factory=factory)
    result = link.test()
    check("未配置可读原因", result["ok"] is False and "未配置" in result["reason"], result)
    check("未配置不建连", factory.clients == [])


def test_apply_and_logging() -> None:
    section("改配置与凭证")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("doorbell")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    factory = Factory()
    link = IotLink(base_settings(), client_factory=factory)
    link.start()
    wait_for(lambda: link.snapshot().status is Status.CONNECTED)
    link.publish(EVENT)
    wait_for(lambda: link.snapshot().sent == 1)

    link.apply(base_settings(host="127.0.0.1", event_topic="evt999"))
    check("客户端重建", wait_for(lambda: len(factory.clients) == 2), len(factory.clients))
    check("新地址生效", wait_for(
        lambda: len(factory.clients) > 1
        and bool(factory.clients[1].connect_calls)
        and factory.clients[1].connect_calls[-1][0] == "127.0.0.1"
    ), factory.clients[1].connect_calls if len(factory.clients) > 1 else None)
    check("累计发送保留", link.snapshot().sent == 1, link.snapshot().sent)
    check("新主题生效", wait_for(lambda: link.snapshot().status is Status.CONNECTED))

    check("坏事件不抛", link.publish({"cls": "nope"}) is False)
    link.stop()
    logger.removeHandler(handler)
    check("日志不含密码", "secret-pwd" not in stream.getvalue())


def test_paho_adapter() -> None:
    section("paho 适配器")
    from doorbell.iot.link import paho_client_factory

    try:
        client = paho_client_factory(base_settings(), client_id="doorbell-pc-01")
        check("可构造 paho 客户端（含 lib/ 回退）", client is not None)
    except Exception as exc:  # noqa: BLE001  —— 这里就是要展示失败原因
        check("可构造 paho 客户端（含 lib/ 回退）", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    test_payload()
    test_settings()
    test_switch_and_unconfigured()
    test_connect_and_publish()
    test_disconnect_drop()
    test_queue_overflow()
    test_heartbeat()
    test_connection_test()
    test_paho_adapter()
    test_apply_and_logging()

    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
