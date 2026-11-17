"""固件侧离线自测：不需要 K10、不需要外网、不需要 paho。

    python -X utf8 tools/board_selftest.py

它验证的是 `board/` 里**与硬件无关**的那部分：协议解析、去重、心跳、退避重连、
看门狗、云端状态切换。真正碰 K10 的两个适配器（`k10_mqtt.py` / `ui_k10.py`）
只能在真板上验，见文件末尾的"未验证"说明。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT / "board"), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import protocol as board_protocol  # noqa: E402
from mqtt_link import BoardLink  # noqa: E402

from doorbell.iot import payload as pc_payload  # noqa: E402

FAILURES: list[str] = []


def section(title: str) -> None:
    print(f"\n== {title} ==")


def check(name: str, condition: object, detail: object = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------- 假件


class FakeClock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


class FakeUi:
    def __init__(self) -> None:
        self.shows: list[dict] = []
        self.cloud: list[bool] = []
        self.ticks = 0

    def show(self, event) -> None:
        self.shows.append(event)

    def set_cloud(self, connected) -> None:
        self.cloud.append(bool(connected))

    def tick(self) -> None:
        self.ticks += 1


class FakePin:
    def __init__(self, number) -> None:
        self.number = number
        self.digital: list[int] = []
        self.analog: list[tuple] = []

    def write_digital(self, value) -> None:
        self.digital.append(value)

    def write_analog(self, value=0, freq=0) -> None:
        self.analog.append((value, freq))


class FakePinFactory:
    def __init__(self) -> None:
        self.pins: dict[int, FakePin] = {}

    def __call__(self, number) -> FakePin:
        pin = FakePin(number)
        self.pins[number] = pin
        return pin


class FakeMqtt:
    def __init__(self, on_message, *, connect_ok=True, publish_ok=True) -> None:
        self.on_message = on_message
        self.connect_ok = connect_ok
        self.publish_ok = publish_ok
        self.alive = True
        self.subscribed: list[str] = []
        self.published: list[tuple] = []
        self.polls = 0
        self.disconnects = 0

    def connect(self) -> bool:
        return self.connect_ok

    def subscribe(self, topic) -> bool:
        self.subscribed.append(topic)
        return True

    def publish(self, topic, data) -> bool:
        self.published.append((topic, data))
        return self.publish_ok

    def poll(self) -> None:
        self.polls += 1

    def is_connected(self) -> bool:
        return self.alive

    def disconnect(self) -> None:
        self.disconnects += 1
        self.alive = False

    def deliver(self, data) -> None:
        self.on_message("evt", data)


class Factory:
    def __init__(self, **attrs) -> None:
        self.attrs = attrs
        self.clients: list[FakeMqtt] = []

    def __call__(self, on_message) -> FakeMqtt:
        client = FakeMqtt(on_message, **self.attrs)
        self.clients.append(client)
        return client


def make_link(factory, ui, clock, **overrides) -> BoardLink:
    params = dict(
        topic_event="evt",
        topic_status="hb",
        device="k10-01",
        heartbeat_ms=30000,
        watchdog_ms=45000,
        clock_ms=clock,
        sleep_ms=lambda ms: None,
        wall=lambda: 1763265600.0,
        log=lambda message: None,
    )
    params.update(overrides)
    return BoardLink(factory, ui, **params)


# ---------------------------------------------------------------- 用例


def test_protocol_contract() -> None:
    section("协议与上位机一致")
    check("版本一致", board_protocol.VERSION == pc_payload.VERSION)
    check("类别一致", board_protocol.CLASSES == pc_payload.CLASSES)
    check("短码一致", board_protocol.SHORT_CODES == pc_payload.SHORT_CODES)
    check("等级一致", board_protocol.TIERS == pc_payload.TIERS)

    encoded = pc_payload.encode_event(
        cls="kettle", tier="L3", score=0.74, latency=0.41, ts=1763265600.123, demo=True
    )
    event = board_protocol.decode(encoded)
    check("上位机 JSON → 板子", event is not None and event["cls"] == "kettle" and event["demo"] is True, event)
    check("ts 保真", abs(event["ts"] - 1763265600.123) < 0.001)
    check("tier 保真", event["tier"] == "L3")
    check("score 透传", abs(event["score"] - 0.74) < 0.001, event)
    check("latency 透传", abs(event["latency"] - 0.41) < 0.001, event)

    short = pc_payload.encode_event(cls="fire", tier="L3", score=0.9, latency=0.4, ts=2.0, fmt="short")
    event = board_protocol.decode(short)
    check("上位机短码 → 板子", event is not None and event["cls"] == "fire" and event["ts"] is None, event)
    check("短码没有 score", event["score"] is None, event)

    heartbeat = board_protocol.heartbeat_bytes("k10-01", 1763265600.0)
    parsed = pc_payload.parse_heartbeat(heartbeat)
    check("板子心跳 → 上位机", parsed is not None and parsed["dev"] == "k10-01", parsed)
    check("心跳 ≤64B", len(heartbeat) <= 64, len(heartbeat))

    check("心跳不会被当成事件", board_protocol.decode(heartbeat) is None)
    check("垃圾载荷不崩", board_protocol.decode(b"{broken") is None)
    check("未知类别不触发", board_protocol.decode(b'{"v":1,"cls":"music"}') is None)

    text_event = board_protocol.decode('{"v":1,"cls":"knock","ts":7.0}')
    check("str 载荷也能解析（K10 的 message() 类型未证实）", text_event is not None and text_event["cls"] == "knock", text_event)
    short_text = board_protocol.decode("DOOR")
    check("str 短码也能解析", short_text is not None and short_text["cls"] == "doorbell", short_text)


def test_dedup() -> None:
    section("去重与重触发")
    clock = FakeClock()
    ui = FakeUi()
    factory = Factory()
    link = make_link(factory, ui, clock)
    link.open()

    event = {"cls": "doorbell", "tier": "L2", "demo": False, "ts": 1000.0, "short": False}

    link.inject(json.dumps(_payload("doorbell", ts=1000.0)).encode())
    clock.now += 100
    link.inject(json.dumps(_payload("doorbell", ts=1000.0)).encode())
    check("同一 ts 只触发一次", len(ui.shows) == 1, ui.shows)

    clock.now += 1500
    link.inject(json.dumps(_payload("doorbell", ts=1000.0)).encode())
    check("同一 ts 超出 1 秒窗口后会再触发（按窗口定义）", len(ui.shows) == 2, ui.shows)

    clock.now += 100
    link.inject(json.dumps(_payload("doorbell", ts=1001.0)).encode())
    check("不同 ts、同类事件必须重触发", len(ui.shows) == 3, ui.shows)

    clock.now += 400
    link.inject(b"KNOCK")
    clock.now += 100
    link.inject(b"KNOCK")
    check("短码 300ms 内去抖", len(ui.shows) == 4, ui.shows)
    clock.now += 400
    link.inject(b"KNOCK")
    check("短码超时后重新触发", len(ui.shows) == 5, ui.shows)

    clock.now += 100
    link.inject(b"FIRE")
    check("不同短码立即触发", len(ui.shows) == 6, ui.shows)
    check("事件记到 last_event", link.last_event is not None)


def _payload(cls, ts):
    return {
        "v": 1,
        "cls": cls,
        "tier": "L2",
        "score": 0.8,
        "latency": 0.4,
        "ts": ts,
        "demo": False,
    }


def test_backoff() -> None:
    section("连接退避 1→16 秒")
    clock = FakeClock()
    ui = FakeUi()
    factory = Factory(connect_ok=False)
    link = make_link(factory, ui, clock)

    link.step()
    check("第一次尝试即失败", len(factory.clients) == 1)
    check("断开时界面显示云端断开", ui.cloud and ui.cloud[-1] is False)

    clock.now = 999
    link.step()
    check("1 秒内不重试", len(factory.clients) == 1)
    clock.now = 1000
    link.step()
    check("1 秒后重试", len(factory.clients) == 2)
    clock.now = 2999
    link.step()
    check("退避 2 秒内不重试", len(factory.clients) == 2)
    clock.now = 3000
    link.step()
    check("退避 2 秒后重试", len(factory.clients) == 3)
    clock.now = 7000
    link.step()
    check("退避 4 秒后重试", len(factory.clients) == 4)
    clock.now = 15000
    link.step()
    check("退避 8 秒后重试", len(factory.clients) == 5)
    clock.now = 31000
    link.step()
    check("退避 16 秒后重试", len(factory.clients) == 6)
    clock.now = 47000
    link.step()
    check("上限保持 16 秒", len(factory.clients) == 7)


def test_heartbeat_and_watchdog() -> None:
    section("心跳与看门狗")
    clock = FakeClock()
    ui = FakeUi()
    factory = Factory()
    link = make_link(factory, ui, clock)
    link.step()
    client = factory.clients[0]
    check("连接后立即发第一拍", len(client.published) == 1, client.published)
    topic, data = client.published[0]
    check("心跳发到状态主题", topic == "hb")
    parsed = pc_payload.parse_heartbeat(data)
    check("上位机认得出心跳", parsed is not None and parsed["dev"] == "k10-01", parsed)
    check("云端状态为已连接", ui.cloud[-1] is True)

    clock.now += 30000
    link.step()
    check("30 秒后再发一拍", len(client.published) == 2, len(client.published))

    section("连续 3 次心跳失败 → 重连（把看门狗调大，只测这条路径）")
    clock = FakeClock()
    ui = FakeUi()
    factory = Factory()
    link = make_link(factory, ui, clock, watchdog_ms=10 ** 9)
    link.step()
    client = factory.clients[0]
    check("已连接", link.connected)
    client.publish_ok = False
    for _ in range(3):
        clock.now += 30000
        link.step()
    check("连续 3 次失败触发重连", not link.connected)
    check("断开后界面回云端断开", ui.cloud[-1] is False)

    clock.now += 1000
    link.step()
    check("退避后重新建连", len(factory.clients) == 2, len(factory.clients))

    section("看门狗：45 秒无成功心跳")
    clock = FakeClock()
    ui = FakeUi()
    factory = Factory()
    link = make_link(factory, ui, clock, heartbeat_ms=10 ** 9)
    link.step()
    check("先连上", link.connected)
    clock.now += 45000
    link.step()
    check("45 秒无心跳成功 → 重连", not link.connected and ui.cloud[-1] is False)

    section("未配状态主题：不心跳、不看门狗")
    clock = FakeClock()
    ui = FakeUi()
    factory = Factory()
    link = make_link(factory, ui, clock, topic_status="")
    link.step()
    client = factory.clients[0]
    clock.now += 120000
    link.step()
    check("不发心跳", client.published == [], client.published)
    check("不因看门狗断开", link.connected)
    link.inject(json.dumps(_payload("doorbell", ts=5.0)).encode())
    check("事件照常触发", len(ui.shows) == 1, ui.shows)


def test_network_drop() -> None:
    section("网络断开与恢复")
    clock = FakeClock()
    ui = FakeUi()
    factory = Factory()
    link = make_link(factory, ui, clock)
    link.step()
    client = factory.clients[0]
    check("已连接", link.connected)

    client.alive = False
    link.step()
    check("检测到断线并关闭连接", not link.connected)
    check("界面显示云端断开", ui.cloud[-1] is False)
    check("客户端被断开", client.disconnects >= 1)

    clock.now += 1000
    link.step()
    check("退避后自动重连", link.connected and len(factory.clients) == 2)
    check("界面恢复云端已连接", ui.cloud[-1] is True)


def test_ignore_bad_input() -> None:
    section("脏数据")
    clock = FakeClock()
    ui = FakeUi()
    factory = Factory()
    link = make_link(factory, ui, clock)
    link.step()
    for junk in (b"", b"hello", b"{oops", b'{"v":2,"cls":"doorbell"}', b'{"v":1,"hb":1}'):
        link.inject(junk)
    check("全部忽略、不抛异常", ui.shows == [], ui.shows)
    check("主循环仍可继续", link.connected)


def advance(fb, clock, ms, step=20):
    end = clock.now + ms
    while clock.now < end:
        clock.now += step
        fb.tick()


def test_feedback() -> None:
    section("外接蜂鸣器 / 震动节奏")
    from feedback import Feedback

    quiet = lambda message: None  # noqa: E731

    clock = FakeClock()
    pins = FakePinFactory()
    fb = Feedback(buzzer_pin=0, vibration_pin=1, pin_factory=pins, clock=clock, log=quiet)
    check("两个通道就绪", fb.ready)
    fb.play("doorbell")
    check("门铃：蜂鸣器立即响", pins.pins[0].digital == [1], pins.pins[0].digital)
    check("门铃：马达立即震", pins.pins[1].digital == [1], pins.pins[1].digital)
    advance(fb, clock, 200)
    check("门铃：180ms 后蜂鸣器停", pins.pins[0].digital[-1] == 0, pins.pins[0].digital)
    advance(fb, clock, 200)
    check("门铃：350ms 后马达停", pins.pins[1].digital[-1] == 0, pins.pins[1].digital)

    clock = FakeClock()
    pins = FakePinFactory()
    fb = Feedback(buzzer_pin=0, vibration_pin=1, pin_factory=pins, clock=clock, log=quiet)
    fb.play("knock")
    advance(fb, clock, 600)
    check("敲门：两声", pins.pins[0].digital.count(1) == 2, pins.pins[0].digital)
    check("敲门：震动也两下", pins.pins[1].digital.count(1) == 2, pins.pins[1].digital)

    clock = FakeClock()
    pins = FakePinFactory()
    fb = Feedback(buzzer_pin=0, pin_factory=pins, clock=clock, log=quiet)
    fb.play("kettle")
    advance(fb, clock, 400)
    check("水壶：长鸣 700ms 未结束", pins.pins[0].digital[-1] == 1, pins.pins[0].digital)
    advance(fb, clock, 400)
    check("水壶：700ms 后停", pins.pins[0].digital[-1] == 0, pins.pins[0].digital)

    clock = FakeClock()
    pins = FakePinFactory()
    fb = Feedback(buzzer_pin=0, pin_factory=pins, clock=clock, log=quiet)
    fb.play("fire")
    advance(fb, clock, 3000)
    check("火警：连续六声", pins.pins[0].digital.count(1) == 6, pins.pins[0].digital)

    clock = FakeClock()
    pins = FakePinFactory()
    fb = Feedback(buzzer_pin=0, buzzer_mode="passive", pin_factory=pins, clock=clock, log=quiet)
    fb.play("doorbell")
    check("无源蜂鸣器走 PWM", pins.pins[0].analog[:1] == [(500, 880)], pins.pins[0].analog)
    advance(fb, clock, 200)
    check("无源蜂鸣器停止时占空比为 0", pins.pins[0].analog[-1][0] == 0, pins.pins[0].analog)

    clock = FakeClock()
    pins = FakePinFactory()
    fb = Feedback(buzzer_pin=0, buzzer_active_high=False, pin_factory=pins, clock=clock, log=quiet)
    fb.play("doorbell")
    check("低电平触发模块", pins.pins[0].digital == [0], pins.pins[0].digital)

    clock = FakeClock()
    pins = FakePinFactory()
    fb = Feedback(buzzer_pin=0, vibration_pin=1, pin_factory=pins, clock=clock, log=quiet)
    fb.play("fire")
    advance(fb, clock, 100)
    before = list(pins.pins[0].digital)
    fb.silence()
    check("silence 立即停", pins.pins[0].digital[-1] == 0 and pins.pins[1].digital[-1] == 0, pins.pins[0].digital)
    advance(fb, clock, 2000)
    check("silence 后不再触发", pins.pins[0].digital == before + [0], pins.pins[0].digital)

    fb = Feedback(log=quiet)
    check("没配引脚就静默降级", fb.ready is False)
    fb.play("doorbell")
    fb.tick()
    fb.silence()
    check("没配引脚也不抛异常", True)


def main() -> int:
    started = time.monotonic()
    test_protocol_contract()
    test_dedup()
    test_backoff()
    test_heartbeat_and_watchdog()
    test_network_drop()
    test_feedback()
    test_ignore_bad_input()

    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"全部通过（{time.monotonic() - started:.2f}s）。")
    print("注：k10_mqtt.py / ui_k10.py 的硬件调用、feedback.py 的真实引脚只能在真板上验证；")
    print("    本测试用假引脚覆盖了节奏逻辑与降级路径。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
