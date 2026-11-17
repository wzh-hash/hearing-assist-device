"""识别、学习、试听、设置与 IoT 的编排层。

与桌面版共用同一套 ``Detector`` / ``Recorder`` / ``learn_doorbell``，不改识别
算法；网页只在外围加一层状态机、播放与上报。
"""

from __future__ import annotations

import glob
import math
import os
import secrets
import threading
import time
import traceback

import numpy as np

import pc_recognizer as R
from doorbell.iot import (
    ConfigError as IotConfigError,
    IotLink,
    Status,
    apply_update,
    load as iot_load,
    save as iot_save,
)

from . import config as config_mod
from .audio import AudioPlayer, wav_duration
from .errors import ApiError
from .state import StateBus

EVENT_CLASSES = tuple(R.EVENT_CLASSES.keys())
STATUS_MAP = {
    "已关闭": "unset",
    "未配置": "unset",
    "连接中": "connecting",
    "已连接": "online",
    "重连中": "reconnecting",
    "失败": "failed",
}


class LearnCancelled(Exception):
    """用户主动取消学习；由可取消 Recorder / 进度回调抛出。"""


class CancelableRecorder(R.Recorder):
    """在每次读块前后检查取消事件，让学习不必等满 15 秒超时。"""

    def __init__(self, device, cancel: threading.Event):
        self._cancel = cancel
        super().__init__(device)

    def read_block(self, timeout: float = 0.5):
        if self._cancel.is_set():
            raise LearnCancelled()
        block = super().read_block(timeout=min(float(timeout or 0.5), 0.2))
        if self._cancel.is_set():
            raise LearnCancelled()
        return block


class App:
    def __init__(self, *, no_lan: bool = False):
        self.config = config_mod.load()
        if no_lan:
            self.config["lan"] = False

        self.bus = StateBus(os.path.join(R.SOUND_DIR, "events"))
        self.bus.set_config(config_mod.public(self.config))

        self.net = {"local": "", "lan": "", "port": 0, "lan_enabled": self.lan_enabled}
        self.bus.set_net(self.net)

        self.devices = self._scan_devices()
        self.bus.set_devices(self.devices)

        self.templates: list = []
        self.template_names: list[str] = []
        self.refresh_templates(initial=True)

        self.audio = AudioPlayer(self.log)
        self.iot_settings = iot_load()
        self.iot_link = IotLink(self.iot_settings)

        self._lock = threading.RLock()
        self._closed = False
        self._tokens: set[str] = set()

        self._listen_stop = threading.Event()
        self._listen_thread: threading.Thread | None = None
        self._detector = None
        self._stopping = False

        self._learn_thread: threading.Thread | None = None
        self._learn_cancel = threading.Event()

        self._suppress_until = 0.0
        self._playing_until = 0.0

        self._ticker_stop = threading.Event()
        self._ticker: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.iot_link.start()
        self._ticker = threading.Thread(target=self._tick_loop, name="doorbell-ticker", daemon=True)
        self._ticker.start()
        threading.Thread(
            target=self.audio.preopen,
            args=(self._output_index(),),
            name="doorbell-preopen",
            daemon=True,
        ).start()
        self.log("服务已启动")

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_learn()
        self.stop_listen()

        for thread, timeout in ((self._learn_thread, 2.0), (self._listen_thread, 2.0)):
            if thread and thread.is_alive():
                try:
                    thread.join(timeout=timeout)
                except Exception:  # noqa: BLE001
                    pass

        self._ticker_stop.set()
        if self._ticker and self._ticker.is_alive():
            try:
                self._ticker.join(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass

        try:
            self.audio.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.iot_link.stop()
        except Exception:  # noqa: BLE001
            pass
        self.log("服务已停止")

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    @property
    def lan_enabled(self) -> bool:
        return bool(self.config.get("lan", True))

    def lan_allows(self, address: str) -> bool:
        if self.lan_enabled:
            return True
        return address in ("127.0.0.1", "::1", "localhost", "")

    def log(self, text: str) -> None:
        entry = self.bus.add_log(text)
        try:
            print(f"[{entry['time']}] {text}", flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _scan_devices(self) -> dict:
        inputs = [{"index": -1, "name": "系统默认（跟随 Windows）"}]
        outputs = [{"index": -1, "name": "系统默认（跟随 Windows）"}]
        try:
            import sounddevice as sd

            default_in = ""
            default_out = ""
            try:
                default_in = str(sd.query_devices(kind="input").get("name") or "")
            except Exception:  # noqa: BLE001
                pass
            try:
                default_out = str(sd.query_devices(kind="output").get("name") or "")
            except Exception:  # noqa: BLE001
                pass
            if default_in:
                inputs[0]["name"] = f"系统默认（{default_in}）"
            if default_out:
                outputs[0]["name"] = f"系统默认（{default_out}）"

            for index, device in enumerate(sd.query_devices()):
                if int(device.get("max_input_channels", 0)) > 0:
                    inputs.append({"index": index, "name": str(device.get("name") or index)})
                if int(device.get("max_output_channels", 0)) > 0:
                    outputs.append({"index": index, "name": str(device.get("name") or index)})
        except Exception as exc:  # noqa: BLE001
            self.log(f"枚举音频设备失败：{exc}")
        return {"inputs": inputs, "outputs": outputs}

    def refresh_templates(self, *, initial: bool = False) -> None:
        learned = sorted(glob.glob(os.path.join(R.SOUND_DIR, "learned_*.wav")))
        # 试听只加载实际门铃那一个模板：有学习结果就不再叠加内置四类参考。
        refs = learned if learned else R.learned_refs()
        templates: list = []
        try:
            templates = R.build_templates(refs)
        except SystemExit as exc:
            self.log(f"模板加载失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"模板加载失败：{exc}")

        self.templates = templates
        self.template_names = sorted({os.path.basename(t.path) for t in templates})
        skipped = max(0, len(refs) - len(self.template_names))
        self.bus.set_templates({
            "count": len(self.template_names),
            "names": self.template_names,
            "skipped": skipped,
        })

        if initial:
            if self.template_names:
                if learned:
                    self.log(
                        "已加载 %d 个学习模板（%s），只使用这些模板"
                        % (len(self.template_names), "、".join(self.template_names))
                    )
                else:
                    self.log(f"已加载 {len(self.template_names)} 个模板（内置门铃/敲门/水壶/火警可直接使用）")
            else:
                self.log("没有可用模板：请先学习门铃")
        else:
            self.log(f"模板已刷新：{len(self.template_names)} 个")
        if skipped:
            self.log(f"提示：已跳过 {skipped} 段不像门铃的参考音")

    def set_net(self, local: str, lan: str, port: int) -> None:
        self.net = {
            "local": local,
            "lan": lan,
            "port": port,
            "lan_enabled": self.lan_enabled,
        }
        self.bus.set_net(self.net)

    def public_config(self) -> dict:
        return config_mod.public(self.config)

    def _output_index(self):
        index = int(self.config.get("speaker_index", -1))
        return None if index < 0 else index

    def _input_index(self):
        index = int(self.config.get("mic_index", -1))
        return None if index < 0 else index

    # ------------------------------------------------------------------
    # 8 Hz 快照
    # ------------------------------------------------------------------

    def _tick_loop(self) -> None:
        while not self._ticker_stop.is_set():
            try:
                self.bus.expire()
                self._follow_demo_display()
                self._refresh_iot()
                self.bus.broadcast_state()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            self._ticker_stop.wait(0.125)

    def _follow_demo_display(self) -> None:
        """试听的大字跟随音频的真实有声区间，不在静音尾/间隙里硬撑。"""
        playing = self.bus.playing
        if not playing or playing.get("test"):
            return
        if playing.get("cls") not in R.EVENT_CLASSES:
            return
        active = playing.get("active") or []
        start = int(playing.get("start") or 0)
        if not active or not start:
            return
        last = self.bus.last_hit
        if not last or int(last.get("ts") or 0) < start:
            return
        elapsed = int(time.time() * 1000) - start
        if not any(int(begin) <= elapsed <= int(end) for begin, end in active):
            return
        if self.bus.hit is not None:
            self.bus.extend_hit(350, max_total_ms=12000)
        else:
            self.bus.reshow_last_hit()

    def _active_intervals(self, path: str, block_ms: int = 50) -> list[list[int]]:
        """音频里的有声区间（ms），用于试听时让大字跟着声音而不是文件长度。"""
        try:
            audio, rate = R.load_wav(path)
        except Exception:  # noqa: BLE001
            return []
        audio = np.asarray(R.to_16k(audio, rate), dtype=np.float32)
        if audio.size == 0:
            return []
        block = max(1, int(R.SR * block_ms / 1000))
        levels: list[float] = []
        for start in range(0, len(audio), block):
            chunk = audio[start:start + block].astype(np.float64)
            levels.append(float(10 * np.log10(np.mean(chunk ** 2) + 1e-12)))
        if not levels:
            return []
        floor = float(np.percentile(levels, 20))
        gate = floor + float(self.config.get("gate_db") or 8)
        raw: list[list[int]] = []
        active_start: int | None = None
        for index, level in enumerate(levels):
            if level > gate and active_start is None:
                active_start = index
            elif level <= gate and active_start is not None:
                raw.append([active_start * block_ms, index * block_ms])
                active_start = None
        if active_start is not None:
            raw.append([active_start * block_ms, len(levels) * block_ms])

        merged: list[list[int]] = []
        for begin, end in raw:
            if merged and begin - merged[-1][1] <= 250:
                merged[-1][1] = end
            else:
                merged.append([begin, end])
        return [[int(begin), int(end)] for begin, end in merged if end - begin >= 120]

    def _refresh_iot(self) -> None:
        snap = self.iot_link.snapshot()
        status = STATUS_MAP.get(getattr(snap.status, "value", str(snap.status)), "unset")
        self.bus.set_iot({
            "state": status,
            "last_ok": int(snap.last_sent_at * 1000) if snap.last_sent_at else None,
            "last_error": snap.last_error or "",
            "sent": int(snap.sent),
            "dropped": int(snap.dropped),
            "config": self._iot_public(),
        })
        self.bus.set_board({"online": snap.board_online is True})

    def _iot_public(self) -> dict:
        settings = self.iot_settings
        return {
            "address": settings.host,
            "port": settings.port,
            "iot_id": settings.iot_id,
            "topic_event": settings.event_topic,
            "topic_state": settings.status_topic,
            "enabled": settings.enabled,
            "qos": settings.qos,
            "report_ignore": settings.publish_ignored,
            "password_set": bool(settings.iot_pwd),
        }

    # ------------------------------------------------------------------
    # 监听
    # ------------------------------------------------------------------

    def start_listen(self) -> dict:
        with self._lock:
            if self._closed:
                raise ApiError("服务正在退出", 503)
            if self.bus.learn is not None or (self._learn_thread and self._learn_thread.is_alive()):
                raise ApiError("正在学习，请先取消学习", 409)
            if self._stopping:
                raise ApiError("正在停止上一次监听，请稍候", 409)
            if self._listen_thread and self._listen_thread.is_alive():
                return {"ok": True}
            if not self.templates:
                raise ApiError("没有可用模板，请先学习门铃", 409)

            threshold = float(self.config["threshold"])
            suggested = R.threshold_from_templates(self.templates)
            if suggested is not None and threshold > suggested:
                old_threshold = threshold
                threshold = round(float(suggested), 2)
                self.config["threshold"] = threshold
                config_mod.save(self.config)
                self.bus.set_config(self.public_config())
                self.log(f"按模板一致性把阈值从 {old_threshold:.2f} 自动下调到 {threshold:.2f}")

            self._listen_stop = threading.Event()
            self.bus.set_listening(True)
            self.log(f"开始监听（阈值 {threshold:.2f}，门控 {int(self.config['gate_db'])} dB）")
            thread = threading.Thread(
                target=self._listen_loop,
                args=(self._listen_stop, threshold),
                name="doorbell-listen",
                daemon=True,
            )
            self._listen_thread = thread
            thread.start()
        return {"ok": True}

    def stop_listen(self) -> dict:
        with self._lock:
            self._listen_stop.set()
            self.bus.set_listening(False)
            thread = self._listen_thread
            if thread and thread.is_alive():
                self._stopping = True
                threading.Thread(
                    target=self._join_listener, args=(thread,), name="doorbell-join", daemon=True
                ).start()
            else:
                self._listen_thread = None
                self._stopping = False
            self.log("停止监听")
        return {"ok": True}

    def _join_listener(self, thread: threading.Thread) -> None:
        try:
            thread.join(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            if self._listen_thread is thread:
                self._listen_thread = None
            self._stopping = False

    def _listen_loop(self, stop: threading.Event, threshold: float) -> None:
        rec = None
        detector = None
        suppressed = False
        try:
            detector = R.Detector(
                self.templates,
                threshold=threshold,
                gate_db=float(self.config["gate_db"]),
                cooldown=R.COOLDOWN,
                early=True,
                save_dir=None,
                strict=True,
            )
            self._detector = detector
            rec = R.Recorder(self._input_index())
            rec.start()
            self.log("麦克风已打开")

            while not stop.is_set():
                block = rec.read_block(timeout=0.2)
                if block is None:
                    continue
                if time.time() < self._suppress_until:
                    if not suppressed:
                        # 播放期间丢弃麦克风块；与桌面版一致，试听前清掉未完成的采集。
                        detector.reset()
                        suppressed = True
                    self.bus.set_level(self._block_db(block), detector.floor)
                    continue
                if suppressed:
                    detector.reset()
                    suppressed = False
                events = detector.process(block)
                level = detector.level
                gate = detector.floor + float(detector.gate_db)
                above = level > gate
                if above and self.bus.hit is not None:
                    # 只在声音仍然存在时顺延；声音一停，约 0.4 秒后消失。
                    # 不在这里重亮上一次命中：那会把“别的声响”显示成旧类别。
                    self.bus.extend_hit(400, max_total_ms=12000)
                self.bus.set_level(detector.level, detector.floor)
                for event in events:
                    self._handle_event(event, detector, demo=False)

        except SystemExit as exc:
            self._fail_listen(f"打不开麦克风：{exc}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._fail_listen(f"监听中断：{type(exc).__name__}: {exc}")
        finally:
            if rec is not None:
                try:
                    rec.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._detector = None
            with self._lock:
                if self._listen_thread is threading.current_thread():
                    self._listen_thread = None
                self._stopping = False
            self.log("监听线程已退出")

    def _fail_listen(self, message: str) -> None:
        self.log(message)
        self.bus.set_listening(False)
        title = "麦克风不可用" if "麦克风" in message else "监听中断"
        self.bus.set_notice({
            "kind": "error",
            "title": title,
            "detail": message,
            "ts": int(time.time() * 1000),
            "until": int(time.time() * 1000) + 24 * 3600 * 1000,
        })

    def _handle_event(self, event: dict, detector, demo: bool) -> None:
        kind = event.get("type")
        if kind == "trigger":
            self.bus.note_heard(0.6)
            return
        if kind in ("hit", "ignore"):
            self._record_event(event, detector, demo=demo)

    # ------------------------------------------------------------------
    # 事件入库
    # ------------------------------------------------------------------

    def _record_event(self, event: dict, detector, demo: bool) -> None:
        wav = event.get("wav")
        score = float(event.get("score") or 0.0)
        conf = float(event.get("conf") or 0.0)
        dur = float(event.get("dur") or 0.0)

        tpl_scores: list[list] = []
        peak_db = None
        if isinstance(wav, np.ndarray) and wav.size:
            try:
                query = wav
                if wav.size >= int(0.45 * R.SR):
                    query = R.trim_active(wav, rel_db=18.0, margin=0.08, min_dur=0.4)
                tpl_scores = [
                    [name, round(float(score_value), 2)]
                    for name, score_value in R.template_scores(query, detector.templates)
                ]
            except Exception:  # noqa: BLE001
                tpl_scores = []
            try:
                peak_db = round(float(20 * np.log10(np.max(np.abs(wav)) + 1e-9)), 1)
            except Exception:  # noqa: BLE001
                peak_db = None

        snr = None
        if peak_db is not None:
            try:
                snr = round(peak_db - float(detector.floor), 1)
            except Exception:  # noqa: BLE001
                snr = None

        trace_points = event.get("trace") or []
        trace_series: list[list] = []
        for point in trace_points:
            try:
                trace_series.append([round(float(point[0]), 2), round(float(point[1]), 2)])
            except (TypeError, ValueError, IndexError):
                continue
        if trace_series:
            best = max(trace_series, key=lambda item: item[1])
            trace = {"score": best[1], "at": best[0]}
        else:
            trace = {"score": round(score, 2), "at": round(dur, 2)}

        entry_type = str(event.get("type") or "hit")
        cls = str(event.get("cls") or "")
        label = str(event.get("label") or "")
        tier = str(event.get("tier") or "L1")
        reason = None
        if entry_type == "ignore":
            threshold = float(self.config["threshold"])
            if score < threshold:
                reason = f"得分 {score:.2f} 低于阈值 {threshold:.2f}"
            else:
                reason = (
                    f"声学指标未通过（调性 {float(event.get('tonal') or 0):.0f}，"
                    f"调制 {float(event.get('mod') or 0):.0%}）"
                )

        wav_file = self._save_recording(wav, entry_type, cls, score, conf, demo)

        normalized = {
            "type": entry_type,
            "cls": cls,
            "label": label,
            "tier": tier,
            "conf": round(conf, 2),
            "score": round(score, 2),
            "who": str(event.get("who") or ""),
            "dur": round(dur, 2),
            "latency": int(round(float(event.get("latency") or 0.0) * 1000)),
            "tonal": round(float(event.get("tonal") or 0.0), 2),
            "mod": round(float(event.get("mod") or 0.0), 2),
            "decay": round(float(event.get("decay") or 0.0), 2),
            "stab": round(float(event.get("stab") or 0.0), 2),
            "freq_ratio": round(float(event.get("freq_ratio") or 1.0), 2),
            "conc_p90": round(float(event.get("conc_p90") or 0.0), 2),
            "n_knock": int(event.get("n_knock") or 0),
            "n_pulse": int(event.get("n_pulse") or 0),
            "sustain": round(float(event.get("sustain") or 0.0), 2),
            "peak_db": peak_db,
            "snr": snr,
            "trace": trace,
            "trace_points": trace_series,
            "threshold": round(float(self.config["threshold"]), 2),
            "tpl_scores": tpl_scores,
            "reason": reason,
            "demo": bool(demo),
            "wav_file": wav_file,
        }

        public = self.bus.add_event(normalized)
        try:
            self.iot_link.publish({
                "type": entry_type,
                "cls": cls,
                "tier": tier,
                "score": score,
                "latency": float(event.get("latency") or 0.0),
                "ts": public["ts"] / 1000.0,
                "demo": bool(demo),
            })
        except Exception:  # noqa: BLE001
            pass

        prefix = "试听命中" if demo else ("命中" if entry_type == "hit" else "忽略")
        self.log(
            f"{prefix}：{label or '未知'}（{tier}）得分 {score:.2f} "
            f"延迟 {int(round(float(event.get('latency') or 0.0) * 1000))} ms"
        )

    def _save_recording(self, wav, entry_type: str, cls: str, score: float,
                        conf: float, demo: bool) -> str | None:
        if not isinstance(wav, np.ndarray) or not wav.size:
            return None
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            suffix = "_demo" if demo else ""
            name = (
                f"{stamp}_{entry_type}_{cls or 'none'}_s{score:.2f}_c{conf:.2f}"
                f"{suffix}.wav"
            )
            folder = os.path.join(R.SOUND_DIR, "events")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, name)
            counter = 1
            while os.path.exists(path):
                name = (
                    f"{stamp}_{entry_type}_{cls or 'none'}_s{score:.2f}_c{conf:.2f}"
                    f"{suffix}_{counter}.wav"
                )
                path = os.path.join(folder, name)
                counter += 1
                if counter > 999:
                    break
            R.save_wav(path, wav)

            old = sorted(glob.glob(os.path.join(folder, "*.wav")))
            for stale in old[:-300]:
                try:
                    os.remove(stale)
                except OSError:
                    pass
            return name
        except Exception as exc:  # noqa: BLE001
            self.log(f"保存判定录音失败：{exc}")
            return None

    @staticmethod
    def _block_db(block: np.ndarray) -> float:
        try:
            return float(10 * np.log10(np.mean(np.asarray(block, dtype=np.float64) ** 2) + 1e-12))
        except Exception:  # noqa: BLE001
            return -120.0

    # ------------------------------------------------------------------
    # 事件数据图（波形 / 对数梅尔谱）
    # ------------------------------------------------------------------

    def event_analysis(self, seq: int) -> dict:
        """从该次判定录音生成波形包络与对数梅尔谱（按需，不常驻）。"""
        event = self.bus.find_event(seq)
        if not event:
            raise ApiError("事件不存在", 404)
        name = event.get("wav_file")
        if not name:
            raise ApiError("这次判定没有录音", 404)
        path = self.bus.recording_path(name)
        if path is None:
            raise ApiError("录音不存在或已清空", 404)

        try:
            audio, rate = R.load_wav(str(path))
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"录音读取失败：{exc}", 500) from None
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size < int(0.05 * R.SR):
            raise ApiError("录音太短，无法绘图", 422)
        rate = int(rate or R.SR)
        duration = len(audio) / float(rate)

        bins = 240
        step = max(1, len(audio) // bins)
        waveform = []
        for start in range(0, len(audio), step):
            chunk = audio[start:start + step]
            if chunk.size:
                waveform.append([round(float(chunk.min()), 3), round(float(chunk.max()), 3)])
            if len(waveform) >= bins:
                break

        try:
            mel = R.logmel(audio)  # (帧数, 64)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"梅尔谱计算失败：{exc}", 500) from None
        frames, bands = mel.shape
        if frames < 1 or bands < 1:
            raise ApiError("录音太短，无法绘图", 422)

        cols = min(120, max(1, frames))
        step_frames = max(1, frames // cols)
        rows = min(64, bands)
        matrix: list[list[float]] = []
        for band in range(rows):
            row = []
            for column in range(cols):
                chunk = mel[column * step_frames:(column + 1) * step_frames, band]
                row.append(float(chunk.mean()) if getattr(chunk, "size", 0) else -100.0)
            matrix.append(row)

        flat = np.asarray(matrix, dtype=np.float64)
        low = float(np.percentile(flat, 5))
        high = float(np.percentile(flat, 95))
        if high - low < 1e-6:
            high = low + 1e-6
        normalized = [
            [round(max(0.0, min(1.0, (value - low) / (high - low))), 3) for value in row]
            for row in matrix
        ]

        return {
            "duration": round(duration, 2),
            "sample_rate": rate,
            "waveform": waveform,
            "mel": normalized,
            "rows": rows,
            "cols": cols,
        }

    # ------------------------------------------------------------------
    # 学习
    # ------------------------------------------------------------------

    def start_learn(self, takes: int | None = None) -> dict:
        with self._lock:
            if self._closed:
                raise ApiError("服务正在退出", 503)
            if self._stopping:
                raise ApiError("正在停止监听，请稍后再学习", 409)
            if self._listen_thread and self._listen_thread.is_alive() or self.bus.on_air:
                raise ApiError("请先停止监听再学习", 409)
            if self.bus.learn is not None or (self._learn_thread and self._learn_thread.is_alive()):
                learn = self.bus.learn or {}
                take = learn.get("take", 1)
                total = learn.get("total", int(self.config["takes"]))
                raise ApiError(f"另一台设备正在学习（{take}/{total}）", 409)

            try:
                takes = int(takes) if takes is not None else int(self.config["takes"])
            except (TypeError, ValueError):
                raise ApiError("学习次数必须是整数") from None
            if not 1 <= takes <= 5:
                raise ApiError("学习次数需在 1 到 5 之间")
            self.audio.stop_current()
            self._suppress_until = 0.0
            self.bus.set_playing(None)
            self.bus.set_notice(None)
            self._learn_cancel.clear()
            self.bus.set_learning({
                "take": 1,
                "total": takes,
                "window": R.LEARN_TIMEOUT,
                "attempts": 0,
                "deadline": int(time.time() * 1000) + int(R.LEARN_TIMEOUT * 1000),
                "message": "",
            })
            self.log(f"开始学习，共 {takes} 次")
            thread = threading.Thread(
                target=self._learn_loop, args=(takes,), name="doorbell-learn", daemon=True
            )
            self._learn_thread = thread
            thread.start()
        return {"ok": True, "takes": takes}

    def cancel_learn(self) -> dict:
        with self._lock:
            if self.bus.learn is None and not (self._learn_thread and self._learn_thread.is_alive()):
                return {"ok": True}
            self._learn_cancel.set()
            self.log("正在取消学习…")
        return {"ok": True}

    def _learn_progress(self, kind: str, kw: dict) -> None:
        if self._learn_cancel.is_set():
            raise LearnCancelled()

        if kind == "ask":
            self.bus.update_learn(
                take=int(kw.get("index", 1)),
                total=int(kw.get("takes", self.config["takes"])),
                attempts=int(kw.get("attempt", 0)),
                deadline=int(time.time() * 1000) + int(R.LEARN_TIMEOUT * 1000),
            )
            self.log(f"第 {kw.get('index')}/{kw.get('takes')} 次：等待门铃…")
        elif kind == "warn":
            self.bus.update_learn(message=str(kw.get("text", "")))
            self.log(f"  {kw.get('text', '')}")
        elif kind == "saved":
            message = (
                f"第 {kw.get('index')} 次已录到：{float(kw.get('dur', 0)):.2f}s "
                f"{float(kw.get('peak', 0)):.0f}dBFS"
            )
            self.bus.update_learn(message=message)
            self.log(f"  {message}")
        elif kind == "verified":
            message = f"几次一致性：最低 {float(kw.get('worst', 0)):.2f}"
            self.bus.update_learn(message=message)
            self.log(f"  {message}")

    def _learn_loop(self, takes: int) -> None:
        device = self._input_index()
        res: dict = {}
        try:
            res = R.learn_doorbell(
                device,
                takes=takes,
                progress=self._learn_progress,
                recorder_factory=lambda: CancelableRecorder(device, self._learn_cancel),
            )
        except LearnCancelled:
            self._cleanup_learn_temps()
            self.bus.set_learning(None)
            self.bus.set_notice({
                "kind": "learn_cancelled",
                "title": "已取消学习",
                "detail": "原来的模板与阈值保持不变。",
                "ts": int(time.time() * 1000),
                "until": int(time.time() * 1000) + 6000,
            })
            self.log("已取消学习，原有模板与阈值保持不变")
            return
        except SystemExit as exc:
            self._learn_failed(f"打不开麦克风：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._learn_failed(f"学习中断：{type(exc).__name__}: {exc}")
            return
        finally:
            with self._lock:
                if self._learn_thread is threading.current_thread():
                    self._learn_thread = None

        if res.get("ok"):
            try:
                threshold = round(float(res.get("threshold", self.config["threshold"])), 2)
                self.config["threshold"] = threshold
                config_mod.save(self.config)
                self.bus.set_config(self.public_config())
                self.refresh_templates()
                self.bus.set_learning(None)
                self.bus.set_notice({
                    "kind": "learn_done",
                    "title": "学习完成 ✓",
                    "detail": f"新阈值 {threshold:.2f}，已可用于监听。",
                    "ts": int(time.time() * 1000),
                    "until": int(time.time() * 1000) + 6000,
                })
                self.log(f"学习完成，新阈值 {threshold:.2f}")
                if res.get("warning"):
                    self.log(f"提示：{res['warning']}")
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._learn_failed(f"学习结果保存失败：{exc}")
        else:
            self._learn_failed(str(res.get("reason") or "没有录到有效门铃"))

    def _learn_failed(self, reason: str) -> None:
        self._cleanup_learn_temps()
        self.bus.set_learning(None)
        self.bus.set_notice({
            "kind": "learn_failed",
            "title": "没有学到",
            "detail": reason,
            "ts": int(time.time() * 1000),
            "until": int(time.time() * 1000) + 8000,
        })
        self.log(f"学习失败：{reason}")

    def _cleanup_learn_temps(self) -> None:
        for path in glob.glob(os.path.join(R.SOUND_DIR, "_learn_tmp_*.wav")):
            try:
                os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 试听
    # ------------------------------------------------------------------

    def preview(self, cls: str | None = None, test: bool = False,
                file: str | None = None) -> dict:
        with self._lock:
            if self.bus.mode == "learning":
                raise ApiError("学习进行中，暂时不能试听", 409)

        index = self._output_index()

        if test:
            self._begin_playback("test", "测试音", test=True, duration=1.0)
            threading.Thread(
                target=self.audio.play_test_tone, args=(index,),
                name="doorbell-testtone", daemon=True,
            ).start()
            return {"ok": True}

        if file:
            path = self._resolve_template_file(file)
            if not path:
                raise ApiError("找不到这个模板文件", 404)
            duration = wav_duration(path)
            self._begin_playback("file", os.path.basename(path), test=False, duration=duration)
            threading.Thread(
                target=self.audio.play, args=(path, 1.0, index),
                name="doorbell-play-file", daemon=True,
            ).start()
            return {"ok": True}

        cls = cls if cls in R.EVENT_CLASSES else "doorbell"
        label = R.EVENT_CLASSES[cls]["label"]
        original = os.path.join(R.ORIGINAL_AUDIO, f"{cls}.wav")
        path = original if os.path.exists(original) else os.path.join(
            R.BUILTIN_AUDIO, f"{cls}.wav"
        )
        if not os.path.exists(path):
            raise ApiError("缺少试听音频文件", 404)

        duration = wav_duration(path)
        active = self._active_intervals(path)
        self._begin_playback(cls, label, test=False, duration=duration, active=active)
        threading.Thread(
            target=self.audio.play,
            args=(path, float(R.PLAY_GAIN.get(cls, 1.0)), index),
            name="doorbell-play", daemon=True,
        ).start()
        timer = threading.Timer(0.35, self._record_demo, args=(cls,))
        timer.daemon = True
        timer.start()
        return {"ok": True}

    def _begin_playback(self, cls: str, label: str, *, test: bool, duration: float,
                        active: list[list[int]] | None = None) -> None:
        now = time.time()
        until = int((now + duration + 0.2) * 1000)
        self._playing_until = now + duration
        self._suppress_until = max(self._suppress_until, now + duration + 0.5)
        self.bus.set_playing({
            "cls": cls,
            "label": label,
            "test": bool(test),
            "until": until,
            "start": int(now * 1000),
            "active": active or [],
        })

    def _record_demo(self, cls: str) -> None:
        try:
            path = os.path.join(R.BUILTIN_AUDIO, f"{cls}.wav")
            if not os.path.exists(path):
                path = os.path.join(R.ORIGINAL_AUDIO, f"{cls}.wav")
            if not os.path.exists(path):
                return
            audio, rate = R.load_wav(path)
            audio = R.to_16k(audio, rate)
            detector = R.Detector(
                self.templates,
                threshold=float(self.config["threshold"]),
                gate_db=float(self.config["gate_db"]),
                cooldown=0.0,
                early=True,
                save_dir=None,
                strict=False,
            )
            block = int(R.SR * R.BLOCK_SEC)
            padded = np.concatenate([
                np.zeros(int(0.30 * R.SR), dtype=np.float32),
                audio.astype(np.float32),
                np.zeros(int(0.80 * R.SR), dtype=np.float32),
            ])
            event = None
            for start in range(0, len(padded) - block + 1, block):
                for candidate in detector.process(padded[start:start + block]):
                    if candidate.get("type") == "hit":
                        event = candidate
                        break
                if event is not None:
                    break

            if event is None:
                event = {
                    "type": "hit",
                    "cls": cls,
                    "label": R.EVENT_CLASSES[cls]["label"],
                    "tier": R.EVENT_CLASSES[cls]["tier"],
                    "conf": 0.9,
                    "score": 0.9,
                    "who": "demo",
                    "dur": len(audio) / float(R.SR),
                    "latency": 0.35,
                    "tonal": 0.0,
                    "mod": 0.0,
                    "decay": 0.0,
                    "stab": 0.0,
                    "freq_ratio": 1.0,
                    "conc_p90": 0.0,
                    "n_knock": 0,
                    "n_pulse": 0,
                    "sustain": 0.0,
                    "trace": [],
                    "wav": audio,
                }

            info = R.EVENT_CLASSES[cls]
            event["type"] = "hit"
            event["cls"] = cls
            event["label"] = info["label"]
            event["tier"] = info["tier"]
            # 试听是离线跑完整段音频，用固定值给页面一个可读的“延迟”。
            event["latency"] = 0.35
            self._record_event(event, detector, demo=True)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.log(f"试听命中生成失败：{exc}")

    def _resolve_template_file(self, file: str) -> str | None:
        name = os.path.basename(str(file or ""))
        if not name:
            return None
        for base in (R.SOUND_DIR, R.BUILTIN_AUDIO, R.CALIB_TPL, R.BUILTIN_TPL):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return path
        for path in R.learned_refs():
            if os.path.basename(path) == name:
                return path
        return None

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            old = dict(self.config)
            new = config_mod.apply_patch(self.config, patch)
            config_mod.save(new)
            self.config = new
            self.bus.set_config(self.public_config())

            live = []
            if self.bus.on_air and self._detector is not None:
                if new["threshold"] != old["threshold"]:
                    self._detector.threshold = float(new["threshold"])
                    live.append("阈值")
                if new["gate_db"] != old["gate_db"]:
                    self._detector.gate_db = float(new["gate_db"])
                    live.append("门控")

            if new["mic_index"] != old["mic_index"]:
                if self.bus.on_air:
                    self.log("麦克风已更换：将在下一次开始监听时生效")
                else:
                    self.log("麦克风已更换：将在下一次开始监听时生效")
            if new["speaker_index"] != old["speaker_index"]:
                self.log("播放设备已更换：将在下一次播放时生效")

            if new["lan"] != old["lan"]:
                self.net["lan_enabled"] = bool(new["lan"])
                self.bus.set_net(self.net)
                self.log("局域网访问已开启" if new["lan"] else "局域网访问已关闭（仅本机可访问）")

            old_has = config_mod.password_set(old)
            new_has = config_mod.password_set(new)
            if old_has != new_has or new.get("password_hash") != old.get("password_hash"):
                self.log("访问口令已更新" if new_has else "访问口令已清除")

            suffix = f"（{ '、'.join(live) } 已即时生效）" if live else ""
            self.log(f"设置已保存{suffix}")
            return {"ok": True, "config": self.public_config()}

    def update_iot(self, patch: dict) -> dict:
        with self._lock:
            mapped: dict = {}
            if "address" in patch:
                mapped["host"] = str(patch["address"])
            if "port" in patch:
                mapped["port"] = patch["port"]
            if "iot_id" in patch:
                mapped["iot_id"] = str(patch["iot_id"])
            if "topic_event" in patch:
                mapped["event_topic"] = str(patch["topic_event"])
            if "topic_state" in patch:
                mapped["status_topic"] = str(patch["topic_state"])
            if "enabled" in patch:
                mapped["enabled"] = bool(patch["enabled"])
            if "qos" in patch:
                mapped["qos"] = patch["qos"]
            if "report_ignore" in patch:
                mapped["publish_ignored"] = bool(patch["report_ignore"])
            if patch.get("password"):
                mapped["iot_pwd"] = str(patch["password"])

            try:
                settings = apply_update(self.iot_settings, mapped)
            except IotConfigError as exc:
                raise ApiError(str(exc)) from None

            iot_save(settings)
            self.iot_settings = settings
            self.iot_link.apply(settings)
            self._refresh_iot()
            self.log("上报设置已保存")
            return {"ok": True}

    def test_iot(self) -> dict:
        result = self.iot_link.test()
        ok = bool(result.get("ok"))
        return {"ok": ok, "error": "" if ok else str(result.get("reason") or "未知原因")}

    # ------------------------------------------------------------------
    # 模板 / 录音
    # ------------------------------------------------------------------

    def refresh_templates_api(self) -> dict:
        self.refresh_templates()
        return {"ok": True}

    def clear_recordings(self) -> dict:
        removed = self.bus.clear_recordings()
        self.log(f"已清空事件录音（{removed} 个文件）")
        return {"ok": True}

    # ------------------------------------------------------------------
    # 口令
    # ------------------------------------------------------------------

    def authorize(self, password: str) -> str:
        if not config_mod.password_set(self.config):
            return ""
        if not config_mod.verify(self.config, password):
            raise ApiError("口令不正确", 401)
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._tokens.add(token)
        return token

    def authorized(self, token: str | None) -> bool:
        if not config_mod.password_set(self.config):
            return True
        with self._lock:
            return bool(token and token in self._tokens)
