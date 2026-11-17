"""PC 上的"模拟板子"：用真连接跑固件那套状态机。

    python board/sim_board.py status
    python board/sim_board.py listen
    python board/sim_board.py send --cls doorbell --repeat 2
    python board/sim_board.py send --cls fire --short

为什么值得跑：`listen` 复用的就是 `board/mqtt_link.py` 里的 `BoardLink`——同一个
去重、心跳、看门狗、退避重连，只把 MQTT 适配器换成 paho、界面换成终端打印。
所以"模拟板子表现对"能覆盖到真板子才能覆盖的那一半逻辑。

依赖：`sounds/iot.json` 里要有可用的 Iot_id / Iot_pwd / 事件主题、状态主题。
模拟器用 `<本机标识>-sim` 作为客户端标识，不会和正在运行的上位机互相踢下线。
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT), str(ROOT / "board")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import protocol  # noqa: E402  （board/protocol.py）

from host.iot import payload as pc_payload  # noqa: E402
from host.iot.link import paho_client_factory  # noqa: E402
from host.iot.settings import load  # noqa: E402

ACTIONS = {
    "doorbell": "屏幕「门铃」 · 短鸣 · 蓝色",
    "knock": "屏幕「敲门」 · 两声短鸣",
    "kettle": "屏幕「水壶」 · 长鸣 · 黄色",
    "fire": "屏幕「火警」 · 连续鸣 · 红色闪烁",
}


class ConsoleUi:
    """界面的终端替身：只打印板子"会做什么"。"""

    def __init__(self) -> None:
        self._last = ""

    def show(self, event):
        stamp = time.strftime("%H:%M:%S")
        cls = event.get("cls", "")
        mark = "（试听）" if event.get("preview") else ""
        detail = ""
        if isinstance(event.get("score"), (int, float)):
            detail += " 得分 %.2f" % float(event["score"])
        if isinstance(event.get("latency"), (int, float)):
            detail += " 延迟 %.2fs" % float(event["latency"])
        line = (
            f"[{stamp}] 板子动作{mark}：{ACTIONS.get(cls, cls)}"
            f"  [{event.get('tier', 'L2')}]{detail}"
        )
        print(line)
        self._last = line

    def set_cloud(self, connected):
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {'云端已连接' if connected else '云端断开'}")

    def tick(self):
        return None


class PahoBoardClient:
    """把 paho 包成固件期望的那个小接口。"""

    def __init__(self, on_message, settings, client_id):
        self._on_message = on_message
        self._settings = settings
        self._client = paho_client_factory(settings, client_id=client_id)
        self._client.on_message = self._on_paho_message
        self._started = False

    def _on_paho_message(self, client, userdata, message):
        try:
            self._on_message(message.topic, message.payload)
        except Exception as exc:
            print(f"[sim] 回调异常：{exc}")

    def connect(self):
        settings = self._settings
        try:
            self._client.connect(settings.host, settings.port, keepalive=60)
        except Exception as exc:
            print(f"[sim] 连接失败：{exc}")
            return False
        self._client.loop_start()
        self._started = True
        return True

    def subscribe(self, topic):
        result = self._client.subscribe(topic, qos=0)
        rc = result[0] if isinstance(result, tuple) else getattr(result, "rc", 1)
        if rc:
            print(f"[sim] 订阅失败：{topic}")
            return False
        print(f"[sim] 已订阅：{topic}")
        return True

    def publish(self, topic, data):
        info = self._client.publish(topic, data, qos=self._settings.qos, retain=False)
        return getattr(info, "rc", 1) == 0

    def poll(self):
        return None  # paho 自带网络线程，不需要外部轮询

    def is_connected(self):
        return bool(self._client.is_connected())

    def disconnect(self):
        try:
            if self._started:
                self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


def require_settings():
    settings = load()
    if not settings.configured:
        print("未配置：请先在 sounds/iot.json 填好 Iot_id / Iot_pwd / 事件主题。")
        print("（页面入口由网页前端提供；当前可手写该文件，字段见 docs/REQ-01-iot.md §4 IOT-1.2）")
        raise SystemExit(2)
    return settings


def cmd_status(_args):
    settings = load()
    data = settings.masked()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print("已配置" if settings.configured else "未配置（缺 Iot_id / Iot_pwd / 事件主题）")
    return 0


def cmd_listen(_args):
    from mqtt_link import BoardLink

    settings = require_settings()
    client_id = f"{settings.client_id}-sim"
    factory = lambda on_message: PahoBoardClient(on_message, settings, client_id)  # noqa: E731
    link = BoardLink(
        factory,
        ConsoleUi(),
        settings.event_topic,
        topic_status=settings.status_topic,
        device=settings.device_name,
    )
    print(f"[sim] 模拟板子启动，客户端标识 {client_id}，Ctrl+C 退出")
    try:
        link.run()
    except KeyboardInterrupt:
        link.close()
        print("\n[sim] 已退出")
    return 0


def cmd_send(args):
    settings = require_settings()
    if not settings.event_topic:
        print("事件主题为空，无法发送")
        return 2

    ts = args.ts or time.time()
    for index in range(args.repeat):
        current = ts if (args.same_ts or index == 0) else ts + index * args.gap
        if args.short:
            data = pc_payload.encode_event(
                cls=args.cls, tier=args.tier, score=0.8, latency=0.4,
                ts=current, fmt="short",
            )
        else:
            data = pc_payload.encode_event(
                cls=args.cls, tier=args.tier, score=0.8, latency=0.4,
                ts=current, preview=args.preview,
            )
        client = paho_client_factory(settings, client_id=f"{settings.client_id}-sim-send")
        try:
            client.connect(settings.host, settings.port, keepalive=20)
            client.loop_start()
            info = client.publish(settings.event_topic, data, qos=settings.qos, retain=False)
            time.sleep(0.3)
            ok = getattr(info, "rc", 1) == 0
            print(f"[sim] 第 {index + 1}/{args.repeat} 条 {'已发' if ok else '发送失败'}：ts={round(current, 3)} {data[:80]!r}")
        except Exception as exc:
            print(f"[sim] 发送失败：{exc}")
            return 1
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        if index + 1 < args.repeat:
            time.sleep(args.gap)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="模拟板子（复用固件状态机）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="打印当前配置（密码掩码）")

    listen = sub.add_parser("listen", help="订阅事件主题，打印板子动作，每 30 秒发心跳")
    listen.set_defaults(func=cmd_listen)

    send = sub.add_parser("send", help="向上位机的事件主题发一条事件（用于验收 AC-IOT-7）")
    send.add_argument("--cls", default="doorbell", choices=protocol.CLASSES)
    send.add_argument("--tier", default="L2", choices=protocol.TIERS)
    send.add_argument("--ts", type=float, default=0.0, help="固定时间戳；0 表示用当前时间")
    send.add_argument("--repeat", type=int, default=1)
    send.add_argument("--gap", type=float, default=1.0, help="重复发送的间隔秒数（仅 --new-ts 时生效）")
    send.add_argument("--same-ts", action="store_true", default=True, help="重复发送时复用同一 ts（测去重）")
    send.add_argument("--new-ts", dest="same_ts", action="store_false", help="重复发送时递增 ts（测重触发）")
    send.add_argument("--short", action="store_true", help="发短码而不是 JSON")
    send.add_argument("--preview", action="store_true", help="标记为试听命中")
    send.set_defaults(func=cmd_send)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
