"""K10 界面：屏幕 + 3 颗 WS2812 + 预置 WAV + 外接蜂鸣器/震动。

屏幕布局（240×320 竖屏）：

    ┌──────────────────────────────┐
    │ ● 云端                 k10-01 │  ← 状态条
    │ 15:42:03            [演示]    │
    │                              │
    │            门铃               │  ← 大字（中文可切短码）
    │            DOOR               │
    │  [L2]   得分 0.80 · 0.42s     │
    │                              │
    │  按门铃 / 手机试听             │
    └──────────────────────────────┘

声音仍由 `board/sounds/*.wav` 走 2W 扬声器（D-20）；蜂鸣器与震动是**外接**器件，
节奏见 `feedback.py`，没配引脚就静默跳过。

中文显示取决于固件字体（R-2）：`SHOW_CHINESE=False` 时大字退化为短码。
"""

import time

try:
    import os
except ImportError:  # pragma: no cover
    os = None

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


W = 240
BAR_H = 30

COLORS = {
    "doorbell": 0x3399FF,  # 蓝
    "knock": 0xFFFFFF,     # 白
    "kettle": 0xFFCC00,    # 黄
    "fire": 0xFF2200,      # 红
}
TIER_COLORS = {"L1": 0x2F80ED, "L2": 0xF2C94C, "L3": 0xEB5757}
TEXTS_CN = {"doorbell": "门铃", "knock": "敲门", "kettle": "水壶", "fire": "火警"}
TEXTS_SHORT = {"doorbell": "DOOR", "knock": "KNOCK", "kettle": "KETTLE", "fire": "FIRE"}
SOUND_FILES = {
    "doorbell": "doorbell.wav",
    "knock": "knock.wav",
    "kettle": "kettle.wav",
    "fire": "fire.wav",
}

BAR_BG = 0x101820
TEXT_DIM = 0x8899AA
TEXT_SOFT = 0xCCCCCC
BG = 0x000000

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


def _hms(ts, tz_hours):
    """Unix 时间戳 → HH:MM:SS（不依赖板子 RTC，只做算术）。"""
    if ts is None:
        return "--:--:--"
    try:
        seconds = int(float(ts)) + int(tz_hours * 3600)
    except (TypeError, ValueError):
        return "--:--:--"
    seconds %= 86400
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def _text_width(text, size):
    """粗估文字宽度：中文按 1 个字宽，ASCII 按 0.55。"""
    width = 0
    for char in text:
        width += size if ord(char) > 127 else int(size * 0.55)
    return width


def _center_x(text, size, left=0, width=W):
    return max(left + 2, left + (width - _text_width(text, size)) // 2)


class K10Ui:
    """板子界面。`show(event)` 一次把屏、灯、声音、提醒全部换掉。"""

    def __init__(
        self,
        show_chinese=True,
        feedback=None,
        tz_offset_hours=8,
        device_name="k10-01",
        log=print,
    ):
        self._log = log
        self._show_chinese = show_chinese
        self._feedback = feedback
        self._tz = tz_offset_hours
        self._device_name = device_name
        self._last_event = None
        self._cloud = False
        self._blink_until = 0
        self._blink_at = 0
        self._blink_on = False
        self._draw_error_logged = False

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
        self._draw_idle()

    # ---------- 对外接口（BoardLink 只会调这三个） ----------

    def show(self, event):
        """event 是 `protocol.decode()` 的返回值（cls/tier/demo/ts/score/latency）。"""
        self._last_event = event
        cls = event.get("cls", "")
        self._draw_event(event)
        self._rgb(COLORS.get(cls, 0xFFFFFF))
        self._play(cls)
        if self._feedback is not None:
            try:
                self._feedback.play(cls)
            except Exception as exc:
                self._log("提醒节奏失败：%s" % exc)
        if cls == "fire":
            self._blink_until = _now_ms() + BLINK_TOTAL_MS
            self._blink_at = 0
            self._blink_on = True

    def set_cloud(self, connected):
        self._cloud = bool(connected)
        if not connected:
            self._draw_cloud_down()
            self._rgb(0x220000)
            return
        if self._last_event is None:
            self._draw_idle()
        else:
            self._draw_event(self._last_event)

    def tick(self):
        """火警红闪 + 外接提醒节奏；都跑在主循环里，不阻塞。"""
        now = _now_ms()
        if self._feedback is not None:
            try:
                self._feedback.tick(now)
            except Exception:
                pass
        if not self._blink_until:
            return
        if now >= self._blink_until:
            self._blink_until = 0
            if self._blink_on:
                self._blink_on = False
                self._rgb(0x000000)
            return
        if now - self._blink_at < BLINK_MS:
            return
        self._blink_at = now
        self._blink_on = not self._blink_on
        self._rgb(COLORS["fire"] if self._blink_on else 0x000000)

    # ---------- 绘制 ----------

    def _label(self, cls):
        table = TEXTS_CN if self._show_chinese else TEXTS_SHORT
        return table.get(cls, cls)

    def _rgb(self, color):
        if rgb is None:
            return
        try:
            rgb.write(color=color)
        except Exception as exc:
            self._log_once("RGB 失败：%s" % exc)

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
            self._log_once("刷屏失败：%s" % exc)

    def _fill(self, x, y, w, h, color):
        if screen is None:
            return
        try:
            screen.draw_rect(x=x, y=y, w=w, h=h, bcolor=color, fcolor=color)
        except Exception as exc:
            self._log_once("画矩形失败：%s" % exc)

    def _text(self, text, x, y, size, color):
        if screen is None:
            return
        try:
            screen.draw_text(text=text, x=x, y=y, font_size=size, color=color)
        except Exception as exc:
            self._log_once("绘制失败：%s" % exc)

    def _log_once(self, message):
        if self._draw_error_logged:
            return
        self._draw_error_logged = True
        self._log(message)

    def _status_bar(self):
        self._fill(0, 0, W, BAR_H, BAR_BG)
        color = 0x2ECC71 if self._cloud else 0xEB5757
        self._fill(8, 11, 10, 10, color)
        self._text("云端" if self._cloud else "云端断开", 24, 7, 16, 0xFFFFFF)
        x = W - 8 - _text_width(self._device_name, 14)
        self._text(self._device_name, max(4, x), 8, 14, TEXT_DIM)

    def _draw_event(self, event):
        cls = event.get("cls", "")
        tier = event.get("tier", "L2")
        demo = bool(event.get("demo"))
        color = COLORS.get(cls, 0xFFFFFF)

        self._clear()
        self._status_bar()

        self._text(_hms(event.get("ts"), self._tz), 10, 38, 20, TEXT_DIM)
        if demo:
            self._fill(158, 37, 72, 24, 0xE67E22)
            self._text("演示", 178, 39, 16, 0xFFFFFF)

        label = self._label(cls)
        label_size = 56 if len(label) <= 2 else 44
        self._text(label, _center_x(label, label_size), 92, label_size, color)

        short = TEXTS_SHORT.get(cls, cls.upper())
        self._text(short, _center_x(short, 18), 176, 18, 0x667788)

        self._fill(10, 204, 52, 26, TIER_COLORS.get(tier, 0x888888))
        self._text(tier, _center_x(tier, 18, 10, 52), 208, 18, 0xFFFFFF)

        detail = self._detail(event)
        if detail:
            self._text(detail, 74, 208, 16, TEXT_SOFT)

        self._text("按门铃 / 手机试听", _center_x("按门铃 / 手机试听", 14), 272, 14, 0x556677)
        self._flush()

    def _detail(self, event):
        parts = []
        score = event.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            parts.append("得分 %.2f" % float(score))
        latency = event.get("latency")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            parts.append("%.2fs" % float(latency))
        return " · ".join(parts)

    def _draw_idle(self):
        self._clear()
        self._status_bar()
        self._text("等待事件", _center_x("等待事件", 32), 108, 32, TEXT_SOFT)
        self._text("按门铃 / 手机试听", _center_x("按门铃 / 手机试听", 16), 158, 16, 0x667788)
        self._text(self._device_name, 10, 272, 14, 0x556677)
        self._flush()

    def _draw_cloud_down(self):
        self._clear()
        self._status_bar()
        self._text("云端断开", _center_x("云端断开", 32), 108, 32, 0xEB5757)
        self._text("正在重连…", _center_x("正在重连…", 16), 158, 16, TEXT_DIM)
        self._flush()

    # ---------- 声音 ----------

    def _play(self, cls):
        if speaker is None:
            return
        name = SOUND_FILES.get(cls)
        if not name:
            return
        candidates = (name, "sounds/" + name)
        if os is not None:
            for candidate in candidates:
                try:
                    if os.path.exists(candidate):
                        speaker.play_sys_music(candidate)
                        return
                except Exception:
                    continue
        try:
            speaker.play_sys_music(name)
        except Exception as exc:
            self._log("播放 %s 失败：%s" % (name, exc))
