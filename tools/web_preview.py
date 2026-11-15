#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
环境事件识别台 · 局域网预览服务（模拟数据）

用途：在**不接麦克风、不改识别代码**的前提下，用真实浏览器、真实 SSE 推送
     预览 `web/static/` 的界面。手机与电脑连同一 Wi-Fi 即可同时打开。

用法：
    python tools/web_preview.py
    python tools/web_preview.py --port 8766 --no-lan
    python tools/web_preview.py --password 1234

只依赖 Python 标准库；不需要 numpy / sounddevice / paho-mqtt。
"""

from __future__ import annotations

import argparse
import csv
import hmac
import io
import json
import math
import os
import queue
import random
import re
import secrets
import socket
import struct
import sys
import threading
import time
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "web", "static")

CLASSES = {
    "doorbell": {"label": "门铃", "tier": "L1"},
    "knock": {"label": "敲门", "tier": "L1"},
    "kettle": {"label": "水壶鸣笛", "tier": "L2"},
    "fire": {"label": "火警警报", "tier": "L3"},
}
CLASS_KEYS = list(CLASSES.keys())

HIT_MS = 3500
LEARN_WINDOW_S = 15
LEARN_MAX_ATTEMPTS = 8
COOLDOWN_MS = 8000
EVENT_LIMIT = 300
LOG_LIMIT = 300
SSE_REPLAY_EVENTS = 40

RECORDING_RE = re.compile(r"^/api/recordings/(\d+)\.wav$")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def now_ms() -> float:
    return time.time() * 1000.0


def clock(ts_ms: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts_ms / 1000.0))


def full_time(ts_ms: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_ms / 1000.0))


def round1(value: float) -> float:
    return round(float(value), 1)


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# 状态中枢
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.subscribers: set[queue.Queue] = set()
        self.tokens: set[str] = set()
        self.password = ""

        self.seq = 0
        self.events: list[dict] = []
        self.logs: list[dict] = []
        self.recordings: dict[int, dict] = {}

        self.mode = "idle"          # idle | listening | learning | hit
        self.stopped = False
        self.heard = False
        self.floor_db = -61.5
        self.level_db = None
        self.env = "quiet"

        self.last_hit = None
        self.hit = None
        self.before_hit = "listening"

        self.learn_state = None
        self.playing = None
        self.notice = None

        self.cooldown_until = 0.0
        self.spike_until = 0.0
        self.pending_hit_at = 0.0
        self.next_spike_at = 0.0

        self.templates = {
            "count": 4,
            "names": ["doorbell.wav", "knock.wav", "kettle.wav", "fire.wav"],
            "skipped": 0,
        }

        self.config = {
            "threshold": 0.60,
            "gate_db": 8,
            "mic_index": 0,
            "speaker_index": 1,
            "takes": 3,
            "lan": True,
            "password_set": False,
        }

        self.devices = {
            "inputs": [
                {"index": 0, "name": "系统默认（麦克风阵列）"},
                {"index": 1, "name": "麦克风阵列 (Realtek Audio)"},
            ],
            "outputs": [
                {"index": 0, "name": "系统默认"},
                {"index": 1, "name": "扬声器 (Realtek Audio)"},
            ],
        }

        self.iot = {
            "state": "unset",       # unset | connecting | online | reconnecting | failed
            "last_ok": None,
            "last_error": "",
            "sent": 0,
            "dropped": 0,
            "config": {
                "address": "iot.dfrobot.com.cn",
                "port": 1883,
                "iot_id": "",
                "topic_event": "",
                "topic_state": "",
                "enabled": False,
                "qos": 1,
                "report_ignore": False,
                "password_set": False,
            },
        }

        self.board = {"online": False}
        self.net = {
            "local": "http://127.0.0.1:8765",
            "lan": "http://127.0.0.1:8765",
            "port": 8765,
        }

        self._seed()

    # -- 种子 ---------------------------------------------------------------

    def _seed(self) -> None:
        seeds = [
            ("doorbell", False, 45 * 1000, False),
            ("knock", False, 4 * 60 * 1000, False),
            ("doorbell", False, 7 * 60 * 1000, True),
            ("kettle", False, 13 * 60 * 1000, False),
            ("fire", True, 21 * 60 * 1000, False),
            ("doorbell", False, 29 * 60 * 1000, False),
            ("knock", False, 38 * 60 * 1000, True),
            ("kettle", False, 47 * 60 * 1000, False),
            ("doorbell", True, 55 * 60 * 1000, False),
            ("doorbell", False, 66 * 60 * 1000, False),
            ("knock", False, 78 * 60 * 1000, False),
            ("doorbell", False, 91 * 60 * 1000, True),
        ]
        for cls, demo, ago, ignore in seeds:
            event = self._make_event(cls, demo=demo, ago_ms=ago, ignore=ignore)
            self._attach_recording(event)
            self._store_event(event)

        self._append_log("服务已启动，加载 4 个内置模板", ts=now_ms() - 95 * 60 * 1000)

    # -- 事件 ---------------------------------------------------------------

    def _make_event(self, cls: str, demo: bool = False, ago_ms: float = 0.0,
                    ignore: bool = False) -> dict:
        info = CLASSES.get(cls, CLASSES["doorbell"])
        ts = now_ms() - ago_ms
        score = random.uniform(0.36, 0.54) if ignore else random.uniform(0.68, 0.94)
        self.seq += 1

        return {
            "seq": self.seq,
            "ts": int(ts),
            "time": clock(ts),
            "type": "ignore" if ignore else "hit",
            "cls": cls,
            "label": info["label"],
            "tier": info["tier"],
            "score": round(score, 2),
            "conf": round(min(0.99, max(0.2, score + random.uniform(-0.08, 0.08))), 2),
            "latency": random.randint(320, 760),
            "dur": round(random.uniform(0.45, 1.6), 2),
            "demo": bool(demo),
            "tonal": round(random.uniform(0.20, 0.85), 2),
            "mod": round(random.uniform(0.10, 0.80), 2),
            "decay": round(random.uniform(0.10, 0.70), 2),
            "stab": round(random.uniform(0.20, 0.90), 2),
            "freq_ratio": round(random.uniform(0.85, 1.25), 2),
            "n_knock": random.randint(2, 4) if cls == "knock" else 0,
            "n_pulse": random.randint(3, 9) if cls in ("fire", "kettle") else 0,
            "peak_db": round1(random.uniform(-30, -12)),
            "snr": round1(random.uniform(16, 34)),
            "trace": {
                "score": round(min(0.99, score + 0.03), 2),
                "at": round(random.uniform(0.2, 0.8), 2),
            },
            "tpl_scores": (
                [[f"{cls}.wav", round(score, 2)]] +
                [[f"{key}.wav", round(random.uniform(0.05, 0.45), 2)]
                 for key in CLASS_KEYS if key != cls][:2]
            ),
            "reason": (f"得分 {score:.2f} 低于阈值 {self.config['threshold']:.2f}"
                       if ignore else None),
            "wav": None,
        }

    def _store_event(self, event: dict) -> None:
        self.events.insert(0, event)
        if len(self.events) > EVENT_LIMIT:
            self.events.pop()
        if event.get("wav"):
            self.recordings[event["seq"]] = event

    def _attach_recording(self, event: dict) -> None:
        event["wav"] = f"/api/recordings/{event['seq']}.wav"
        self.recordings[event["seq"]] = event

    def _push_event(self, event: dict) -> None:
        self._attach_recording(event)
        self._store_event(event)
        self.broadcast("event", event)

    # -- 日志 / 广播 --------------------------------------------------------

    def _append_log(self, text: str, ts: float | None = None) -> dict:
        ts = ts if ts is not None else now_ms()
        entry = {"ts": int(ts), "time": clock(ts), "text": text}
        self.logs.append(entry)
        if len(self.logs) > LOG_LIMIT:
            self.logs.pop(0)
        return entry

    def log(self, text: str) -> None:
        with self.lock:
            entry = self._append_log(text)
        self.broadcast("log", entry)

    def broadcast(self, name: str, data) -> None:
        payload = (
            f"event: {name}\n"
            f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
        ).encode("utf-8")
        with self.lock:
            subscribers = list(self.subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(payload)
            except queue.Full:
                pass

    # -- 快照 ---------------------------------------------------------------

    def stats_locked(self) -> dict:
        hit = ignore = demo = 0
        latency_total = 0
        latency_count = 0
        max_score = None
        by_class = {key: 0 for key in CLASS_KEYS}

        for event in self.events:
            if event["type"] == "hit":
                hit += 1
                latency_total += event["latency"]
                latency_count += 1
                score = event["score"]
                max_score = score if max_score is None else max(max_score, score)
                by_class[event["cls"]] = by_class.get(event["cls"], 0) + 1
            else:
                ignore += 1
            if event.get("demo"):
                demo += 1

        return {
            "hit": hit,
            "ignore": ignore,
            "demo": demo,
            "avg_latency": (latency_total / latency_count) if latency_count else None,
            "max_score": max_score,
            "by_class": by_class,
        }

    def learning_snapshot(self, ts: float) -> dict | None:
        if not self.learn_state:
            return None
        remaining = max(0.0, (self.learn_state["deadline"] - ts) / 1000.0)
        return {
            "take": self.learn_state["take"],
            "total": self.learn_state["total"],
            "remaining": round(remaining, 1),
            "window": LEARN_WINDOW_S,
            "attempts": self.learn_state["attempts"],
        }

    def snapshot_locked(self, ts: float) -> dict:
        return {
            "app": "doorbell-web",
            "mock": True,
            "seq": self.seq,
            "now": int(ts),
            "mode": self.mode,
            "on_air": self.mode == "listening" or (self.mode == "hit" and self.before_hit == "listening"),
            "stopped": self.stopped,
            "heard": self.heard,
            "level_db": None if self.level_db is None else round1(self.level_db),
            "floor_db": None if self.mode == "idle" else round1(self.floor_db),
            "env": self.env,
            "templates": {
                "count": self.templates["count"],
                "names": list(self.templates["names"]),
                "skipped": self.templates["skipped"],
            },
            "last_hit": self.last_hit,
            "hit": self.hit,
            "learn": self.learning_snapshot(ts),
            "playing": self.playing,
            "notice": self.notice,
            "iot": {
                "state": self.iot["state"],
                "last_ok": self.iot["last_ok"],
                "last_error": self.iot["last_error"],
                "sent": self.iot["sent"],
                "dropped": self.iot["dropped"],
                "config": dict(self.iot["config"]),
            },
            "board": dict(self.board),
            "stats": self.stats_locked(),
            "config": dict(self.config),
            "devices": self.devices,
            "net": dict(self.net),
            "events_total": len(self.events),
            "rec_count": len(self.recordings),
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self.snapshot_locked(now_ms())

    def full_state(self) -> dict:
        with self.lock:
            data = self.snapshot_locked(now_ms())
            data["events"] = list(self.events[:SSE_REPLAY_EVENTS])
            data["logs"] = list(self.logs[-LOG_LIMIT:])
            return data

    # -- 模拟 ---------------------------------------------------------------

    def fire_hit(self, cls: str, demo: bool = False) -> None:
        with self.lock:
            if self.mode == "learning":
                return
            event = self._make_event(cls, demo=demo)
            if self.mode in ("listening", "idle"):
                self.before_hit = self.mode
            self.mode = "hit"
            self.hit = dict(event)
            self.hit["until"] = int(now_ms() + HIT_MS)
            self.last_hit = event
            self.heard = False
            self.playing = None
            self.pending_hit_at = 0.0
            self.cooldown_until = now_ms() + COOLDOWN_MS
            self._push_event(event)

            prefix = "演示命中" if demo else "命中"
            self.log(f"{prefix}：{event['label']}，得分 {event['score']:.2f}")

    def start_learning(self, takes) -> None:
        total = max(1, min(5, int(takes or 3)))
        ts = now_ms()
        self.mode = "learning"
        self.stopped = False
        self.learn_state = {
            "take": 1,
            "total": total,
            "attempts": 0,
            "deadline": ts + LEARN_WINDOW_S * 1000,
            "step_at": ts + random.uniform(2600, 5200),
        }
        self.level_db = self.floor_db + 2
        self.log(f"开始学习，共 {total} 次")

    def finish_learning(self, ok: bool) -> None:
        if not self.learn_state:
            return
        self.learn_state = None
        self.mode = "idle"
        self.playing = None
        self.level_db = None
        self.heard = False
        ts = now_ms()

        if ok:
            self.config["threshold"] = round(random.uniform(0.52, 0.62), 2)
            self.notice = {
                "kind": "learn_done",
                "title": "学习完成 ✓",
                "detail": f"新阈值 {self.config['threshold']:.2f}，已可用于监听。",
                "ts": int(ts),
                "until": int(ts + 6000),
            }
            self.log(f"学习完成，新阈值 {self.config['threshold']:.2f}")
        else:
            self.notice = {
                "kind": "learn_failed",
                "title": "没有学到",
                "detail": "没有录到有效门铃。原有模板和阈值保持不变。",
                "ts": int(ts),
                "until": int(ts + 6000),
            }
            self.log("学习失败：没有录到有效门铃")

    def _tick_learning(self, ts: float) -> None:
        learn = self.learn_state
        if not learn:
            return

        if ts >= learn["deadline"]:
            learn["attempts"] += 1
            if learn["attempts"] >= LEARN_MAX_ATTEMPTS:
                self.finish_learning(False)
                return
            learn["deadline"] = ts + LEARN_WINDOW_S * 1000
            learn["step_at"] = ts + random.uniform(2200, 4800)
            self.log(f"等待门铃超时，继续第 {learn['take']} 次")
            return

        if ts >= learn["step_at"]:
            learn["take"] += 1
            if learn["take"] > learn["total"]:
                self.finish_learning(True)
                return
            learn["deadline"] = ts + LEARN_WINDOW_S * 1000
            learn["step_at"] = ts + random.uniform(2400, 4800)
            self.log(f"已录到第 {learn['take'] - 1} 次")

    def _tick_audio(self, ts: float) -> None:
        if self.mode == "learning":
            self.level_db = self.floor_db + random.uniform(1, 5)
            self.env = "quiet"
            return

        if self.mode not in ("listening", "hit"):
            self.level_db = None
            self.heard = False
            return

        self.floor_db += random.uniform(-0.25, 0.25)
        self.floor_db = max(-68.0, min(-56.0, self.floor_db))
        gate = self.floor_db + self.config["gate_db"]

        if self.next_spike_at <= 0:
            self.next_spike_at = ts + random.uniform(4000, 8000)

        if ts >= self.next_spike_at and self.mode == "listening" and not self.playing:
            self.spike_until = ts + random.uniform(500, 900)
            self.next_spike_at = ts + random.uniform(9000, 15000)
            if ts > self.cooldown_until:
                self.pending_hit_at = ts + 420

        if ts < self.spike_until:
            self.level_db = max(-30.0, min(-14.0, gate + random.uniform(2, 10)))
            self.heard = True
        else:
            self.level_db = self.floor_db + random.uniform(1.5, 5.0)
            self.heard = self.mode == "listening" and self.level_db > gate

        self.env = "clip" if self.level_db > -12 else "loud" if self.level_db > -30 else "quiet"

        if self.pending_hit_at and ts >= self.pending_hit_at:
            roll = random.random()
            cls = "doorbell" if roll < 0.55 else "knock" if roll < 0.78 else "kettle" if roll < 0.92 else "fire"
            self.fire_hit(cls, demo=False)

    def tick(self) -> None:
        ts = now_ms()
        with self.lock:
            if self.hit and ts >= self.hit.get("until", 0):
                self.hit = None
                self.mode = self.before_hit if self.before_hit in ("listening", "idle") else "idle"

            if self.playing and ts >= self.playing.get("until", 0):
                self.playing = None
            if self.notice and self.notice.get("until") and ts >= self.notice["until"]:
                self.notice = None

            self._tick_learning(ts)
            self._tick_audio(ts)

            snapshot = self.snapshot_locked(ts)

        self.broadcast("state", snapshot)

    # -- 命令 ---------------------------------------------------------------

    def listen(self, on: bool) -> dict:
        with self.lock:
            if on:
                if self.mode == "learning":
                    raise ApiError("正在学习，请先取消学习", 409)
                if not self.templates["count"]:
                    raise ApiError("没有可用模板，请先学习门铃", 409)
                self.mode = "listening"
                self.stopped = False
                self.level_db = self.floor_db + 3
                self.next_spike_at = now_ms() + random.uniform(5000, 9000)
                self.cooldown_until = 0.0
                self.log("开始监听")
            else:
                self.mode = "idle"
                self.stopped = True
                self.hit = None
                self.level_db = None
                self.heard = False
                self.pending_hit_at = 0.0
                self.log("停止监听")
        return {"ok": True}

    def learn(self, action: str, takes) -> dict:
        with self.lock:
            if action == "cancel":
                if self.learn_state:
                    self.log("取消学习")
                self.learn_state = None
                if self.mode == "learning":
                    self.mode = "idle"
                self.level_db = None
                self.heard = False
                return {"ok": True}

            if self.mode == "learning" and self.learn_state:
                raise ApiError(
                    f"另一台设备正在学习（{self.learn_state['take']}/{self.learn_state['total']}）", 409
                )
            if self.mode == "listening" or self.mode == "hit":
                raise ApiError("请先停止监听再学习", 409)

            self.start_learning(takes)
            return {"ok": True, "takes": self.learn_state["total"] if self.learn_state else 3}

    def preview(self, cls=None, test=False, file=None) -> dict:
        with self.lock:
            if self.mode == "learning":
                raise ApiError("学习进行中，暂时不能试听", 409)

            if file:
                lowered = str(file).lower()
                chosen = "doorbell"
                for key in CLASS_KEYS:
                    if key in lowered:
                        chosen = key
                        break
                self.playing = {
                    "cls": chosen,
                    "label": CLASSES[chosen]["label"],
                    "test": False,
                    "until": int(now_ms() + 1600),
                }
                threading.Timer(0.35, self.fire_hit, args=(chosen, True)).start()
                return {"ok": True}

            if test:
                self.playing = {
                    "cls": "test",
                    "label": "测试音",
                    "test": True,
                    "until": int(now_ms() + 1000),
                }
                return {"ok": True}

            chosen = cls if cls in CLASSES else "doorbell"
            self.playing = {
                "cls": chosen,
                "label": CLASSES[chosen]["label"],
                "test": False,
                "until": int(now_ms() + 1600),
            }
            threading.Timer(0.35, self.fire_hit, args=(chosen, True)).start()
            return {"ok": True}

    def update_settings(self, body: dict) -> dict:
        with self.lock:
            if body.get("clear_password"):
                self.password = ""
                self.config["password_set"] = False
                self.tokens.clear()
                self.log("已清除访问口令")
                return {"ok": True, "config": dict(self.config)}

            if "threshold" in body:
                try:
                    threshold = float(body["threshold"])
                except (TypeError, ValueError):
                    raise ApiError("阈值必须是数字")
                if not (0.40 <= threshold <= 0.95):
                    raise ApiError("阈值需在 0.40 到 0.95 之间")
                self.config["threshold"] = round(threshold, 2)

            if "gate_db" in body:
                try:
                    gate = float(body["gate_db"])
                except (TypeError, ValueError):
                    raise ApiError("门控必须是数字")
                if not (6 <= gate <= 30):
                    raise ApiError("门控需在 6 到 30 dB 之间")
                self.config["gate_db"] = int(round(gate))

            if "takes" in body:
                try:
                    takes = int(body["takes"])
                except (TypeError, ValueError):
                    raise ApiError("学习次数必须是整数")
                if not (1 <= takes <= 5):
                    raise ApiError("学习次数需在 1 到 5 之间")
                self.config["takes"] = takes

            if "mic_index" in body:
                self.config["mic_index"] = int(body["mic_index"])
            if "speaker_index" in body:
                self.config["speaker_index"] = int(body["speaker_index"])
            if "lan" in body:
                self.config["lan"] = bool(body["lan"])

            password = body.get("password")
            if isinstance(password, str) and password:
                self.password = password
                self.config["password_set"] = True

            self.log("设置已保存")
            return {"ok": True, "config": dict(self.config)}

    def update_iot(self, body: dict) -> dict:
        with self.lock:
            config = self.iot["config"]
            if "address" in body:
                config["address"] = str(body["address"]).strip()
            if "port" in body:
                try:
                    port = int(body["port"])
                except (TypeError, ValueError):
                    raise ApiError("端口必须是整数")
                if not (1 <= port <= 65535):
                    raise ApiError("端口需在 1 到 65535 之间")
                config["port"] = port
            if "iot_id" in body:
                config["iot_id"] = str(body["iot_id"]).strip()
            if "topic_event" in body:
                config["topic_event"] = str(body["topic_event"]).strip()
            if "topic_state" in body:
                config["topic_state"] = str(body["topic_state"]).strip()
            if "enabled" in body:
                config["enabled"] = bool(body["enabled"])
            if "qos" in body:
                config["qos"] = 0 if int(body["qos"]) == 0 else 1
            if "report_ignore" in body:
                config["report_ignore"] = bool(body["report_ignore"])

            password = body.get("password")
            if isinstance(password, str) and password:
                config["password_set"] = True

            if config["enabled"] and (not config["address"] or not config["iot_id"]):
                config["enabled"] = False
                raise ApiError("启用上报前请填写地址与 Iot_id")

            if config["enabled"] and config["address"] and config["iot_id"]:
                self.iot["state"] = "connecting"
                self.iot["last_error"] = ""
                threading.Timer(1.0, self._iot_online).start()
            elif not config["enabled"]:
                self.iot["state"] = "unset"
                self.iot["last_error"] = ""

            self.log("上报设置已保存")
            return {"ok": True}

    def _iot_online(self) -> None:
        with self.lock:
            self.iot["state"] = "online"
            self.iot["last_ok"] = int(now_ms())
            self.iot["last_error"] = ""
            self.board["online"] = True
        self.log("上报平台已连接")

    def test_iot(self) -> dict:
        with self.lock:
            if not self.iot["config"]["iot_id"]:
                raise ApiError("先填写 Iot_id 再测试连接")
            self.iot["state"] = "connecting"
            self.iot["last_error"] = ""
        threading.Timer(0.9, self._iot_online).start()
        self.log("测试上报连接")
        return {"ok": True}

    def clear_recordings(self) -> dict:
        with self.lock:
            self.recordings.clear()
            for event in self.events:
                event["wav"] = None
            self.log("已清空事件录音")
        return {"ok": True}

    def authenticate(self, password: str) -> str:
        if not self.password:
            return ""
        if not hmac.compare_digest(str(password or ""), self.password):
            raise ApiError("口令不正确", 401)
        token = secrets.token_urlsafe(18)
        with self.lock:
            self.tokens.add(token)
        return token


# ---------------------------------------------------------------------------
# WAV 合成（仅预览用）
# ---------------------------------------------------------------------------

def recording_wav(cls: str) -> bytes:
    sample_rate = 22050
    duration = 1.0
    frames = bytearray()
    count = int(sample_rate * duration)

    for i in range(count):
        t = i / sample_rate
        fade = min(1.0, t / 0.02, (duration - t) / 0.05)
        value = 0.0

        if cls == "doorbell":
            env = math.exp(-2.6 * t)
            freq = 660.0 if (t % 0.9) < 0.45 else 880.0
            value = math.sin(2 * math.pi * freq * t) * env + 0.3 * math.sin(2 * math.pi * freq * 2 * t) * env
        elif cls == "knock":
            for offset in (0.10, 0.42, 0.70):
                if t >= offset:
                    dt = t - offset
                    value += math.exp(-38 * dt) * math.sin(2 * math.pi * 120 * dt) * 0.9
                    value += math.exp(-55 * dt) * random.uniform(-0.3, 0.3)
        elif cls == "kettle":
            env = min(1.0, t * 8) * max(0.0, 1.0 - t * 0.6)
            value = math.sin(2 * math.pi * 1800 * t) * (0.6 + 0.4 * math.sin(2 * math.pi * 6 * t)) * env
        else:  # fire
            pulse = 0.5 + 0.5 * math.sin(2 * math.pi * 3 * t)
            square = 1.0 if math.sin(2 * math.pi * 900 * t) >= 0 else -1.0
            value = square * pulse * 0.5

        sample = int(max(-1.0, min(1.0, value * 0.55 * fade)) * 32767)
        frames += struct.pack("<h", sample)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(frames))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # Windows 上 SO_REUSEADDR 会允许 0.0.0.0 与 127.0.0.1 抢同一端口

    hub: Hub


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "doorbell-preview/0.1"

    # -- 基础 ---------------------------------------------------------------

    @property
    def hub(self) -> Hub:
        return self.server.hub

    def log_message(self, fmt, *args):
        return

    def send_json(self, status: int, payload: dict, extra_headers=None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_bytes(self, status: int, body: bytes, content_type: str, extra_headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise ApiError("请求体过大", 413)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("请求内容不是有效的 JSON")

    def authorized(self) -> bool:
        if not self.hub.password:
            return True
        cookie = self.headers.get("Cookie") or ""
        for piece in cookie.split(";"):
            key, _, value = piece.strip().partition("=")
            if key == "db_auth" and value in self.hub.tokens:
                return True
        return False

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json(401, {"error": "需要访问口令"})
        return False

    # -- GET ----------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path == "/events":
                if not self.require_auth():
                    return
                self.handle_sse()
                return

            if path == "/api/health":
                self.send_json(200, {
                    "app": "doorbell-web",
                    "mock": True,
                    "pid": os.getpid(),
                    "port": self.server.server_address[1],
                })
                return

            if path == "/api/state":
                if not self.require_auth():
                    return
                self.send_json(200, self.hub.full_state())
                return

            if path == "/api/events":
                if not self.require_auth():
                    return
                query = parse_qs(parsed.query)
                before = int(query.get("before", ["0"])[0] or 0)
                limit = int(query.get("limit", ["40"])[0] or 40)
                limit = max(1, min(300, limit))
                with self.hub.lock:
                    pool = [event for event in self.hub.events if event["seq"] < before] if before else self.hub.events
                    events = list(pool[:limit])
                    total = len(self.hub.events)
                self.send_json(200, {"events": events, "total": total})
                return

            if path == "/api/export.csv":
                if not self.require_auth():
                    return
                self.send_export()
                return

            match = RECORDING_RE.match(path)
            if match:
                if not self.require_auth():
                    return
                seq = int(match.group(1))
                with self.hub.lock:
                    event = self.hub.recordings.get(seq)
                if not event:
                    self.send_json(404, {"error": "录音不存在或已清空"})
                    return
                body = recording_wav(event["cls"])
                filename = f"{event['time'].replace(':', '')}_{event['type']}_{event['cls']}.wav"
                self.send_bytes(
                    200, body, "audio/wav",
                    [("Content-Disposition", f'inline; filename="{filename}"')],
                )
                return

            if path.startswith("/api/"):
                self.send_json(404, {"error": "接口不存在"})
                return

            self.serve_static(path)

        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 - 任何异常只回 500，不冒到识别侧
            self.send_json(500, {"error": "服务内部错误，请查看控制台"})
            print(f"[web_preview] GET {path} 出错：{exc!r}", file=sys.stderr)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/api/auth":
                body = self.read_json()
                try:
                    token = self.hub.authenticate(str(body.get("password", "")))
                except ApiError as err:
                    self.send_json(err.status, {"error": err.message})
                    return
                headers = []
                if token:
                    headers.append((
                        "Set-Cookie",
                        f"db_auth={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400",
                    ))
                self.send_json(200, {"ok": True}, headers)
                return

            if not self.require_auth():
                return

            body = self.read_json()

            if path == "/api/listen":
                self.send_json(200, self.hub.listen(bool(body.get("on"))))
                return
            if path == "/api/learn":
                self.send_json(200, self.hub.learn(str(body.get("action", "start")), body.get("takes")))
                return
            if path == "/api/preview":
                self.send_json(200, self.hub.preview(
                    cls=body.get("cls"),
                    test=bool(body.get("test")),
                    file=body.get("file"),
                ))
                return
            if path == "/api/settings":
                self.send_json(200, self.hub.update_settings(body))
                return
            if path == "/api/templates/refresh":
                with self.hub.lock:
                    self.hub.templates["names"] = ["doorbell.wav", "knock.wav", "kettle.wav", "fire.wav"]
                    self.hub.templates["count"] = 4
                self.hub.log("模板已刷新，共 4 个")
                self.send_json(200, {"ok": True})
                return
            if path == "/api/iot/config":
                self.send_json(200, self.hub.update_iot(body))
                return
            if path == "/api/iot/test":
                self.send_json(200, self.hub.test_iot())
                return
            if path == "/api/recordings/clear":
                self.send_json(200, self.hub.clear_recordings())
                return

            self.send_json(404, {"error": "接口不存在"})

        except ApiError as err:
            self.send_json(err.status, {"error": err.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            self.send_json(500, {"error": "服务内部错误，请查看控制台"})
            print(f"[web_preview] POST {path} 出错：{exc!r}", file=sys.stderr)

    # -- SSE ----------------------------------------------------------------

    def handle_sse(self) -> None:
        sub: queue.Queue = queue.Queue(maxsize=64)
        with self.hub.lock:
            self.hub.subscribers.add(sub)

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            self.wfile.write(b"retry: 3000\n\n")
            initial = self.hub.snapshot()
            self.wfile.write(
                ("event: state\ndata: " + json.dumps(initial, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")
            )
            self.wfile.flush()

            last_sent = time.time()
            while True:
                try:
                    payload = sub.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_sent >= 1.0:
                        heartbeat = json.dumps({"ts": int(now_ms())})
                        self.wfile.write(f"event: heartbeat\ndata: {heartbeat}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_sent = time.time()
                    continue

                self.wfile.write(payload)
                self.wfile.flush()
                last_sent = time.time()

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.hub.lock:
                self.hub.subscribers.discard(sub)
            self.close_connection = True

    # -- 静态文件 -----------------------------------------------------------

    def serve_static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        if relative == "":
            relative = "index.html"

        base = os.path.realpath(STATIC)
        full = os.path.realpath(os.path.join(base, relative))

        if full != base and not full.startswith(base + os.sep):
            self.send_json(403, {"error": "禁止访问"})
            return

        if os.path.isdir(full):
            full = os.path.join(full, "index.html")

        if not os.path.isfile(full):
            self.send_json(404, {"error": "文件不存在"})
            return

        content_type = "application/octet-stream"
        if full.endswith(".html"):
            content_type = "text/html; charset=utf-8"
        elif full.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif full.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        elif full.endswith(".svg"):
            content_type = "image/svg+xml"
        elif full.endswith(".wav"):
            content_type = "audio/wav"
        elif full.endswith(".png"):
            content_type = "image/png"

        with open(full, "rb") as handle:
            body = handle.read()

        self.send_bytes(200, body, content_type)

    # -- CSV ----------------------------------------------------------------

    def send_export(self) -> None:
        header = ["时间", "类型", "类别", "tier", "是否演示", "得分", "置信度",
                  "延迟ms", "时长s", "调性", "调制", "衰减", "稳定度", "主频比"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(header)

        with self.hub.lock:
            events = list(reversed(self.hub.events))

        for event in events:
            writer.writerow([
                full_time(event["ts"]),
                "命中" if event["type"] == "hit" else "忽略",
                event["label"],
                event["tier"],
                "是" if event.get("demo") else "否",
                f"{event['score']:.2f}",
                f"{event['conf']:.2f}",
                event["latency"],
                f"{event['dur']:.2f}",
                f"{event['tonal']:.2f}",
                f"{event['mod']:.2f}",
                f"{event['decay']:.2f}",
                f"{event['stab']:.2f}",
                f"{event['freq_ratio']:.2f}",
            ])

        body = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        filename = "session_" + time.strftime("%Y%m%d_%H%M%S") + ".csv"
        self.send_bytes(
            200, body, "text/csv; charset=utf-8",
            [("Content-Disposition", f'attachment; filename="{filename}"')],
        )


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def simulator_loop(hub: Hub, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            hub.tick()
        except Exception as exc:  # noqa: BLE001
            print(f"[web_preview] 模拟循环异常：{exc!r}", file=sys.stderr)
            try:
                hub.log("模拟循环出现异常，已跳过一帧")
            except Exception:  # noqa: BLE001
                pass
        stop.wait(0.125)


def bind_server(host: str, port: int) -> Server:
    last_error = None
    for candidate in (port, port + 1, port + 2):
        try:
            server = Server((host, candidate), Handler)
            server.hub = Hub()
            return server
        except OSError as exc:
            last_error = exc
    raise SystemExit(
        f"端口 {port} 到 {port + 2} 都被占用，无法启动预览服务。\n"
        f"请关掉占用端口的程序，或用 --port 换一个端口。\n"
        f"（{last_error}）"
    )


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description="环境事件识别台 · 局域网预览服务（模拟数据）")
    parser.add_argument("--port", type=int, default=8765, help="起始端口，占用时自动 +1 / +2")
    parser.add_argument("--no-lan", action="store_true", help="只监听 127.0.0.1，不开放给局域网")
    parser.add_argument("--password", default="", help="可选的访问口令（只存在内存里，不写日志）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开默认浏览器")
    args = parser.parse_args()

    host = "127.0.0.1" if args.no_lan else "0.0.0.0"
    server = bind_server(host, args.port)
    hub = server.hub
    port = server.server_address[1]
    ip = lan_ip()

    hub.password = args.password
    hub.config["password_set"] = bool(args.password)
    hub.net = {
        "local": f"http://127.0.0.1:{port}",
        "lan": f"http://{ip}:{port}" if host != "127.0.0.1" else f"http://127.0.0.1:{port}",
        "port": port,
    }
    if args.password:
        hub.log("已开启访问口令")

    stop = threading.Event()
    thread = threading.Thread(target=simulator_loop, args=(hub, stop), daemon=True)
    thread.start()

    print("")
    print("  环境事件识别台 · 局域网预览（模拟数据，不接麦克风）")
    print("  " + "-" * 52)
    print(f"  本机    {hub.net['local']}")
    if host != "127.0.0.1":
        if ip.startswith("127."):
            print("  局域网  未检测到内网地址（可能是防火墙或没连 Wi-Fi）")
        else:
            print(f"  局域网  {hub.net['lan']}")
    else:
        print("  局域网  已用 --no-lan 关闭")
    if args.password:
        print("  口令    已开启（浏览器会要求输入）")
    print("  " + "-" * 52)
    print("  手机与电脑连同一 Wi-Fi，打开上面的局域网地址即可。")
    print("  首次启动如果 Windows 防火墙弹窗，请点「允许」。")
    print("  页面里的数据是模拟的，顶部会标「模拟数据」。Ctrl+C 停止。")
    print("")

    if args.open:
        webbrowser.open(hub.net["local"])

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
