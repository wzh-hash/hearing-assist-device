"""外接蜂鸣器 / 震动马达的提醒节奏（K10 板载没有这两个器件）。

K10 的 IO 事实（官方 MicroPython 文档）：

- **只有 P0 / P1** 是原生 GPIO，`write_digital()` 与 `write_analog(value, freq)` 都能用；
- 其余引脚经 I2C-GPIO 扩展，**只能拉高/拉低**，没有 PWM；
- 所以：有源蜂鸣器、震动马达可以接任意数字口；**无源蜂鸣器要音调，只能接 P0/P1**。

接口（非阻塞，主循环每 20ms 调一次 `tick`）：

    fb = Feedback(buzzer_pin=0, vibration_pin=1)
    fb.play("doorbell")     # 设定节奏，立刻返回
    fb.tick(now_ms)         # 推进节奏
    fb.silence()            # 立刻停

没配引脚、或没有 `unihiker_k10` 库时全部静默降级，不影响屏幕与上报。
"""

import time

try:
    from unihiker_k10 import pin as k10_pin
except Exception:  # PC 上导入、或固件没有该库
    k10_pin = None


#: 每类事件的节奏：[(电平, 持续毫秒), ...]，1=响/震，0=停
PATTERNS = {
    "doorbell": {
        "buzzer": [(1, 180)],
        "vibration": [(1, 350)],
    },
    "knock": {
        "buzzer": [(1, 90), (0, 90), (1, 90)],
        "vibration": [(1, 130), (0, 110), (1, 130)],
    },
    "kettle": {
        "buzzer": [(1, 700)],
        "vibration": [(1, 700)],
    },
    "fire": {
        "buzzer": [(1, 150), (0, 100)] * 6,
        "vibration": [(1, 220), (0, 120)] * 5,
    },
}

#: 无源蜂鸣器每类事件的音调（Hz）；有源蜂鸣器忽略它
TONES = {"doorbell": 880, "knock": 660, "kettle": 1000, "fire": 1400}


def _now_ms():
    ticks = getattr(time, "ticks_ms", None)
    if ticks is not None:
        return ticks()
    monotonic = getattr(time, "monotonic", None)
    if monotonic is not None:
        return int(monotonic() * 1000)
    return int(time.time() * 1000)


class _Output:
    """一个提醒输出脚：有源器件给高低电平，无源蜂鸣器给 PWM。"""

    def __init__(self, number, *, active_high=True, pwm=False, pin_factory=None, log=print):
        self._active_high = active_high
        self._pwm = pwm
        self._log = log
        self._pin = None
        if number is None:
            return
        factory = pin_factory if pin_factory is not None else k10_pin
        if factory is None:
            log("提醒输出未启用：没有 unihiker_k10.pin（PC 环境正常）")
            return
        try:
            self._pin = factory(number)
        except Exception as exc:
            log("提醒输出初始化失败（引脚 %s）：%s" % (number, exc))

    @property
    def ready(self):
        return self._pin is not None

    def on(self, tone_hz=None):
        if self._pin is None:
            return
        if self._pwm and tone_hz:
            try:
                self._pin.write_analog(value=500, freq=tone_hz)
                return
            except Exception:
                pass
        try:
            self._pin.write_digital(1 if self._active_high else 0)
        except Exception:
            pass

    def off(self):
        if self._pin is None:
            return
        if self._pwm:
            try:
                self._pin.write_analog(value=0, freq=1000)
                return
            except Exception:
                pass
        try:
            self._pin.write_digital(0 if self._active_high else 1)
        except Exception:
            pass


class Feedback:
    """蜂鸣器 + 震动两个通道的节奏播放器，完全不阻塞主循环。"""

    def __init__(
        self,
        buzzer_pin=None,
        vibration_pin=None,
        *,
        buzzer_mode="active",
        buzzer_active_high=True,
        vibration_active_high=True,
        pin_factory=None,
        clock=None,
        log=print,
    ):
        self._clock = clock or _now_ms
        self._buzzer = _Output(
            buzzer_pin,
            active_high=buzzer_active_high,
            pwm=(buzzer_mode == "passive"),
            pin_factory=pin_factory,
            log=log,
        )
        self._vibration = _Output(
            vibration_pin,
            active_high=vibration_active_high,
            pin_factory=pin_factory,
            log=log,
        )
        self._channels = {}

    @property
    def ready(self):
        return self._buzzer.ready or self._vibration.ready

    def play(self, cls):
        """按类别设定节奏；未知类别什么都不做。"""
        now = self._clock()
        pattern = PATTERNS.get(cls)
        if not pattern:
            self.silence()
            return
        self._start("buzzer", pattern.get("buzzer"), now, TONES.get(cls))
        self._start("vibration", pattern.get("vibration"), now, None)

    def tick(self, now=None):
        """推进节奏；主循环每 20ms 调一次即可。"""
        now = self._clock() if now is None else now
        for name in list(self._channels):
            channel = self._channels[name]
            while now >= channel["until"] and channel["index"] < len(channel["schedule"]):
                level, duration = channel["schedule"][channel["index"]]
                channel["index"] += 1
                channel["until"] = now + duration
                if level:
                    channel["out"].on(channel["tone"])
                else:
                    channel["out"].off()
            if channel["index"] >= len(channel["schedule"]) and now >= channel["until"]:
                channel["out"].off()
                del self._channels[name]

    def silence(self):
        outs = []
        for name in list(self._channels):
            outs.append(self._channels[name]["out"])
        self._channels = {}
        for out in outs:
            out.off()
        # 没在节奏里的通道也要确保是关着的；已经在上面关过的不要重复关
        if not any(out is self._buzzer for out in outs):
            self._buzzer.off()
        if not any(out is self._vibration for out in outs):
            self._vibration.off()

    def _start(self, name, schedule, now, tone):
        out = self._buzzer if name == "buzzer" else self._vibration
        if not out.ready or not schedule:
            self._channels.pop(name, None)
            out.off()
            return
        self._channels[name] = {
            "schedule": schedule,
            "index": 0,
            "until": now,
            "out": out,
            "tone": tone,
        }
        # 第一下立刻响/震，不等下一次 tick（20ms 的差别在"门铃响了"这件事上很值）
        self.tick(now)
