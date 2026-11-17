"""K10 固件入口：开机自动运行。

固件文件名必须是 `main.py`（官方部署方式：Thonny 保存到 MicroPython 设备，命名 main.py）。

板子根目录应有的文件：

    main.py  mqtt_link.py  protocol.py  k10_mqtt.py  ui_k10.py  feedback.py  config.py
    doorbell.wav  knock.wav  kettle.wav  fire.wav

其中 `config.py` 由 `config.example.py` 复制填写（不进版本库），4 个 wav 由
`tools/make_board_sounds.py` 生成。

启动流程：显示「启动中」→ 连 WiFi（阻塞，最多 timeout）→ 连 MQTT 并订阅
→ 进入 `BoardLink.run()`。心跳与断线重连都在 BoardLink 里，这里不做业务。
"""


def _show(text, color=0xFFFFFF, font_size=26):
    try:
        from unihiker_k10 import screen

        screen.clear()
        screen.draw_text(text=text, x=20, y=130, font_size=font_size, color=color)
        screen.show_draw()
    except Exception:
        pass


def main():
    try:
        import config
    except ImportError:
        _show("缺少 config.py", 0xFF2200)
        print("[k10] 缺少 config.py：请复制 config.example.py 并填写")
        return 1

    _show("启动中…")

    from feedback import Feedback
    from k10_mqtt import make_factory
    from mqtt_link import BoardLink
    from ui_k10 import K10Ui

    feedback = Feedback(
        buzzer_pin=getattr(config, "BUZZER_PIN", None),
        vibration_pin=getattr(config, "VIBRATION_PIN", None),
        buzzer_mode=getattr(config, "BUZZER_MODE", "active"),
        buzzer_active_high=getattr(config, "BUZZER_ACTIVE_HIGH", True),
        vibration_active_high=getattr(config, "VIBRATION_ACTIVE_HIGH", True),
    )
    ui = K10Ui(
        show_chinese=getattr(config, "SHOW_CHINESE", True),
        feedback=feedback,
        tz_offset_hours=getattr(config, "TZ_OFFSET_HOURS", 8),
        device_name=getattr(config, "DEVICE_NAME", "k10-01"),
    )
    link = BoardLink(
        make_factory(config),
        ui,
        config.EVENT_TOPIC,
        topic_status=getattr(config, "STATUS_TOPIC", ""),
        device=getattr(config, "DEVICE_NAME", "k10-01"),
        heartbeat_ms=getattr(config, "HEARTBEAT_MS", 30000),
    )
    print("[k10] 进入主循环")
    link.run()
    return 0


if __name__ == "__main__":
    main()
