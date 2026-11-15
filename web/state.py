"""唯一状态总线：快照、事件、日志、统计、SSE 订阅。

线程模型：
- 识别线程/学习线程/HTTP 线程都只通过本类写入；
- 一个 8 Hz 的 ticker（在 `web.app`）负责过期与广播快照；
- 事件与日志即时广播，快照不携带它们（8 Hz 下太重）。
"""

from __future__ import annotations

import copy
import csv
import glob
import io
import json
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import quote

MAX_EVENTS = 300
MAX_LOGS = 300
SUBSCRIBER_QUEUE = 128
HIT_DISPLAY_MS = 3500
ENV_WINDOW_SEC = 2.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class StateBus:
    def __init__(self, events_dir: str | os.PathLike):
        self.lock = threading.RLock()
        self.subscribers: set[queue.Queue] = set()
        self.events_dir = Path(events_dir)
        try:
            self.events_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self.events: list[dict] = []
        self.logs: list[dict] = []
        self.seq = 0
        self._last_event_ts = 0

        self.mode = "idle"          # idle | listening | learning | hit
        self.on_air = False
        self.stopped = False
        self.heard_until = 0.0
        self.level_db: float | None = None
        self.floor_db: float | None = None
        self.env = "quiet"
        self._levels: list[tuple[float, float]] = []

        self.templates = {"count": 0, "names": [], "skipped": 0}
        self.last_hit = None
        self.hit = None
        self.learn = None
        self.playing = None
        self.notice = None

        self.config = {}
        self.devices = {"inputs": [], "outputs": []}
        self.net = {"local": "", "lan": "", "port": 0, "lan_enabled": True}
        self.iot = {
            "state": "unset",
            "last_ok": None,
            "last_error": "",
            "sent": 0,
            "dropped": 0,
            "config": {},
        }
        self.board = {"online": False}
        self.rec_count = self._count_recordings()

    # ------------------------------------------------------------------
    # 录制文件
    # ------------------------------------------------------------------

    def _count_recordings(self) -> int:
        try:
            return len(glob.glob(str(self.events_dir / "*.wav")))
        except OSError:
            return 0

    def refresh_recordings(self) -> int:
        with self.lock:
            self.rec_count = self._count_recordings()
            return self.rec_count

    def recording_path(self, name: str) -> Path | None:
        safe = os.path.basename(str(name or ""))
        if safe != str(name) or not safe.lower().endswith(".wav"):
            return None
        path = self.events_dir / safe
        return path if path.is_file() else None

    def clear_recordings(self) -> int:
        removed = 0
        with self.lock:
            for path in glob.glob(str(self.events_dir / "*.wav")):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
            for event in self.events:
                event["wav_file"] = None
            self.rec_count = self._count_recordings()
        return removed

    # ------------------------------------------------------------------
    # 事件与日志
    # ------------------------------------------------------------------

    def public_event(self, event: dict) -> dict:
        out = {k: v for k, v in event.items() if k != "wav_file"}
        name = event.get("wav_file")
        out["wav"] = f"/api/recordings/{quote(name)}" if name else None
        return copy.deepcopy(out)

    def add_event(self, event: dict) -> dict:
        with self.lock:
            event = dict(event)
            ts = _now_ms()
            if ts <= self._last_event_ts:
                ts = self._last_event_ts + 1
            self._last_event_ts = ts
            self.seq += 1
            event["seq"] = self.seq
            event["ts"] = ts
            event["time"] = time.strftime("%H:%M:%S", time.localtime(ts / 1000.0))

            self.events.insert(0, event)
            if len(self.events) > MAX_EVENTS:
                self.events.pop()

            public = self.public_event(event)
            if event.get("type") == "hit":
                self.hit = dict(public)
                self.hit["until"] = ts + HIT_DISPLAY_MS
                self.last_hit = public
                self.mode = "hit"
                self.heard_until = 0.0
            elif self.on_air and self.mode == "idle":
                # 正常情况下忽略事件发生在 listening；这里只兜底，绝不打断 3.5 秒命中展示。
                self.mode = "listening"

            self.rec_count = self._count_recordings()

        self.publish("event", public)
        return public

    def add_log(self, text: str) -> dict:
        entry = {
            "ts": _now_ms(),
            "time": time.strftime("%H:%M:%S"),
            "text": str(text),
        }
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > MAX_LOGS:
                self.logs.pop(0)
        self.publish("log", entry)
        return entry

    def stats(self) -> dict:
        with self.lock:
            events = list(self.events)

        hit = ignore = demo = 0
        latency_total = 0
        latency_count = 0
        max_score = None
        by_class = {"doorbell": 0, "knock": 0, "kettle": 0, "fire": 0}

        for event in events:
            if event.get("type") == "hit":
                hit += 1
                latency_total += int(event.get("latency") or 0)
                latency_count += 1
                score = float(event.get("score") or 0.0)
                max_score = score if max_score is None else max(max_score, score)
                cls = event.get("cls") or ""
                if cls in by_class:
                    by_class[cls] += 1
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

    # ------------------------------------------------------------------
    # 状态写入
    # ------------------------------------------------------------------

    def set_listening(self, on: bool) -> None:
        with self.lock:
            self.on_air = bool(on)
            if on:
                self.mode = "listening"
                self.stopped = False
                self.hit = None
                self.notice = None
                self.heard_until = 0.0
            else:
                self.mode = "idle"
                self.stopped = True
                self.level_db = None
                self.floor_db = None
                self.heard_until = 0.0
                self.hit = None
                self._levels.clear()

    def set_learning(self, learn: dict | None) -> None:
        with self.lock:
            self.learn = dict(learn) if learn else None
            if learn:
                self.mode = "learning"
                self.on_air = False
                self.stopped = False
                self.hit = None
                self.heard_until = 0.0
                self.level_db = None
                self.floor_db = None
            elif self.mode == "learning":
                self.mode = "idle"
                self.stopped = False

    def update_learn(self, **fields) -> None:
        with self.lock:
            if self.learn is not None:
                self.learn.update(fields)

    def set_playing(self, playing: dict | None) -> None:
        with self.lock:
            self.playing = dict(playing) if playing else None

    def set_notice(self, notice: dict | None) -> None:
        with self.lock:
            self.notice = dict(notice) if notice else None

    def set_level(self, db: float | None, floor: float | None) -> None:
        now = time.time()
        with self.lock:
            self.level_db = None if db is None else float(db)
            self.floor_db = None if floor is None else float(floor)
            if db is None:
                self._levels.clear()
                return
            self._levels.append((now, float(db)))
            cutoff = now - ENV_WINDOW_SEC
            while self._levels and self._levels[0][0] < cutoff:
                self._levels.pop(0)
            values = sorted(value for _, value in self._levels)
            if not values:
                return
            median = values[len(values) // 2]
            self.env = "clip" if median >= -12 else "loud" if median >= -30 else "quiet"

    def note_heard(self, seconds: float = 0.6) -> None:
        with self.lock:
            self.heard_until = time.time() + max(0.1, seconds)

    def set_templates(self, templates: dict) -> None:
        with self.lock:
            self.templates = {
                "count": int(templates.get("count", 0)),
                "names": list(templates.get("names", [])),
                "skipped": int(templates.get("skipped", 0)),
            }

    def set_config(self, config: dict) -> None:
        with self.lock:
            self.config = copy.deepcopy(config)

    def set_devices(self, devices: dict) -> None:
        with self.lock:
            self.devices = copy.deepcopy(devices)

    def set_net(self, net: dict) -> None:
        with self.lock:
            self.net = copy.deepcopy(net)

    def set_iot(self, iot: dict) -> None:
        with self.lock:
            self.iot = copy.deepcopy(iot)

    def set_board(self, board: dict) -> None:
        with self.lock:
            self.board = copy.deepcopy(board)

    def expire(self) -> None:
        """过期命中展示、播放与短暂提示；不改变 stopped。"""
        now = _now_ms()
        with self.lock:
            if self.hit and now >= int(self.hit.get("until") or 0):
                self.hit = None
                self.mode = "listening" if self.on_air else "idle"
            if self.playing and now >= int(self.playing.get("until") or 0):
                self.playing = None
            if self.notice and self.notice.get("until") and now >= int(self.notice["until"]):
                self.notice = None

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        now_ms = int(now * 1000)
        with self.lock:
            learn = None
            if self.learn:
                learn = {k: v for k, v in self.learn.items() if k != "deadline"}
                deadline = self.learn.get("deadline")
                if deadline:
                    learn["remaining"] = max(0.0, round((deadline - now_ms) / 1000.0, 1))
                else:
                    learn["remaining"] = 0.0

            active = self.mode in ("listening", "hit", "learning")
            heard = self.mode == "listening" and now < self.heard_until

            return {
                "app": "doorbell-web",
                "mock": False,
                "seq": self.seq,
                "now": now_ms,
                "mode": self.mode,
                "on_air": self.on_air,
                "stopped": self.stopped,
                "heard": heard,
                "level_db": None if self.level_db is None or not active else round(self.level_db, 1),
                "floor_db": None if self.floor_db is None or not active else round(self.floor_db, 1),
                "env": self.env if active else "quiet",
                "templates": {
                    "count": self.templates["count"],
                    "names": list(self.templates["names"]),
                    "skipped": self.templates["skipped"],
                },
                "last_hit": copy.deepcopy(self.last_hit),
                "hit": copy.deepcopy(self.hit),
                "learn": learn,
                "playing": copy.deepcopy(self.playing),
                "notice": copy.deepcopy(self.notice),
                "iot": copy.deepcopy(self.iot),
                "board": copy.deepcopy(self.board),
                "stats": self.stats(),
                "config": copy.deepcopy(self.config),
                "devices": copy.deepcopy(self.devices),
                "net": copy.deepcopy(self.net),
                "events_total": len(self.events),
                "rec_count": self.rec_count,
            }

    def full_state(self) -> dict:
        with self.lock:
            events = [self.public_event(event) for event in self.events[:40]]
            logs = copy.deepcopy(self.logs[-MAX_LOGS:])
        state = self.snapshot()
        state["events"] = events
        state["logs"] = logs
        return state

    def list_events(self, before: int = 0, limit: int = 40) -> dict:
        limit = max(1, min(MAX_EVENTS, int(limit or 40)))
        with self.lock:
            pool = (
                [event for event in self.events if event["seq"] < before]
                if before
                else self.events
            )
            events = [self.public_event(event) for event in pool[:limit]]
            total = len(self.events)
        return {"events": events, "total": total}

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------

    def csv_bytes(self) -> bytes:
        header = ["时间", "类型", "类别", "tier", "是否演示", "得分", "置信度",
                  "延迟ms", "时长s", "调性", "调制", "衰减", "稳定度", "主频比"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(header)

        with self.lock:
            events = list(reversed(self.events))

        for event in events:
            stamp = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(int(event.get("ts", 0)) / 1000.0)
            )
            writer.writerow([
                stamp,
                "命中" if event.get("type") == "hit" else "忽略",
                event.get("label", ""),
                event.get("tier", ""),
                "是" if event.get("demo") else "否",
                f"{float(event.get('score') or 0):.2f}",
                f"{float(event.get('conf') or 0):.2f}",
                int(event.get("latency") or 0),
                f"{float(event.get('dur') or 0):.2f}",
                f"{float(event.get('tonal') or 0):.2f}",
                f"{float(event.get('mod') or 0):.2f}",
                f"{float(event.get('decay') or 0):.2f}",
                f"{float(event.get('stab') or 0):.2f}",
                f"{float(event.get('freq_ratio') or 1.0):.2f}",
            ])
        return ("\ufeff" + buffer.getvalue()).encode("utf-8")

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE)
        with self.lock:
            self.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self.lock:
            self.subscribers.discard(sub)

    def publish(self, name: str, data) -> None:
        payload = (
            f"event: {name}\n"
            f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
        )
        with self.lock:
            subscribers = list(self.subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(payload)
            except queue.Full:
                pass

    def broadcast_state(self) -> None:
        self.publish("state", self.snapshot())
