"""服务端扬声器播放：试听、测试音、模板试听。

与桌面版相同的策略：
- 复用一条 OutputStream，避免每次点击都等设备初始化；
- 新播放打断旧播放；
- 自动限制增益避免削波；
- 播放只由服务端出声，浏览器不采集麦克风。
"""

from __future__ import annotations

import os
import threading
import time
import wave

import numpy as np

import pc_recognizer as R


def wav_duration(path: str) -> float:
    """读取 WAV 时长（秒）；失败返回 1 秒，保证抑制窗口总比播放长一点。"""
    try:
        with wave.open(path, "rb") as handle:
            rate = handle.getframerate() or 1
            return float(handle.getnframes()) / float(rate)
    except (OSError, EOFError, wave.Error):
        return 1.0


class AudioPlayer:
    def __init__(self, log):
        self._log = log
        self._play_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._stream = None
        self._sr: int | None = None
        self._idx: int | None = None
        self._channels: int | None = None

    # ------------------------------------------------------------------
    # 输出流
    # ------------------------------------------------------------------

    def _device_info(self, idx: int | None):
        import sounddevice as sd  # 延迟导入：没有音频设备时也能启动网页

        info = (
            sd.query_devices(idx, "output")
            if idx is not None
            else sd.query_devices(kind="output")
        )
        rate = int(info["default_samplerate"] or 48000)
        channels = 2 if int(info.get("max_output_channels", 1)) >= 2 else 1
        return rate, channels

    def _close_locked(self) -> None:
        stream = self._stream
        self._stream = None
        self._sr = None
        self._idx = None
        self._channels = None
        if stream is not None:
            for action in (stream.stop, stream.close):
                try:
                    action()
                except Exception:  # noqa: BLE001
                    pass

    def _ensure_stream(self, idx: int | None):
        import sounddevice as sd

        with self._stream_lock:
            rate, channels = self._device_info(idx)
            if (
                self._stream is not None
                and self._idx == idx
                and self._sr == rate
            ):
                return self._stream, rate, channels

            self._close_locked()
            started = time.perf_counter()
            self._stream = sd.OutputStream(
                samplerate=rate,
                channels=channels,
                dtype="float32",
                device=idx,
                latency="high",
            )
            self._sr = rate
            self._idx = idx
            self._channels = channels
            self._log(f"播放设备已就绪（{1000 * (time.perf_counter() - started):.0f} ms）")
            return self._stream, rate, channels

    def preopen(self, idx: int | None = None) -> None:
        try:
            self._ensure_stream(idx)
        except Exception as exc:  # noqa: BLE001
            self._log(f"预打开播放设备失败：{exc}")

    def close(self) -> None:
        with self._stream_lock:
            self._close_locked()

    # ------------------------------------------------------------------
    # 打断
    # ------------------------------------------------------------------

    def stop_current(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # 播放
    # ------------------------------------------------------------------

    def _write(self, stream, data: np.ndarray, channels: int, stop: threading.Event):
        shaped = (
            np.repeat(data.reshape(-1, 1), 2, axis=1)
            if channels == 2
            else data.reshape(-1, 1)
        )
        underflows = 0
        stream.start()
        try:
            block = 1024
            for start in range(0, len(shaped), block):
                if stop.is_set():
                    break
                if stream.write(shaped[start:start + block]):
                    underflows += 1
        finally:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001
                pass
        if underflows:
            self._log(f"播放缓冲不足 {underflows} 次（可能有轻微断续）")

    def play(self, path: str, gain: float = 1.0, idx: int | None = None) -> float:
        """阻塞播放；返回实际时长（秒）。被新播放打断时只播放到一半。"""
        self._stop_event.set()
        if not self._play_lock.acquire(timeout=3.0):
            self._log("上一个声音还在播放，稍后再点")
            return 0.0

        stop = threading.Event()
        self._stop_event = stop
        try:
            stream, rate, channels = self._ensure_stream(idx)
            if rate == 44100:
                alt = os.path.join(R.ORIGINAL_AUDIO_44K, os.path.basename(path))
                if os.path.exists(alt):
                    path = alt
            audio, used_gain, limited = R.prepare_playback(path, rate, gain)
            if limited:
                self._log(f"音量自动降到 {used_gain:.2f}（避免削波杂音）")
            self._write(stream, audio, channels, stop)
            return len(audio) / float(rate)
        except Exception as exc:  # noqa: BLE001
            self._log(f"播放失败：{exc}")
            return 0.0
        finally:
            try:
                self._play_lock.release()
            except RuntimeError:
                pass

    def play_test_tone(self, idx: int | None = None) -> float:
        """1 秒 1 kHz 纯音，用于排查播放链路。"""
        self._stop_event.set()
        if not self._play_lock.acquire(timeout=3.0):
            return 0.0

        stop = threading.Event()
        self._stop_event = stop
        try:
            stream, rate, channels = self._ensure_stream(idx)
            count = int(1.0 * rate)
            t = np.arange(count) / float(rate)
            audio = (0.25 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
            fade = int(0.02 * rate)
            if fade > 1:
                audio[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
                audio[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
            self._write(stream, audio, channels, stop)
            return count / float(rate)
        except Exception as exc:  # noqa: BLE001
            self._log(f"测试音播放失败：{exc}")
            return 0.0
        finally:
            try:
                self._play_lock.release()
            except RuntimeError:
                pass
