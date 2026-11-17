"""K10 界面：信号灯牌（Signal Board）。

设计原则
--------
- **一个词**：整块屏只回答"发生了什么"——`DOOR` / `KNOCK` / `KETTLE` / `FIRE`。
- **一种颜色**：颜色只表达等级，不表达类别（D-25）：
  L1 蓝 `#2F80ED` / L2 黄 `#F2C94C` / L3 红 `#EB5757`。
  门铃与敲门同为 L1，因此同色；类别靠文字与蜂鸣节奏区分。
- **第二维度是节奏**：L1 常亮、L2 呼吸一次、L3 连闪（5 秒后停在常亮）。
- **其余全部退灰**：状态、时间、边角标记只用灰阶，不抢主色。
- 不显示得分、延迟、设备名、底部提示。

布局（240×320）
---------------
    ● CLOUD                 15:42:03
                  DOOR
                 [ L2 ]
                                    TEST

字体：`font_msg48.bin`（Arial Bold 48，消息）/ `font_meta18.bin`（Arial 18，状态）。
字库只含 ASCII，用 LVGL 从内存加载（buffer 保活）；缺字库回退内置字体，
LVGL 不可用时回退 `screen.draw_text`。
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


FONT_MSG_FILE = "font_msg48.bin"
FONT_META_FILE = "font_meta18.bin"

#: 等级 → 颜色（唯一色相来源）
TIER_COLORS = {"L1": 0x2F80ED, "L2": 0xF2C94C, "L3": 0xEB5757}
TEXTS_SHORT = {"doorbell": "DOOR", "knock": "KNOCK", "kettle": "KETTLE", "fire": "FIRE"}
SOUND_FILES = {
    "doorbell": "doorbell.wav",
    "knock": "knock.wav",
    "kettle": "kettle.wav",
    "fire": "fire.wav",
}

BG = 0x000000
META = 0x8A8A8A
META_DIM = 0x555555
ONLINE_DOT = 0x8A8A8A
OFFLINE_DOT = 0xEB5757

BLINK_MS = 400
BLINK_TOTAL_MS = 5000
BREATH_AT_MS = 700
BREATH_OFF_MS = 300


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


def _luminance(hex_color):
    red = (hex_color >> 16) & 0xFF
    green = (hex_color >> 8) & 0xFF
    blue = hex_color & 0xFF
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


class K10Ui:
    """板子界面。`show(event)` 一次把屏、灯、声音、提醒全部换掉。"""

    def __init__(
        self,
        show_chinese=True,  # 兼容旧参数；界面固定英文
        feedback=None,
        tz_offset_hours=8,
        device_name="k10-01",  # 兼容旧参数；界面不再显示设备名
        log=print,
    ):
        self._log = log
        self._feedback = feedback
        self._tz = tz_offset_hours
        self._last_event = None
        self._cloud = False
        self._motion = None
        self._font_buffers = []
        self._error_logged = False

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
                rgb.write(color=0x000000)
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
        self._scrn.set_style_bg_color(lv.color_hex(BG), 0)

        self._fonts_ok = False
        try:
            self._font_msg = self._load_font(FONT_MSG_FILE)
            self._font_meta = self._load_font(FONT_META_FILE)
            self._fonts_ok = True
        except Exception as exc:  # noqa: BLE001
            self._log("字库不可用（%s），改用内置字体" % exc)
            self._font_msg = getattr(lv, "font_k10_24", None) or lv.font_montserrat_24
            self._font_meta = getattr(lv, "font_k10_16", None) or lv.font_montserrat_16

        self._dot = None
        try:
            self._dot = lv.obj(self._scrn)
            self._dot.set_size(10, 10)
            self._dot.set_style_bg_color(lv.color_hex(OFFLINE_DOT), 0)
            self._dot.set_style_border_width(0, 0)
            self._dot.set_style_radius(5, 0)
            self._dot.set_style_pad_all(0, 0)
            self._dot.align(lv.ALIGN.TOP_LEFT, 8, 11)
        except Exception:
            self._dot = None

        self._status = self._label(self._font_meta, META, lv.ALIGN.TOP_LEFT, 24, 5)
        self._time = self._label(self._font_meta, META, lv.ALIGN.TOP_RIGHT, -10, 5)
        self._msg = self._label(self._font_msg, 0xFFFFFF, lv.ALIGN.CENTER, 0, -20)
        self._tier = self._label(self._font_meta, 0xFFFFFF, lv.ALIGN.CENTER, 0, 55)
        self._preview = self._label(self._font_meta, META_DIM, lv.ALIGN.BOTTOM_RIGHT, -10, -10)

    def _label(self, font, color, align, x, y):
        label = lv.label(self._scrn)
        label.set_style_text_font(font, 0)
        label.set_style_text_color(lv.color_hex(color), 0)
        label.align(align, x, y)
        return label

    def _badge(self, label, color):
        try:
            label.set_style_text_color(lv.color_hex(color), 0)
            label.set_style_border_color(lv.color_hex(color), 0)
            label.set_style_border_width(2, 0)
            label.set_style_radius(3, 0)
            label.set_style_pad_all(6, 0)
        except Exception:
            pass

    # ---------- 对外接口 ----------

    def show(self, event):
        self._last_event = event
        tier, color = self._render(event)
        self._start_motion(tier, color)
        self._play(event.get("cls", ""))
        if self._feedback is not None:
            try:
                self._feedback.play(event.get("cls", ""))
            except Exception as exc:
                self._log("提醒节奏失败：%s" % exc)

    def _render(self, event):
        """只更新屏幕内容，不触发声音/提醒（重连恢复时复用）。"""
        cls = event.get("cls", "")
        tier = event.get("tier") or "L1"
        color = TIER_COLORS.get(tier, TIER_COLORS["L1"])
        text = TEXTS_SHORT.get(cls, cls.upper())
        with_preview = bool(event.get("preview"))

        if self._mode == "lvgl":
            self._set_text(self._msg, text)
            self._set_color(self._msg, color)
            self._set_text(self._tier, tier)
            self._badge(self._tier, color)
            self._set_text(self._preview, "TEST" if with_preview else "")
            self._set_text(self._time, _hms(event.get("ts"), self._tz))
            self._set_status(True)
        else:
            self._show_canvas(text, tier, color, event, with_preview)
        return tier, color

    def set_cloud(self, connected):
        self._cloud = bool(connected)
        if not connected:
            self._motion = None
            self._rgb(0x000000)
            self._draw_cloud_down()
            return
        if self._last_event is None:
            self._draw_idle()
        else:
            # 重连恢复：重画当前事件，但不重放声音与提醒
            tier, color = self._render(self._last_event)
            self._start_motion(tier, color)

    def tick(self):
        self._tick_motion(_now_ms())
        if self._feedback is not None:
            try:
                self._feedback.tick()
            except Exception:
                pass

    # ---------- 等级动效 ----------

    def _start_motion(self, tier, color):
        now = _now_ms()
        self._motion = {"tier": tier, "color": color, "now": now, "phase": 0}
        self._rgb(color)
        if tier == "L2":
            self._motion.update(kind="breath", next=now + BREATH_AT_MS)
        elif tier == "L3":
            self._motion.update(kind="blink", on=True, next=now + BLINK_MS, stop=now + BLINK_TOTAL_MS)
        else:
            self._motion.update(kind="steady")

    def _tick_motion(self, now):
        motion = self._motion
        if not motion:
            return
        kind = motion.get("kind")
        if kind == "breath":
            if motion["phase"] == 0 and now >= motion["next"]:
                self._rgb(0x000000)
                motion["phase"] = 1
                motion["next"] = now + BREATH_OFF_MS
            elif motion["phase"] == 1 and now >= motion["next"]:
                self._rgb(motion["color"])
                motion["kind"] = "steady"
        elif kind == "blink":
            if now >= motion["stop"]:
                self._rgb(motion["color"])
                motion["kind"] = "steady"
                return
            if now >= motion["next"]:
                motion["on"] = not motion["on"]
                motion["next"] = now + BLINK_MS
                self._rgb(motion["color"] if motion["on"] else 0x000000)

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

    def _set_status(self, online):
        self._set_text(self._status, "CLOUD" if online else "OFFLINE")
        self._set_color(self._status, META if online else OFFLINE_DOT)
        try:
            if self._dot is not None:
                self._dot.set_style_bg_color(lv.color_hex(ONLINE_DOT if online else OFFLINE_DOT), 0)
        except Exception:
            pass

    def _draw_idle(self):
        self._motion = None
        self._rgb(0x000000)
        if self._mode == "lvgl":
            self._set_text(self._msg, "WAIT")
            self._set_color(self._msg, META)
            self._set_text(self._tier, "")
            self._set_text(self._preview, "")
            self._set_text(self._time, "--:--:--")
            self._set_status(self._cloud)
            return
        if self._mode == "canvas":
            self._canvas_clear()
            self._canvas_text("WAIT", 20, 120, 24, META)
            self._canvas_text("CLOUD" if self._cloud else "OFFLINE", 10, 30, 16, META)
            self._canvas_flush()

    def _draw_cloud_down(self):
        if self._mode == "lvgl":
            self._set_status(False)
            self._set_text(self._msg, "NO LINK")
            self._set_color(self._msg, META)
            self._set_text(self._tier, "")
            self._set_text(self._preview, "")
            return
        if self._mode == "canvas":
            self._canvas_clear()
            self._canvas_text("NO LINK", 10, 120, 24, META)
            self._canvas_text("OFFLINE", 10, 30, 16, OFFLINE_DOT)
            self._canvas_flush()

    # ---------- canvas 降级 ----------

    def _show_canvas(self, text, tier, color, event, preview):
        self._canvas_clear()
        self._canvas_text(text, 10, 110, 24, color)
        self._canvas_text(tier, 10, 150, 16, color)
        self._canvas_text(_hms(event.get("ts"), self._tz), 10, 180, 16, META)
        self._canvas_text("CLOUD", 10, 30, 16, META)
        if preview:
            self._canvas_text("TEST", 10, 210, 16, META_DIM)
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
                screen.show_bg(color=BG)
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
