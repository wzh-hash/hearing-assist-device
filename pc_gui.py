#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门铃识别 · 图形界面（Tkinter，无需安装额外库）

设计原则（给听障用户）：
    主界面只留两件事——「学习门铃」「开始/停止监听」。
    所有技术参数（阈值、门控、麦克风）收进「高级设置」，默认不用碰。

运行：
    双击 run_gui.bat（会自动使用带依赖的 Python）
"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
LIB = os.path.join(BASE, "lib")
if os.path.isdir(LIB) and LIB not in sys.path:
    sys.path.insert(0, LIB)

import numpy as np  # noqa: E402

import pc_recognizer as R  # noqa: E402

try:
    import pc_dash  # noqa: E402
except Exception:  # noqa: BLE001
    pc_dash = None

C_BG = "#f4f6f8"
C_CARD = "#ffffff"
C_STOP = "#9aa5b1"
C_LISTEN = "#2e9e5b"
C_CAPTURE = "#e0a300"
C_HIT = "#e8562a"
C_TEXT = "#1f2933"
C_SUB = "#5b6875"
FONT = "Microsoft YaHei UI"


def _clip_db(db: float, lo: float = -70.0, hi: float = -10.0) -> float:
    return max(0.0, min(100.0, (db - lo) / (hi - lo) * 100.0))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("门铃识别 · 演示控制台")
        self.root.geometry("720x680")
        self.root.configure(bg=C_BG)
        self.root.minsize(680, 600)

        self.q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker: threading.Thread | None = None
        self.learner: threading.Thread | None = None
        self.templates: list = []
        self.devices: list[tuple[int, str]] = []
        self._flash_job = None
        self._advanced_open = False
        self.panel = None
        self.history: list[dict] = []
        self.stats = {"hits": 0, "ignores": 0, "latencies": [], "scores": [], "snrs": []}

        self.threshold = tk.DoubleVar(
            value=float(R.load_config().get("threshold", R.DEFAULT_THRESHOLD)))
        self.gate_db = tk.DoubleVar(value=R.GATE_DB)
        self.dev_var = tk.StringVar()

        self._build_ui()
        self._load_templates()
        self._scan_devices()
        self._poll()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ UI
    def _build_ui(self):
        pad = dict(padx=14, pady=6)

        header = tk.Frame(self.root, bg=C_BG)
        header.pack(fill="x", **pad)
        tk.Label(header, text="门铃识别", bg=C_BG, fg=C_TEXT,
                 font=(FONT, 16, "bold")).pack(side="left")
        tk.Label(header, text="听障人士环境事件感知 · Demo", bg=C_BG, fg=C_SUB,
                 font=(FONT, 9)).pack(side="left", padx=10, pady=4)
        self.btn_panel = tk.Button(header, text="📊 演示数据", command=self._open_panel,
                                   bg="#e6ebf0", fg=C_TEXT, relief="flat",
                                   font=(FONT, 9), padx=10)
        self.btn_panel.pack(side="right")

        # ---- 状态卡 ----
        card = tk.Frame(self.root, bg=C_CARD, highlightthickness=1,
                        highlightbackground="#dde3e9")
        card.pack(fill="x", padx=14, pady=6)
        top = tk.Frame(card, bg=C_CARD)
        top.pack(fill="x", padx=16, pady=(12, 0))
        self.dot = tk.Label(top, text="●", fg=C_STOP, bg=C_CARD, font=("Segoe UI", 14))
        self.dot.pack(side="left")
        self.lbl_state = tk.Label(top, text="未设置", bg=C_CARD, fg=C_TEXT,
                                  font=(FONT, 12, "bold"))
        self.lbl_state.pack(side="left", padx=6)
        self.lbl_info = tk.Label(top, text="", bg=C_CARD, fg=C_SUB, font=(FONT, 9))
        self.lbl_info.pack(side="right")

        self.lbl_big = tk.Label(card, text="请先学习门铃", bg=C_CARD, fg="#b6bec7",
                                font=(FONT, 26, "bold"))
        self.lbl_big.pack(pady=(8, 0))
        self.lbl_detail = tk.Label(card, text="点下面的「① 学习门铃」，然后按 3 次门铃",
                                   bg=C_CARD, fg=C_SUB, font=(FONT, 10))
        self.lbl_detail.pack(pady=(2, 14))

        # ---- 音量 ----
        lv = tk.Frame(self.root, bg=C_BG)
        lv.pack(fill="x", **pad)
        tk.Label(lv, text="音量", bg=C_BG, fg=C_SUB, font=(FONT, 9)).pack(side="left")
        self.meter = ttk.Progressbar(lv, orient="horizontal", length=430, mode="determinate")
        self.meter.pack(side="left", padx=8)
        self.lbl_db = tk.Label(lv, text="-- dB", bg=C_BG, fg=C_SUB, width=20, anchor="w",
                               font=("Consolas", 9))
        self.lbl_db.pack(side="left")

        # ---- 主按钮 ----
        main = tk.Frame(self.root, bg=C_BG)
        main.pack(fill="x", padx=14, pady=8)
        self.btn_learn = tk.Button(main, text="① 学习门铃（按 2 次）", command=self._learn,
                                   bg="#2f6fb3", fg="white", relief="flat",
                                   font=(FONT, 12, "bold"), height=2,
                                   activebackground="#285f99", activeforeground="white")
        self.btn_learn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_toggle = tk.Button(main, text="② 开始监听", command=self._toggle_listen,
                                    bg=C_LISTEN, fg="white", relief="flat",
                                    font=(FONT, 12, "bold"), height=2, state="disabled",
                                    activebackground="#248a4c", activeforeground="white")
        self.btn_toggle.pack(side="left", fill="x", expand=True)

        # ---- 高级设置（可折叠） ----
        self.adv_head = tk.Frame(self.root, bg=C_BG)
        self.adv_head.pack(fill="x", **pad)
        self.btn_adv = tk.Button(self.adv_head, text="▸ 高级设置（一般不用动）", command=self._toggle_adv,
                                 bg=C_BG, fg=C_SUB, relief="flat", bd=0, font=(FONT, 9),
                                 activebackground=C_BG)
        self.btn_adv.pack(side="left")

        self.adv = tk.Frame(self.root, bg=C_BG)

        row1 = tk.Frame(self.adv, bg=C_BG)
        row1.pack(fill="x", pady=2)
        tk.Label(row1, text="麦克风", bg=C_BG, fg=C_SUB, font=(FONT, 9)).pack(side="left")
        self.cmb_dev = ttk.Combobox(row1, textvariable=self.dev_var, width=36, state="readonly")
        self.cmb_dev.pack(side="left", padx=6)
        tk.Button(row1, text="刷新模板", command=self._load_templates, relief="flat",
                  bg="#e6ebf0", fg=C_TEXT, font=(FONT, 9)).pack(side="left", padx=4)
        tk.Button(row1, text="▶ 播放样本（会出声）", command=self._play_sound, relief="flat",
                  bg="#f0e9dd", fg=C_TEXT, font=(FONT, 9)).pack(side="left", padx=4)

        row2 = tk.Frame(self.adv, bg=C_BG)
        row2.pack(fill="x", pady=4)
        tk.Label(row2, text="判定阈值", bg=C_BG, fg=C_SUB, font=(FONT, 9)).pack(side="left")
        tk.Scale(row2, variable=self.threshold, from_=0.40, to=0.95, resolution=0.01,
                 orient="horizontal", length=220, bg=C_BG,
                 command=lambda _v: self._update_sliders()).pack(side="left", padx=6)
        self.lbl_thr = tk.Label(row2, text=f"{self.threshold.get():.2f}", bg=C_BG,
                                fg=C_TEXT, font=("Consolas", 10, "bold"), width=5)
        self.lbl_thr.pack(side="left")
        tk.Label(row2, text="门控 dB", bg=C_BG, fg=C_SUB, font=(FONT, 9)).pack(side="left", padx=(16, 0))
        tk.Scale(row2, variable=self.gate_db, from_=6, to=30, resolution=1,
                 orient="horizontal", length=140, bg=C_BG,
                 command=lambda _v: self._update_sliders()).pack(side="left", padx=6)
        self.lbl_gate = tk.Label(row2, text=f"{self.gate_db.get():.0f}", bg=C_BG,
                                 fg=C_TEXT, font=("Consolas", 10, "bold"), width=3)
        self.lbl_gate.pack(side="left")
        tk.Button(row2, text="保存阈值", command=self._save_threshold, relief="flat",
                  bg="#e6ebf0", fg=C_TEXT, font=(FONT, 9)).pack(side="left", padx=8)

        # ---- 日志 ----
        logf = tk.Frame(self.root, bg=C_BG)
        logf.pack(fill="both", expand=True, padx=14, pady=(4, 4))
        tk.Label(logf, text="事件日志", bg=C_BG, fg=C_SUB, font=(FONT, 9)).pack(anchor="w")
        self.log = tk.Text(logf, height=8, bg=C_CARD, fg=C_TEXT, relief="flat",
                           font=("Consolas", 9), highlightthickness=1,
                           highlightbackground="#dde3e9", state="disabled")
        self.log.pack(fill="both", expand=True, pady=(4, 0))

        self.footer = tk.Label(self.root, text="", bg=C_BG, fg=C_SUB,
                               font=(FONT, 8), anchor="w")
        self.footer.pack(fill="x", padx=14, pady=(0, 8))
        self._update_sliders()

    # ============================================================ 小工具
    def _log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _post(self, msg_type: str, **kw):
        self.q.put((msg_type, kw))

    def _update_sliders(self):
        self.lbl_thr.configure(text=f"{self.threshold.get():.2f}")
        self.lbl_gate.configure(text=f"{self.gate_db.get():.0f}")

    def _save_threshold(self):
        R.save_config({"threshold": round(float(self.threshold.get()), 2)})
        self._log(f"阈值已保存：{self.threshold.get():.2f}")

    def _toggle_adv(self):
        self._advanced_open = not self._advanced_open
        if self._advanced_open:
            self.btn_adv.configure(text="▾ 高级设置（一般不用动）")
            self.adv.pack(fill="x", padx=14, after=self.adv_head)
        else:
            self.btn_adv.configure(text="▸ 高级设置（一般不用动）")
            self.adv.pack_forget()

    def _load_templates(self):
        refs = R.learned_refs()
        self.templates = []
        if refs:
            try:
                self.templates = R.build_templates(refs)
            except SystemExit as e:
                self._log(str(e))
            kept = len(self.templates) // 3 if self.templates else 0
            if refs and kept < len(refs):
                self._log(f"提示：已跳过 {len(refs) - kept} 段不像门铃的参考音（多半是环境响动）")
        names = ", ".join(sorted({os.path.basename(t.path) for t in self.templates}))
        self.footer.configure(text=f"模板（{len(self.templates)}）：{names or '还没学习'}")
        if self.templates:
            self.btn_toggle.configure(state="normal")
            self._log(f"已加载模板：{names}")
        else:
            self.btn_toggle.configure(state="disabled")
            self.lbl_big.configure(text="请先学习门铃", fg="#b6bec7")
            self.lbl_detail.configure(text="点下面的「① 学习门铃」，然后按 2 次门铃")

    def _scan_devices(self):
        try:
            import sounddevice as sd

            self.devices = []
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    self.devices.append((i, d["name"]))
            names = [f"[{i}] {n}" for i, n in self.devices]
            self.cmb_dev["values"] = names
            try:
                default = sd.query_devices(kind="input")["name"]
            except Exception:  # noqa: BLE001
                default = ""
            idx = next((k for k, (i, n) in enumerate(self.devices) if n == default), 0)
            if names:
                self.cmb_dev.current(idx)
        except Exception as e:  # noqa: BLE001
            self._log(f"枚举麦克风失败：{e}")

    def _selected_device(self):
        txt = self.dev_var.get()
        if not txt:
            return None
        try:
            return int(txt.split("]")[0].strip("["))
        except Exception:  # noqa: BLE001
            return None

    # ============================================================ 学习
    def _learn(self):
        if self.learner and self.learner.is_alive():
            return
        if self.worker and self.worker.is_alive():
            self._log("请先停止监听，再学习门铃")
            return
        device = self._selected_device()
        self.btn_learn.configure(state="disabled", bg="#9bb4cf")
        self.btn_toggle.configure(state="disabled")
        self.learner = threading.Thread(target=self._learn_worker, args=(device,), daemon=True)
        self.learner.start()

    def _learn_worker(self, device):
        def progress(kind, kw):
            self._post("learn", kind=kind, kw=kw)

        try:
            self._post("big", text="准备学习…", color=C_CAPTURE)
            self._post("state", text="学习门铃中", color=C_CAPTURE)
            res = R.learn_doorbell(device, takes=R.LEARN_TAKES, progress=progress)
            self._post("learn_done", res=res)
        except Exception as e:  # noqa: BLE001
            self._post("learn_done", res={"ok": False, "reason": f"{type(e).__name__}: {e}"})
        finally:
            self._post("enable", which="learn")

    def _on_learn_progress(self, kind: str, kw: dict):
        if kind == "ask":
            self.lbl_big.configure(text=f"请按门铃（{kw['index']}/{kw['takes']}）", fg=C_CAPTURE)
            self.lbl_detail.configure(text=f"第 {kw['index']} 次，{R.LEARN_TIMEOUT:.0f} 秒内按一次门铃")
            self._log(f"第 {kw['index']}/{kw['takes']} 次：等待门铃…")
        elif kind == "warn":
            self._log(f"  {kw['text']}")
            self.lbl_detail.configure(text=kw["text"])
        elif kind == "saved":
            self._log(f"  第 {kw['index']} 次已录到：{kw['dur']:.2f}s {kw['peak']:.0f}dBFS")
        elif kind == "verified":
            self._log(f"三次一致性：最低 {kw['worst']:.2f}")
            for i, j, s in kw.get("pairs", [])[:3]:
                self._log(f"  第{i}段 vs 第{j}段 = {s:.2f}")

    def _on_learn_done(self, res: dict):
        self.btn_learn.configure(state="normal", bg="#2f6fb3")
        if res.get("ok"):
            thr = res["threshold"]
            self.threshold.set(round(thr, 2))
            self._update_sliders()
            self._load_templates()
            self.lbl_big.configure(text="学习完成 ✓", fg=C_LISTEN)
            self.lbl_detail.configure(
                text=f"阈值 {thr:.2f}。点「② 开始监听」即可")
            self._log(f"学习完成：一致性 {res['worst']:.2f}，阈值 {thr:.2f}")
            if res.get("warning"):
                self._log(f"提示：{res['warning']}")
            self.btn_toggle.configure(state="normal")
        else:
            self.lbl_big.configure(text="学习没成功", fg=C_HIT)
            self.lbl_detail.configure(text=res.get("reason", "请重试"))
            self._log(f"学习失败：{res.get('reason')}")

    # ============================================================ 监听
    def _toggle_listen(self):
        if self.worker and self.worker.is_alive():
            self._stop_listen()
        else:
            self._start_listen()

    def _start_listen(self):
        if not self.templates:
            self._log("请先学习门铃")
            return
        self.stop_evt.clear()
        threshold = float(self.threshold.get())
        sugg = R.threshold_from_templates(self.templates)
        if sugg is not None and threshold > sugg:
            self._log(f"按模板一致性把阈值从 {threshold:.2f} 自动下调到 {sugg:.2f}")
            threshold = sugg
            self.threshold.set(round(sugg, 2))
            self._update_sliders()
        gate = float(self.gate_db.get())
        device = self._selected_device()
        self.lbl_big.configure(text="监听中…", fg=C_LISTEN)
        self.lbl_detail.configure(text="听到门铃会大字提示")
        self.lbl_state.configure(text="启动中…")
        self.dot.configure(fg=C_CAPTURE)
        self.btn_toggle.configure(text="停止监听", bg=C_HIT, activebackground="#c94a22")
        self._log(f"开始监听（阈值 {threshold:.2f}，门控 {gate:.0f}dB）")
        self.worker = threading.Thread(target=self._listen_worker,
                                       args=(threshold, gate, device), daemon=True)
        self.worker.start()

    def _stop_listen(self):
        self.stop_evt.set()
        self.btn_toggle.configure(text="② 开始监听", bg=C_LISTEN, activebackground="#248a4c")
        self.dot.configure(fg=C_STOP)
        self.lbl_state.configure(text="已停止")
        self.lbl_big.configure(text="已停止", fg="#b6bec7")
        self.lbl_detail.configure(text="点「② 开始监听」继续")
        self.meter["value"] = 0
        self.lbl_db.configure(text="-- dB")
        self._log("停止监听")

    def _listen_worker(self, threshold: float, gate: float, device):
        try:
            det = R.Detector(self.templates, threshold=threshold, gate_db=gate)
            rec = R.Recorder(device)
            rec.start()
            self._post("state", text="监听中", color=C_LISTEN)
            while not self.stop_evt.is_set():
                block = rec.read_block(timeout=0.3)
                if block is None:
                    continue
                events = det.process(block)
                self._post("level", db=det.level, floor=det.floor)
                for ev in events:
                    if ev["type"] in ("hit", "ignore"):
                        wav = ev.get("wav")
                        try:
                            ev["tpl_scores"] = (R.template_scores(wav, det.templates)
                                                if wav is not None else [])
                            ev["peak_db"] = (float(20 * np.log10(np.max(np.abs(wav)) + 1e-9))
                                             if wav is not None else -120.0)
                        except Exception:  # noqa: BLE001
                            ev["tpl_scores"], ev["peak_db"] = [], -120.0
                        ev["snr"] = ev["peak_db"] - det.floor
                    self._post("event", ev=ev)
            rec.stop()
        except Exception as e:  # noqa: BLE001
            self._post("error", text=f"{type(e).__name__}: {e}")
        finally:
            self._post("state", text="已停止", color=C_STOP)

    # ============================================================ 轮询
    def _poll(self):
        try:
            while True:
                kind, kw = self.q.get_nowait()
                if kind == "event":
                    self._on_event(kw["ev"])
                elif kind == "level":
                    db, floor = kw["db"], kw["floor"]
                    if db < -85:
                        self.meter["value"] = 0
                        self.lbl_db.configure(text=f"     -- dB（底噪 {floor:5.1f}）")
                    else:
                        self.meter["value"] = _clip_db(db)
                        self.lbl_db.configure(text=f"{db:6.1f} dB（底噪 {floor:5.1f}）")
                elif kind == "state":
                    self.lbl_state.configure(text=kw["text"])
                    self.dot.configure(fg=kw["color"])
                elif kind == "big":
                    self.lbl_big.configure(text=kw["text"], fg=kw["color"])
                elif kind == "log":
                    self._log(kw["text"])
                elif kind == "learn":
                    self._on_learn_progress(kw["kind"], kw["kw"])
                elif kind == "learn_done":
                    self._on_learn_done(kw["res"])
                elif kind == "enable":
                    if kw["which"] == "learn":
                        self.btn_learn.configure(state="normal", bg="#2f6fb3")
                elif kind == "error":
                    self._log(f"错误：{kw['text']}")
                    self._stop_listen()
        except queue.Empty:
            pass
        self.root.after(25, self._poll)

    def _on_event(self, ev: dict):
        if ev["type"] == "trigger":
            self.dot.configure(fg=C_CAPTURE)
            self.lbl_state.configure(text="检测到声音")
            self.lbl_big.configure(text="听到声音…", fg=C_CAPTURE)
            return

        entry = {
            "time": time.strftime("%H:%M:%S"),
            "type": ev["type"],
            "score": float(ev["score"]),
            "latency_ms": float(ev["latency"]) * 1000,
            "dur": float(ev["dur"]),
            "wav": ev.get("wav"),
            "trace": ev.get("trace", []),
            "tpl_scores": ev.get("tpl_scores", []),
            "who": ev.get("who", ""),
            "snr": float(ev.get("snr", 0.0)),
        }
        self.history.append(entry)
        if len(self.history) > 60:
            self.history.pop(0)
        if entry["type"] == "hit":
            self.stats["hits"] += 1
            self.stats["latencies"].append(entry["latency_ms"])
            self.stats["scores"].append(entry["score"])
        else:
            self.stats["ignores"] += 1
        self.stats["snrs"].append(entry["snr"])

        if ev["type"] == "hit":
            self.lbl_big.configure(text="门铃！", fg=C_HIT)
            self.lbl_detail.configure(
                text=f"得分 {ev['score']:.2f}　延迟 {ev['latency']*1000:.0f} ms　{time.strftime('%H:%M:%S')}")
            self._log(f"命中 门铃  score={ev['score']:.3f}  "
                      f"调性={ev.get('tonal', 0):.0f}  latency={ev['latency']*1000:.0f}ms")
            self._flash()
            self.dot.configure(fg=C_CAPTURE)
            self.lbl_state.configure(text="等待声音…")
        else:
            self._log(f"忽略  score={ev['score']:.3f}  调性={ev.get('tonal', 0):.0f}")
            self.dot.configure(fg=C_LISTEN)
            self.lbl_state.configure(text="监听中")
            self.lbl_big.configure(text="监听中…", fg=C_LISTEN)

    def _open_panel(self):
        if pc_dash is None:
            self._log("数据面板模块 pc_dash.py 缺失")
            return
        if self.panel is not None and self.panel.winfo_exists():
            self.panel.lift()
            return
        self.panel = pc_dash.DataPanel(self.root, self)
        self._log("已打开演示数据面板")

    def _flash(self):
        self.root.configure(bg="#ffe3d6")
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self._flash_job = self.root.after(500, lambda: self.root.configure(bg=C_BG))

    # ============================================================ 其他
    def _play_sound(self):
        def run():
            try:
                import sounddevice as sd

                x, sr = R.load_wav(R.SOUND_FILE)
                self._post("log", text="播放门铃样本…")
                sd.play(x, sr)
                sd.wait()
            except Exception as e:  # noqa: BLE001
                self._post("log", text=f"播放失败：{e}")
        threading.Thread(target=run, daemon=True).start()

    def _on_close(self):
        self.stop_evt.set()
        time.sleep(0.1)
        self.root.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:  # noqa: BLE001
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
