"""K10 界面：屏幕 + 3 颗 WS2812 + 预置 WAV。

MicroPython 层**没有音调 API**（官方只有 `speaker.play_sys_music` 回放文件），
所以 IOT-6.1–6.4 的"短鸣 / 两声 / 长鸣 / 连续鸣"由 `board/sounds/*.wav`
承担（D-20）。把 4 个 wav 上传到板子根目录（与 main.py 同级），
文件名见下面的 `SOUND_FILES`。

中文显示取决于固件自带字体，社区反馈默认支持很差（R-2）。`SHOW_CHINESE=False`
时退化为短码 `DOOR / KNOCK / KETTLE / FIRE`，保证"至少能读出来"。
"""

import time

try:
    from unihiker_k10 import screen
except Exception:  # PC 上导入本模块时没有硬件库
    screen = None

try:
    from unihiker_k10 import rgb
except Exception:
    rgb = None

try:
    from k10_base import speaker
except Exception:
    speaker = None


COLORS = {
    "doorbell": 0x3399FF,  # 蓝
    "knock": 0xFFFFFF,     # 白
    "kettle": 0xFFCC00,    # 黄
    "fire": 0xFF2200,      # 红
}
TEXTS_CN = {"doorbell": "门铃", "knock": "敲门", "kettle": "水壶", "fire": "火警"}
TEXTS_SHORT = {"doorbell": "DOOR", "knock": "KNOCK", "kettle": "KETTLE", "fire": "FIRE"}
SOUND_FILES = {
    "doorbell": "doorbell.wav",
    "knock": "knock.wav",
    "kettle": "kettle.wav",
    "fire": "fire.wav",
}

FONT_BIG = 48
FONT_SMALL = 20
BLINK_MS = 400
BLINK_TOTAL_MS = 5000


def _now_ms():
    ticks = getattr(time, "ticks_ms", None)
    if ticks is not None:
        return ticks()
    monotonic = getattr(time, "monotonic", None)
    if monotonic is not None:
        return int(monotonic() * 1000)
    return int(time.time() * 1000)


class K10Ui:
    """板子界面。`show()` 一次把屏、灯、声音全部换掉。"""

    def __init__(self, show_chinese=True, log=print):
        self._log = log
        self._show_chinese = show_chinese
        self._last = None
        self._last_tier = "L2"
        self._last_demo = False
        self._cloud = True
        self._blink_until = 0
        self._blink_at = 0
        self._blink_on = False

        if screen is not None:
            try:
                screen.init(dir=2)
            except Exception as exc:
                self._log("屏幕初始化失败：%s" % exc)
        if rgb is not None:
            try:
                rgb.brightness(9)
            except Exception:
                pass
        self._render_idle()

    # ---------- 对外接口（BoardLink 只会调这三个） ----------

    def show(self, cls, tier, demo):
        self._last = cls
        self._last_tier = tier
        self._last_demo = demo
        self._draw_event(cls, tier, demo)
        if rgb is not None:
            try:
                rgb.write(color=COLORS.get(cls, 0xFFFFFF))
            except Exception as exc:
                self._log("RGB 失败：%s" % exc)
        self._play(cls)
        if cls == "fire":
            self._blink_until = _now_ms() + BLINK_TOTAL_MS
            self._blink_at = 0
            self._blink_on = True

    def set_cloud(self, connected):
        self._cloud = connected
        if not connected:
            self._draw_cloud_down()
            if rgb is not None:
                try:
                    rgb.write(color=0x220000)
                except Exception:
                    pass
            return
        if self._last is None:
            self._render_idle()
        else:
            self._draw_event(self._last, self._last_tier, self._last_demo)

    def tick(self):
        """火警的红色闪烁；5 秒后停在熄灭状态。"""
        now = _now_ms()
        if not self._blink_until:
            return
        if now >= self._blink_until:
            self._blink_until = 0
            if self._blink_on:
                self._blink_on = False
                if rgb is not None:
                    try:
                        rgb.write(color=0x000000)
                    except Exception:
                        pass
            return
        if now - self._blink_at < BLINK_MS:
            return
        self._blink_at = now
        self._blink_on = not self._blink_on
        if rgb is not None:
            try:
                rgb.write(color=COLORS["fire"] if self._blink_on else 0x000000)
            except Exception:
                pass

    # ---------- 绘制 ----------

    def _label(self, cls):
        table = TEXTS_CN if self._show_chinese else TEXTS_SHORT
        return table.get(cls, cls)

    def _clear(self):
        if screen is None:
            return
        try:
            screen.clear()
        except Exception:
            pass

    def _flush(self):
        if screen is None:
            return
        try:
            screen.show_draw()
        except Exception as exc:
            self._log("刷屏失败：%s" % exc)

    def _draw_event(self, cls, tier, demo):
        self._clear()
        if screen is not None:
            try:
                screen.draw_text(
                    text=self._label(cls),
                    x=30,
                    y=120,
                    font_size=FONT_BIG,
                    color=COLORS.get(cls, 0xFFFFFF),
                )
                note = tier + (" · 演示" if demo else "")
                screen.draw_text(text=note, x=30, y=200, font_size=FONT_SMALL, color=0xAAAAAA)
            except Exception as exc:
                self._log("绘制失败：%s" % exc)
        self._flush()

    def _draw_cloud_down(self):
        self._clear()
        if screen is not None:
            try:
                screen.draw_text(text="云端断开", x=40, y=140, font_size=28, color=0xFF2200)
            except Exception:
                pass
        self._flush()

    def _render_idle(self):
        self._last = None
        self._clear()
        if screen is not None:
            try:
                screen.draw_text(text="等待事件", x=60, y=140, font_size=FONT_SMALL, color=0x888888)
            except Exception:
                pass
        self._flush()
        if rgb is not None:
            try:
                rgb.clear()
            except Exception:
                pass

    # ---------- 声音 ----------

    def _play(self, cls):
        if speaker is None:
            return
        name = SOUND_FILES.get(cls)
        if not name:
            return
        try:
            speaker.play_sys_music(name)
        except Exception as exc:
            self._log("播放 %s 失败：%s" % (name, exc))
