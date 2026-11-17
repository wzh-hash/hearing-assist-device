"""K10 界面：LVGL 标签 + 外置英文字库（48px / 20px）+ 3 颗 WS2812 + WAV。

界面**只用英文**（DOOR / KNOCK / KETTLE / FIRE、CLOUD、WAIT…），不依赖中文字形；
K10 官方 MicroPython 固件内置字体不含中文、`screen.draw_text` 字号也不受控（D-24）。

字库由 `tools/make_board_fonts.py` 生成（只含 ASCII），放在板子根目录；
**buffer 必须一直被引用**，LVGL 不拷贝字库数据。

    data = open("font_ui48.bin", "rb").read()
    font = lv.binfont_create_from_buffer(data, len(data))
    label.set_style_text_font(font, 0)

三层降级：
1. 正常：LVGL + 48px/20px 字库；
2. 字库缺失：LVGL 内置字体（字小，但内容不变）；
3. LVGL 不可用：退回 `screen.draw_text`（纯 ASCII 小字）。

屏幕布局（240×320，无底部提示行）：

    ● CLOUD                k10-01
    15:42:03              DEMO
              DOOR
    [L2]    SCORE 0.80 · 0.42s
"""

import time

try:
    import os
except ImportError:  # pragma: no cover
    os = None

try:
    import lvgl as lv
except Exception:  # PC 上导入、或固件没有 LVGL
    lv = None

try:
    from unihiker_k10 import screen
except Exception:
    screen = None

try:
    from unihiker_k10 import rgb
except Exception:
    rgb = None

try:
    from k10_base import speaker
except Exception:
    speaker = None


FONT_BIG_FILE = "font_ui48.bin"
FONT_SMALL_FILE = "font_ui20.bin"

COLORS = {
    "doorbell": 0x3399FF,  # 蓝
    "knock": 0xFFFFFF,     # 白
    "kettle": 0xFFCC00,    # 黄
    "fire": 0xFF2200,      # 红
}
TIER_COLORS = {"L1": 0x2F80ED, "L2": 0xF2C94C, "L3": 0xEB5757}
TEXTS_SHORT = {"doorbell": "DOOR", "knock": "KNOCK", "kettle": "KETTLE", "fire": "FIRE"}
SOUND_FILES = {
    "doorbell": "doorbell.wav",
    "knock": "knock.wav",
    "kettle": "kettle.wav",
    "fire": "fire.wav",
}

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
    if ts is None:
        return "--:--:--"
    try:
        seconds = int(float(ts)) + int(tz_hours * 3600)
    except (TypeError, ValueError):
        return "--:--:--"
    seconds %= 86400
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


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
        self._error_logged = False
        self._font_buffers = []

        self._mode = "none"
        if lv is not None and screen is not None:
            try:
                self._init_lvgl()
                self._mode = "lvgl"
            except Exception as exc:  # noqa: BLE001
                self._log("LVGL 界面初始化失败（%s），回退到 canvas" % exc)
                self._mode = "canvas"

        if self._mode in ("canvas", "none"):
            try:
                if screen is not None:
                    screen.init(dir=2)
                self._mode = "canvas"
            except Exception as exc:  # noqa: BLE001
                self._log("屏幕初始化失败：%s" % exc)
                self._mode = "none"

        if rgb is not None:
            try:
                rgb.brightness(9)
            except Exception:
                pass
        self._draw_idle()

    # ---------- 初始化 ----------

    def _load_font(self, name):
        try:
            raw = open(name, "rb").read()
        except OSError:
            raise RuntimeError("缺少字库 %s" % name)
        try:
            font = lv.binfont_create_from_buffer(raw, len(raw))
        except TypeError:
            font = lv.binfont_create_from_buffer(raw)
        if font is None:
            raise RuntimeError("字库加载失败 %s" % name)
        self._font_buffers.append(raw)  # 必须保活：LVGL 不拷贝字库数据
        return font

    def _init_lvgl(self):
        screen.init(dir=2)
        self._scrn = lv.screen_active() if hasattr(lv, "screen_active") else lv.scr_act()
        self._scrn.set_style_bg_color(lv.color_hex(0x000000), 0)

        self._fonts_ok = False
        try:
            self._font_big = self._load_font(FONT_BIG_FILE)
            self._font_small = self._load_font(FONT_SMALL_FILE)
            self._fonts_ok = True
        except Exception as exc:  # noqa: BLE001
            self._log("字库不可用（%s），改用内置字体的英文短码" % exc)
            self._font_big = getattr(lv, "font_k10_24", None) or lv.font_montserrat_24
            self._font_small = getattr(lv, "font_k10_16", None) or lv.font_montserrat_16

        self._dot = None
        try:
            self._dot = lv.obj(self._scrn)
            self._dot.set_size(10, 10)
            self._dot.set_style_bg_color(lv.color_hex(0xEB5757), 0)
            self._dot.set_style_border_width(0, 0)
            self._dot.set_style_radius(5, 0)
            self._dot.set_style_pad_all(0, 0)
            self._dot.align(lv.ALIGN.TOP_LEFT, 8, 11)
        except Exception:
            self._dot = None

        self._status = self._label(self._font_small, 0xFFFFFF, lv.ALIGN.TOP_LEFT, 24, 6)
        self._device = self._label(self._font_small, 0x8899AA, lv.ALIGN.TOP_RIGHT, -8, 6)
        self._time = self._label(self._font_small, 0x8899AA, lv.ALIGN.TOP_LEFT, 10, 38)
        self._demo = self._label(self._font_small, 0xE67E22, lv.ALIGN.TOP_RIGHT, -10, 34)
        self._big = self._label(self._font_big, 0xFFFFFF, lv.ALIGN.CENTER, 0, -50)
        self._short = self._label(self._font_small, 0x667788, lv.ALIGN.CENTER, 0, 20)
        self._tier = self._label(self._font_small, 0xFFFFFF, lv.ALIGN.BOTTOM_LEFT, 10, -40)
        self._badge(self._tier, 0x2F80ED)
        self._detail = self._label(self._font_small, 0xCCCCCC, lv.ALIGN.BOTTOM_LEFT, 78, -46)

        self._set_text(self._device, self._device_name)

    def _label(self, font, color, align, x, y):
        label = lv.label(self._scrn)
        label.set_style_text_font(font, 0)
        label.set_style_text_color(lv.color_hex(color), 0)
        label.align(align, x, y)
        return label

    def _badge(self, label, color):
        try:
            label.set_style_bg_color(lv.color_hex(color), 0)
            label.set_style_bg_opa(lv.OPA.COVER, 0)
            label.set_style_pad_all(4, 0)
            label.set_style_radius(4, 0)
        except Exception:
            pass

    # ---------- 对外接口 ----------

    def show(self, event):
        self._last_event = event
        cls = event.get("cls", "")
        if self._mode == "lvgl":
            self._show_lvgl(event)
        else:
            self._show_canvas(event)
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
            self.show(self._last_event)

    def tick(self):
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
                if self._mode == "lvgl" and self._last_event is not None:
                    self._set_color(self._big, COLORS.get(self._last_event.get("cls"), 0xFFFFFF))
            return
        if now - self._blink_at < BLINK_MS:
            return
        self._blink_at = now
        self._blink_on = not self._blink_on
        self._rgb(COLORS["fire"] if self._blink_on else 0x000000)
        if self._mode == "lvgl":
            self._set_color(self._big, COLORS["fire"] if self._blink_on else 0x330000)

    # ---------- LVGL 绘制 ----------

    def _set_text(self, label, text):
        try:
            if label is not None:
                label.set_text(text)
        except Exception as exc:  # noqa: BLE001
            self._log_once("设置文字失败：%s" % exc)

    def _set_color(self, label, color):
        try:
            if label is not None:
                label.set_style_text_color(lv.color_hex(color), 0)
        except Exception:
            pass

    def _label_text(self, cls):
        return TEXTS_SHORT.get(cls, cls.upper())

    def _set_status(self, online):
        self._set_text(self._status, "CLOUD" if online else "LINK DOWN")
        self._set_color(self._status, 0xFFFFFF if online else 0xFF6B6B)
        try:
            if self._dot is not None:
                self._dot.set_style_bg_color(lv.color_hex(0x2ECC71 if online else 0xEB5757), 0)
        except Exception:
            pass

    def _show_lvgl(self, event):
        cls = event.get("cls", "")
        tier = event.get("tier", "L2")
        demo = bool(event.get("demo"))
        color = COLORS.get(cls, 0xFFFFFF)

        self._set_status(True)
        self._set_text(self._time, _hms(event.get("ts"), self._tz))
        self._set_text(self._demo, "DEMO" if demo else "")
        self._set_text(self._big, self._label_text(cls))
        self._set_color(self._big, color)
        self._set_text(self._short, TEXTS_SHORT.get(cls, cls.upper()))
        self._set_text(self._tier, tier)
        self._badge(self._tier, TIER_COLORS.get(tier, 0x888888))
        self._set_text(self._detail, self._detail_text(event))

    def _detail_text(self, event):
        parts = []
        score = event.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            parts.append("SCORE %.2f" % float(score))
        latency = event.get("latency")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            parts.append("%.2fs" % float(latency))
        return " · ".join(parts)

    def _draw_idle(self):
        if self._mode == "lvgl":
            if self._last_event is None:
                self._set_text(self._big, "WAIT")
                self._set_color(self._big, 0xCCCCCC)
                self._set_text(self._short, "")
                self._set_text(self._detail, "")
                self._set_text(self._demo, "")
                self._set_text(self._time, "--:--:--")
                self._set_text(self._tier, "")
            self._set_status(self._cloud)
            return
        if self._mode == "canvas":
            self._canvas_clear()
            self._canvas_text("WAIT", 10, 100, 24, 0xCCCCCC)
            self._canvas_text("CLOUD " + ("ON" if self._cloud else "OFF"), 10, 40, 16, 0x8899AA)
            self._canvas_flush()

    def _draw_cloud_down(self):
        if self._mode == "lvgl":
            self._set_status(False)
            self._set_text(self._big, "LINK DOWN")
            self._set_color(self._big, 0xEB5757)
            self._set_text(self._short, "")
            self._set_text(self._detail, "")
            self._set_text(self._tier, "")
            return
        if self._mode == "canvas":
            self._canvas_clear()
            self._canvas_text("LINK DOWN", 10, 100, 24, 0xEB5757)
            self._canvas_flush()

    # ---------- canvas 降级（纯 ASCII，字号受限） ----------

    def _show_canvas(self, event):
        cls = event.get("cls", "")
        tier = event.get("tier", "L2")
        short = TEXTS_SHORT.get(cls, cls.upper())
        self._canvas_clear()
        self._canvas_text(short, 10, 90, 24, COLORS.get(cls, 0xFFFFFF))
        self._canvas_text(tier + "  " + _hms(event.get("ts"), self._tz), 10, 140, 16, 0x8899AA)
        detail = self._detail_text(event)
        if detail:
            self._canvas_text(detail, 10, 170, 16, 0xCCCCCC)
        self._canvas_text("CLOUD ON", 10, 40, 16, 0x2ECC71)
        self._canvas_flush()

    def _canvas_text(self, text, x, y, size, color):
        try:
            if screen is not None:
                screen.draw_text(text=text, x=x, y=y, font_size=size, color=color)
        except Exception as exc:  # noqa: BLE001
            self._log_once("canvas 绘制失败：%s" % exc)

    def _canvas_clear(self):
        try:
            if screen is not None:
                screen.clear()
                screen.show_bg(color=0x000000)
        except Exception:
            pass

    def _canvas_flush(self):
        try:
            if screen is not None:
                screen.show_draw()
        except Exception:
            pass

    # ---------- 其他 ----------

    def _rgb(self, color):
        if rgb is None:
            return
        try:
            rgb.write(color=color)
        except Exception as exc:
            self._log_once("RGB 失败：%s" % exc)

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

    def _log_once(self, message):
        if self._error_logged:
            return
        self._error_logged = True
        self._log(message)
