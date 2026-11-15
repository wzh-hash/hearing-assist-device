#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
演示数据面板（给老师 / 评委看的数据与可视化）

内容：
    - 会话统计：模板数、阈值、命中/忽略次数、平均延迟、最高得分
    - 事件列表：每一次判定（时间、类型、得分、延迟、时长）
    - 选中事件的波形、对数梅尔谱、各模板得分条、事件内得分轨迹
    - 导出 CSV

独立模块，主界面随时可以重写，这里只依赖 pc_recognizer。
"""
from __future__ import annotations

import csv
import os
import time
import tkinter as tk
from tkinter import ttk

import numpy as np

import pc_recognizer as R

BG = "#0f1620"
CARD = "#18222e"
TEXT = "#e6edf3"
SUB = "#8b98a5"
ACCENT = "#4aa3ff"
HIT = "#ff6b3d"
OK = "#39c07a"


def _cmap(v: np.ndarray) -> np.ndarray:
    """0~1 → RGB（黑青→蓝→黄→红）。"""
    v = np.clip(v, 0, 1)
    stops = np.array([
        [8, 12, 20],
        [20, 70, 120],
        [30, 160, 150],
        [220, 200, 60],
        [240, 90, 50],
    ], dtype=np.float32)
    pos = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
    idx = np.clip(np.searchsorted(pos, v) - 1, 0, len(stops) - 2)
    frac = (v - pos[idx]) / (pos[idx + 1] - pos[idx] + 1e-9)
    out = stops[idx] * (1 - frac[..., None]) + stops[idx + 1] * frac[..., None]
    return out.astype(np.uint8)


class DataPanel(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("演示数据面板")
        self.geometry("1020x760")
        self.configure(bg=BG)
        self._shown = 0
        self._current = None
        self._photo = None
        self._build()

    # ------------------------------------------------------------ UI
    def _build(self):
        pad = dict(padx=12, pady=6)

        tk.Label(self, text="演示数据 · 门铃识别", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", **pad)

        # 统计条
        stat = tk.Frame(self, bg=CARD)
        stat.pack(fill="x", padx=12, pady=(0, 6))
        self.stat_vars = {}
        for key, label in [("tpl", "模板"), ("thr", "阈值"), ("hits", "命中"),
                           ("ig", "忽略"), ("lat", "平均延迟"), ("best", "最高得分"),
                           ("snr", "平均信噪比")]:
            box = tk.Frame(stat, bg=CARD)
            box.pack(side="left", padx=14, pady=8)
            tk.Label(box, text=label, bg=CARD, fg=SUB,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w")
            v = tk.StringVar(value="—")
            tk.Label(box, textvariable=v, bg=CARD, fg=TEXT,
                     font=("Consolas", 13, "bold")).pack(anchor="w")
            self.stat_vars[key] = v

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # 左：事件列表
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 10))
        tk.Label(left, text="事件列表（点击查看）", bg=BG, fg=SUB,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        cols = ("time", "cls", "type", "score", "lat", "dur")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=22)
        for c, t, w in [("time", "时间", 66), ("cls", "事件", 64),
                        ("type", "判定", 52), ("score", "得分", 56),
                        ("lat", "延迟", 66), ("dur", "时长", 56)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="y", expand=True, pady=(4, 0))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # 右：四张图
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self.cv_wave = self._add_card(right, "波形（最近一次事件）", 430, 96, fill="x", pady=(0, 8))
        self.cv_spec = self._add_card(right, "对数梅尔谱（时频图）", 430, 170, fill="x", pady=(0, 8))

        row = tk.Frame(right, bg=BG)
        row.pack(fill="both", expand=True)
        self.cv_score = self._add_card(row, "各模板得分", 230, 150, side="left",
                                       fill="both", expand=True, padx=(0, 8))
        self.cv_trace = self._add_card(row, "事件内得分轨迹", 230, 150, side="left",
                                       fill="both", expand=True)

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(bottom, text="导出 CSV", command=self._export, bg="#22303f", fg=TEXT,
                  relief="flat", font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.lbl_tip = tk.Label(bottom, text="事件越多，数据越有说服力", bg=BG, fg=SUB,
                                font=("Microsoft YaHei UI", 8))
        self.lbl_tip.pack(side="left", padx=10)

        for cv in (self.cv_wave, self.cv_spec, self.cv_score, self.cv_trace):
            cv.bind("<Configure>", self._on_resize)

        self.after(300, self._tick)

    def _on_resize(self, _evt):
        if self._current is None:
            return
        try:
            self.after_idle(lambda: self._draw(self._current))
        except Exception:  # noqa: BLE001
            pass

    def _add_card(self, parent, title, w, h, **pack_kw):
        frame = tk.Frame(parent, bg=CARD)
        tk.Label(frame, text=title, bg=CARD, fg=SUB,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=10, pady=(6, 0))
        cv = tk.Canvas(frame, width=w, height=h, bg="#0b1118", highlightthickness=0)
        cv.pack(padx=10, pady=(2, 8), fill="both", expand=True)
        frame.pack(**pack_kw)
        return cv

    # ------------------------------------------------------------ 刷新
    def _tick(self):
        try:
            self._refresh_stats()
            hist = self.app.history
            if len(hist) != self._shown:
                self._refresh_tree()
            if hist and self._current is None:
                self._draw(hist[-1])
        except Exception:  # noqa: BLE001
            pass
        self.after(400, self._tick)

    def _refresh_stats(self):
        s = self.app.stats
        self.stat_vars["tpl"].set(str(len({os.path.basename(t.path) for t in self.app.templates})))
        self.stat_vars["thr"].set(f"{self.app.threshold.get():.2f}")
        self.stat_vars["hits"].set(str(s["hits"]))
        self.stat_vars["ig"].set(str(s["ignores"]))
        lats = s["latencies"]
        self.stat_vars["lat"].set(f"{np.mean(lats):.0f} ms" if lats else "—")
        scores = s["scores"]
        self.stat_vars["best"].set(f"{max(scores):.2f}" if scores else "—")
        snrs = s["snrs"]
        self.stat_vars["snr"].set(f"{np.mean(snrs):.1f} dB" if snrs else "—")

    def _refresh_tree(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i, ev in enumerate(self.app.history):
            tag = "hit" if ev["type"] == "hit" else "ig"
            self.tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                ev["time"], ev.get("label", "—"),
                "命中" if ev["type"] == "hit" else "忽略",
                f"{ev['score']:.2f}", f"{ev['latency_ms']:.0f}ms", f"{ev['dur']:.2f}s"))
        self.tree.tag_configure("hit", foreground=HIT)
        self.tree.tag_configure("ig", foreground=SUB)
        self._shown = len(self.app.history)

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.app.history):
            self._draw(self.app.history[idx])

    # ------------------------------------------------------------ 绘图
    def _draw(self, ev):
        self._current = ev
        wav = ev.get("wav")
        if wav is None:
            return
        self._draw_wave(self.cv_wave, wav)
        self._draw_spec(self.cv_spec, wav)
        self._draw_scores(self.cv_score, ev.get("tpl_scores", []))
        self._draw_trace(self.cv_trace, ev.get("trace", []), self.app.threshold.get())

    def _draw_wave(self, cv: tk.Canvas, wav: np.ndarray):
        cv.delete("all")
        w = max(cv.winfo_width(), 60)
        h = max(cv.winfo_height(), 40)
        n = len(wav)
        if n < 8:
            return
        step = max(1, n // w)
        mid = h / 2
        for x in range(w):
            lo = int(x * n / w)
            hi = min(n, lo + step)
            if hi <= lo:
                continue
            seg = wav[lo:hi]
            mn, mx = float(seg.min()), float(seg.max())
            y1 = mid - mx * mid * 0.95
            y2 = mid - mn * mid * 0.95
            cv.create_line(x, y1, x, y2, fill="#4aa3ff")
        cv.create_line(0, mid, w, mid, fill="#22303f")

    def _draw_spec(self, cv: tk.Canvas, wav: np.ndarray):
        cv.delete("all")
        w = max(cv.winfo_width(), 60)
        h = max(cv.winfo_height(), 40)
        try:
            mel = R.logmel(wav)                 # (T, 64)
        except Exception:  # noqa: BLE001
            return
        m = mel.T[::-1]                         # (64, T)，低频在下
        v = (m - np.percentile(m, 5)) / (np.percentile(m, 99) - np.percentile(m, 5) + 1e-9)
        rgb = _cmap(v)
        try:
            from PIL import Image, ImageTk

            img = Image.fromarray(rgb).resize((w, h), Image.BILINEAR)
            self._photo = ImageTk.PhotoImage(img)
            cv.create_image(0, 0, anchor="nw", image=self._photo)
        except Exception:  # noqa: BLE001  没有 Pillow 时降级为方块
            rows, cols = 32, 90
            for r in range(rows):
                for c in range(cols):
                    vv = v[int(r * 64 / rows), int(c * m.shape[1] / cols)]
                    r8, g8, b8 = _cmap(np.array(vv))
                    cv.create_rectangle(c * w / cols, r * h / rows,
                                        (c + 1) * w / cols, (r + 1) * h / rows,
                                        fill=f"#{r8:02x}{g8:02x}{b8:02x}", outline="")
        # 坐标提示
        cv.create_text(4, h - 8, anchor="w", fill="#8b98a5", text="低频", font=("Consolas", 7))
        cv.create_text(4, 8, anchor="w", fill="#8b98a5", text="高频", font=("Consolas", 7))

    def _draw_scores(self, cv: tk.Canvas, scores: list[tuple[str, float]]):
        cv.delete("all")
        w = max(cv.winfo_width(), 80)
        h = max(cv.winfo_height(), 60)
        if not scores:
            cv.create_text(w / 2, h / 2, text="暂无事件", fill=SUB)
            return
        scores = sorted(scores, key=lambda kv: -kv[1])[:5]
        thr = self.app.threshold.get()
        n = len(scores)
        bh = max(14, (h - 20) // max(n, 1) - 6)
        for i, (name, s) in enumerate(scores):
            y = 10 + i * (bh + 8)
            cv.create_text(4, y + bh / 2, anchor="w", fill=SUB,
                           text=name[:12], font=("Consolas", 7))
            x0 = 92
            bw = max(w - x0 - 46, 20)
            cv.create_rectangle(x0, y, x0 + bw, y + bh, fill="#1d2836", outline="")
            cv.create_rectangle(x0, y, x0 + bw * min(s, 1.0), y + bh,
                                fill=HIT if s >= thr else "#3d6d9a", outline="")
            cv.create_text(x0 + bw + 4, y + bh / 2, anchor="w", fill=TEXT,
                           text=f"{s:.2f}", font=("Consolas", 7))
            # 阈值虚线
            tx = x0 + bw * min(thr, 1.0)
            cv.create_line(tx, y - 2, tx, y + bh + 2, fill="#e0a300", dash=(3, 2))

    def _draw_trace(self, cv: tk.Canvas, trace: list[tuple[float, float]], thr: float):
        cv.delete("all")
        w = max(cv.winfo_width(), 80)
        h = max(cv.winfo_height(), 60)
        if not trace:
            cv.create_text(w / 2, h / 2, text="暂无事件", fill=SUB)
            return
        durs = [t[0] for t in trace]
        vals = [t[1] for t in trace]
        x_min, x_max = min(durs), max(durs)
        if x_max - x_min < 0.1:
            x_max = x_min + 0.1
        pad = 18
        pts = []
        for d, v in zip(durs, vals):
            x = pad + (d - x_min) / (x_max - x_min) * (w - 2 * pad)
            y = h - pad - v * (h - 2 * pad)
            pts.append((x, y))
        # 阈值线
        ty = h - pad - thr * (h - 2 * pad)
        cv.create_line(pad, ty, w - pad, ty, fill="#e0a300", dash=(3, 2))
        cv.create_text(w - pad - 2, ty - 8, anchor="e", fill="#e0a300",
                       text=f"阈值 {thr:.2f}", font=("Consolas", 7))
        if len(pts) >= 2:
            cv.create_line(*[c for p in pts for c in p], fill="#4aa3ff", width=2)
        for x, y in pts:
            cv.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#4aa3ff", outline="")
        cv.create_text(pad, h - 6, anchor="w", fill=SUB, text=f"{x_min:.1f}s",
                       font=("Consolas", 7))
        cv.create_text(w - pad, h - 6, anchor="e", fill=SUB, text=f"{x_max:.1f}s",
                       font=("Consolas", 7))

    # ------------------------------------------------------------ 导出
    def _export(self):
        path = os.path.join(R.SOUND_DIR, "session_log.csv")
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["时间", "事件", "判定", "得分", "延迟ms", "时长s", "模板", "信噪比dB"])
                for ev in self.app.history:
                    w.writerow([ev["time"], ev.get("label", ""), ev["type"],
                                f"{ev['score']:.4f}", f"{ev['latency_ms']:.0f}",
                                f"{ev['dur']:.2f}", ev.get("who", ""),
                                f"{ev.get('snr', 0):.1f}"])
            self.lbl_tip.configure(text=f"已导出：{path}")
        except OSError as e:
            self.lbl_tip.configure(text=f"导出失败：{e}")
