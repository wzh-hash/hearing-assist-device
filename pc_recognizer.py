#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门铃识别（单场景 demo，纯识别版，不含网络 / MQTT）

做什么：
    电脑麦克风一直听 → 能量门控发现“像事件”的声音 → 截一段 → 和参考门铃做
    NCC（归一化互相关）匹配 → 超过阈值就打印一行“命中”。

只做一件事：判断“刚才那个声音是不是门铃”。

常用命令（在本目录下执行）：
    python pc_recognizer.py devices         # 列出麦克风
    python pc_recognizer.py selftest        # 离线自测，不用麦克风、不出声
    python pc_recognizer.py record-ref      # 录一段“本房间里的门铃”当模板（强烈建议）
    python pc_recognizer.py calibrate       # 测底噪、测门铃得分，给出建议阈值
    python pc_recognizer.py listen          # 正式监听（默认只打印命中）
    python pc_recognizer.py listen --quiet  # 连启动信息都不打，只输出命中
    python pc_recognizer.py play            # 播放样本门铃（会出声，仅测试用）

为什么模板要自己录：
    干净音频文件经过手机喇叭 + 房间 + 电脑麦克风后频谱形状会变，
    直接比对得分只有 0.5~0.7；在同一环境录一遍再比对，实测可到 0.9 以上。
"""
from __future__ import annotations

import argparse
import glob
import os
import queue
import sys
import time
import wave

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(BASE, "lib")
if os.path.isdir(LIB) and LIB not in sys.path:   # 免 pip 安装的依赖目录
    sys.path.insert(0, LIB)

# ----------------------------- 参数 -----------------------------
SR = 16000                # 统一采样率
N_FFT = 512
HOP = 160                 # 10ms
WIN = 400                 # 25ms
N_MELS = 64
SMOOTH = 3                # 时频平滑窗
FMIN, FMAX = 60, 7000

GATE_DB = 8.0             # 门控：比自适应底噪高 8dB 认为“有事件”（噪声环境也能触发）
END_DB = 5.0              # 连续 END_BLOCKS 块低于 底噪+5dB 判定声音结束
END_BLOCKS = 2
EARLY_MIN = 0.5           # 提前判定：听到多少秒后开始“边听边判”
EARLY_HOP = 0.15          # 提前判定：间隔多少秒判一次
TEMPLATE_MIN = 0.8        # 模板至少保留多长
TEMPLATE_MAX = 1.6        # 模板最多保留多长（取能量最强的一段）
MAX_CAPTURE = 1.8         # 一段声音最长截取 1.8s（短一点，响应更快）
COOLDOWN = 3.0            # 两次判定最小间隔
DEFAULT_THRESHOLD = 0.60  # NCC 阈值（学习后由 config 覆盖）
TONAL_MIN = 300.0         # 调性门槛：门铃 >1000，噪声/环境响动 <30

SOUND_DIR = os.path.join(BASE, "sounds")
SOUND_FILE = os.path.join(SOUND_DIR, "doorbell_classic.wav")
ROOM_REF = os.path.join(SOUND_DIR, "room_ref.wav")


# ----------------------------- 音频读写 -----------------------------
def load_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: 需要 16bit PCM wav")
        ch, sr, n = w.getnchannels(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def save_wav(path: str, x: np.ndarray, sr: int = SR) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def to_16k(x: np.ndarray, sr: int) -> np.ndarray:
    """降到 16kHz。48k 用加窗 sinc 低通，其它采样率用线性插值。"""
    if sr == SR:
        return x
    if sr % SR == 0:
        q = sr // SR
        n = np.arange(-30, 31)
        fc = 0.45 / q
        h = np.sinc(2 * fc * n) * np.hamming(len(n))
        h /= h.sum()
        y = np.convolve(x, h, mode="same")
        return y[::q].astype(np.float32)
    t_new = np.arange(0, len(x) / sr, 1.0 / SR)
    t_old = np.arange(len(x)) / sr
    return np.interp(t_new, t_old, x).astype(np.float32)


# ----------------------------- 特征 -----------------------------
def frame_signal(x: np.ndarray, win: int = WIN, hop: int = HOP) -> np.ndarray:
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def _hz2mel(f: float) -> float:
    return 2595.0 * np.log10(1.0 + f / 700.0)


def _mel2hz(m: float) -> float:
    return 700.0 * (10 ** (m / 2595.0) - 1.0)


def _mel_filterbank() -> np.ndarray:
    mels = np.linspace(_hz2mel(FMIN), _hz2mel(FMAX), N_MELS + 2)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SR)
    hz = _mel2hz(mels)
    fb = np.zeros((N_MELS, len(freqs)), dtype=np.float32)
    for i in range(N_MELS):
        lo, mid, hi = hz[i], hz[i + 1], hz[i + 2]
        left = (freqs - lo) / max(mid - lo, 1e-6)
        right = (hi - freqs) / max(hi - mid, 1e-6)
        fb[i] = np.clip(np.minimum(left, right), 0, None)
    return fb


MEL_FB = _mel_filterbank()


def logmel(x: np.ndarray) -> np.ndarray:
    """波形 -> (帧数, 64) 对数梅尔谱（dB）。"""
    frames = frame_signal(x)
    spec = np.abs(np.fft.rfft(frames * np.hamming(WIN), n=N_FFT)) ** 2
    return 10 * np.log10(spec @ MEL_FB.T + 1e-10)


def smooth2d(m: np.ndarray, k: int = SMOOTH) -> np.ndarray:
    ker = np.ones(k) / k
    m = np.apply_along_axis(lambda r: np.convolve(r, ker, mode="same"), 0, m)
    m = np.apply_along_axis(lambda r: np.convolve(r, ker, mode="same"), 1, m)
    return m


def feat(x: np.ndarray) -> np.ndarray:
    """波形 -> 匹配特征：64 带对数梅尔 + 平滑 + 逐频带标准化。

    逐频带标准化能抵消喇叭/麦克风频响差异，只保留“随时间的纹理”，
    实测比直接用 log-mel 稳定得多。
    """
    m = smooth2d(logmel(x))
    return (m - m.mean(axis=0, keepdims=True)) / (m.std(axis=0, keepdims=True) + 1e-6)


def tonal_score(x: np.ndarray) -> float:
    """谱峰锐度（调性强度）。

    门铃是清晰音高（叮—咚），中频段每帧都有尖锐谱峰：实测干净门铃 7000+，
    即使 0dB 信噪比的噪声里也有 3000 左右；而白噪声/粉红噪声只有 4~25。
    用它可以把“门铃”和“环境响动”区分开，专门防误报。
    """
    if len(x) < WIN * 3:
        return 0.0
    mel = logmel(x)[:, 10:48]          # 约 250Hz~5kHz
    lin = 10 ** (mel / 10)
    med = np.median(lin, axis=1) + 1e-12
    return float(np.mean(np.max(lin, axis=1) / med))


def frame_db(x: np.ndarray, win: int = WIN, hop: int = HOP) -> np.ndarray:
    frames = frame_signal(x, win, hop)
    return 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-10)


def trim_active(x: np.ndarray, rel_db: float = 25.0, margin: float = 0.05,
                min_dur: float = 0.6) -> np.ndarray:
    """按能量裁掉首尾静音；至少保留 min_dur 秒，避免把声音裁没。"""
    if len(x) < WIN * 2:
        return x
    db = frame_db(x)
    peak = db.max()
    floor = np.percentile(db, 30)
    th = max(peak - rel_db, floor + 10.0)
    idx = np.where(db > th)[0]
    if len(idx) == 0:
        return x
    start = max(0, int(idx[0] * HOP - margin * SR))
    end = min(len(x), int(idx[-1] * HOP + WIN + margin * SR))
    need = int(min_dur * SR)
    if end - start < need:
        k = int(np.argmax(db)) * HOP + WIN // 2
        lo = max(0, min(k - need // 2, len(x) - need))
        return x[lo:lo + need]
    return x[start:end]


# ----------------------------- 匹配 -----------------------------
def ncc(query: np.ndarray, tpl: np.ndarray) -> float:
    """归一化互相关：模板时频图在 query 里滑动，返回最高相关系数（约 -1~1）。

    无关声音（噪声、说话、纯音）相关系数接近 0，同一声音通常 > 0.6。
    """
    q, t = query, tpl
    if len(q) < len(t):
        q, t = t, q
    m, f = t.shape
    if len(q) < m or m < 3:
        return 0.0
    tc = t - t.mean()
    tn = np.linalg.norm(tc)
    if tn < 1e-6:
        return 0.0
    tc = tc / tn
    patches = np.lib.stride_tricks.sliding_window_view(q, (m, f))[:, 0, :]
    pc = patches - patches.mean(axis=(1, 2), keepdims=True)
    den = np.linalg.norm(pc.reshape(len(pc), -1), axis=1) + 1e-9
    return float(np.max(np.einsum("ijk,jk->i", pc, tc) / den))


def crop_strongest(x: np.ndarray, max_sec: float = TEMPLATE_MAX, step_sec: float = 0.05) -> np.ndarray:
    """只保留能量最强的一段（模板不要带太多静音）。"""
    m = int(max_sec * SR)
    if len(x) <= m:
        return x
    step = max(1, int(step_sec * SR))
    best_i, best_e = 0, -1.0
    for i in range(0, len(x) - m + 1, step):
        e = float(np.mean(x[i:i + m].astype(np.float64) ** 2))
        if e > best_e:
            best_i, best_e = i, e
    return x[best_i:best_i + m]


def stretch_signal(x: np.ndarray, factor: float) -> np.ndarray:
    """时间伸缩（factor>1 变慢更长），用来生成容错模板。"""
    if abs(factor - 1.0) < 1e-3:
        return x
    n = int(len(x) / factor)
    t_new = np.linspace(0, len(x) - 1, max(n, 2))
    return np.interp(t_new, np.arange(len(x)), x).astype(np.float32)


class Template:
    def __init__(self, path: str, stretch: float = 1.0):
        self.path = path
        self.stretch = stretch
        x, sr = load_wav(path)
        x = trim_active(to_16k(x, sr), rel_db=15.0, margin=0.08, min_dur=TEMPLATE_MIN)
        x = crop_strongest(x)
        self.tonal = tonal_score(x)          # 模板本身的调性，低于门槛说明不是门铃
        x = stretch_signal(x, stretch)
        self.mat = feat(x)
        self.dur = len(x) / SR

    def score(self, query_feat: np.ndarray) -> float:
        return float(np.clip(ncc(query_feat, self.mat), 0.0, 1.0))


def build_templates(patterns: list[str], augment: bool = True) -> list[Template]:
    """加载模板。augment=True 时每个模板额外生成 ±5% 时间伸缩的变体，容错更好。

    会自动跳过“不像门铃”的模板（调性太低，多半是环境响动），
    这样即使学习时误录进了噪声，也不会拿它去误报。
    """
    files: list[str] = []
    for p in patterns:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, "*.wav")))
        else:
            files += sorted(glob.glob(p))
    if not files:
        raise SystemExit(f"找不到参考音频：{patterns}")
    tpls: list[Template] = []
    for f in files:
        t0 = Template(f)
        if t0.tonal < TONAL_MIN:            # 不像门铃（环境响动/语音），跳过
            continue
        tpls.append(t0)
        if augment:
            tpls.append(Template(f, 0.95))
            tpls.append(Template(f, 1.05))
    if not tpls:
        raise SystemExit("参考音里没有像门铃的声音（调性太低），请重新学习")
    return tpls


def template_scores(x: np.ndarray, templates: list[Template]) -> list[tuple[str, float]]:
    """返回每个模板的得分（用于演示面板的可视化）。"""
    if len(x) < int(0.25 * SR):
        return [(os.path.basename(t.path), 0.0) for t in templates]
    q = feat(x)
    out = []
    seen = {}
    for t in templates:
        name = os.path.basename(t.path)
        s = t.score(q)
        out.append((name, s))
        seen[name] = max(seen.get(name, 0.0), s)
    # 同名模板（变体）合并成最高分，展示更清楚
    merged = []
    for name in dict.fromkeys(n for n, _ in out):
        merged.append((name, seen[name]))
    return merged


def analyze(x: np.ndarray, templates: list[Template]) -> tuple[float, str]:
    """对一段声音打分，返回 (最高分, 命中的参考文件)。

    不裁剪查询段：NCC 会在整段里滑动找模板，保留整段更稳。
    """
    if len(x) < int(0.35 * SR):
        return 0.0, ""
    q = feat(x)
    if len(q) < 3:
        return 0.0, ""
    best, who = 0.0, ""
    for t in templates:
        s = t.score(q)
        if s > best:
            best, who = s, os.path.basename(t.path)
    return best, who


class Detector:
    """流式检测状态机（CLI 和 GUI 共用）。

    比旧逻辑快的原因：声音进来 EARLY_MIN 秒后，每 EARLY_HOP 秒就“边听边判”，
    一旦够分立刻命中，不再等整段门铃响完。
    """

    def __init__(self, templates: list[Template], threshold: float = DEFAULT_THRESHOLD,
                 gate_db: float = GATE_DB, cooldown: float = COOLDOWN, early: bool = True):
        self.templates = templates
        self.threshold = threshold
        self.gate_db = gate_db
        self.cooldown = cooldown
        self.early = early
        self.reset()

    def reset(self):
        self.noise: list[float] = []
        self.floor = -60.0
        self.state = "idle"
        self.buf: list[np.ndarray] = []
        self.cap_quiet = 0
        self.last_hit = 0.0
        self.t_onset = 0.0
        self.last_try_dur = 0.0
        self.level = -60.0
        self.last_capture: np.ndarray | None = None
        self.best_score = 0.0            # 本次事件里出现过的最好得分
        self.best_who = ""
        self.best_wav: np.ndarray | None = None
        self.best_dur = 0.0
        self.trace: list[tuple[float, float]] = []   # [(已听时长, 得分), ...]
        self.peak_db = -120.0            # 本次事件到目前为止的最大电平
        self.allow_restart = True        # 采集中遇到明显更响的新起音，允许重开

    def _emit(self, score: float, who: str, dur: float, wav: np.ndarray | None = None) -> dict:
        tonal = tonal_score(wav) if wav is not None else 0.0
        is_hit = score >= self.threshold and tonal >= TONAL_MIN
        ev = {
            "type": "hit" if is_hit else "ignore",
            "score": float(score),
            "who": who,
            "dur": dur,
            "latency": time.time() - self.t_onset,
            "tonal": float(tonal),
            "trace": list(self.trace),
            "wav": wav,
        }
        if wav is not None:
            self.last_capture = wav
        if ev["type"] == "hit":
            self.last_hit = time.time()
        self.state, self.buf = "idle", []
        return ev

    def process(self, block: np.ndarray) -> list[dict]:
        events: list[dict] = []
        db = _db(block)
        self.level = db
        now = time.time()

        if self.state == "idle":
            if db > -85:                       # 跳过纯数字静音块
                self.noise.append(db)
            if len(self.noise) > 100:
                self.noise.pop(0)
            if len(self.noise) < 10:           # 启动后先学约 1 秒底噪，避免一上来就触发
                self.floor = float(np.mean(self.noise)) if self.noise else -60.0
                self.floor = max(-70.0, min(-25.0, self.floor))
                return events
            self.floor = float(np.percentile(self.noise, 30))
            self.floor = max(-70.0, min(-25.0, self.floor))
            if (db > self.floor + self.gate_db and db > -70
                    and now - self.last_hit > self.cooldown):
                self.state, self.buf, self.cap_quiet = "capture", [block], 0
                self.t_onset, self.last_try_dur = now, 0.0
                self.best_score, self.best_who = 0.0, ""
                self.best_wav, self.best_dur = None, 0.0
                self.trace = []
                self.peak_db = db
                events.append({"type": "trigger", "level": db, "floor": self.floor})
            return events

        # --- capture ---
        # 如果采集中突然出现一个明显更响的新起音（例如噪声先触发、随后门铃响起），
        # 丢掉前面的采集，从新起音重新开始，避免真正的门铃被“噪声包”淹没。
        dur_so_far = len(self.buf) * 0.1
        if (self.allow_restart and dur_so_far >= 0.4 and db > self.peak_db + 10.0):
            self.buf, self.cap_quiet = [block], 0
            self.best_score, self.best_who = 0.0, ""
            self.best_wav, self.best_dur = None, 0.0
            self.trace = []
            self.peak_db = db
            self.t_onset = now
            return events

        self.peak_db = max(self.peak_db, db)
        self.buf.append(block)
        self.cap_quiet = 0 if db >= self.floor + END_DB else self.cap_quiet + 1
        dur = len(self.buf) * 0.1

        if self.early and dur >= EARLY_MIN and dur - self.last_try_dur >= EARLY_HOP:
            self.last_try_dur = dur
            x = np.concatenate(self.buf)
            score, who = analyze(x, self.templates)
            self.trace.append((dur, score))
            if score > self.best_score:
                self.best_score, self.best_who = score, who
                self.best_wav, self.best_dur = x, dur
            # 够分还不够，还要“像门铃”（有清晰音高），否则继续听完再判
            if score >= self.threshold and tonal_score(x) >= TONAL_MIN:
                events.append(self._emit(score, who, dur, x))
                return events

        if self.cap_quiet >= END_BLOCKS or dur >= MAX_CAPTURE:
            x = np.concatenate(self.buf)
            score, who = analyze(x, self.templates)
            self.trace.append((dur, score))
            if score > self.best_score:
                self.best_score, self.best_who = score, who
                self.best_wav, self.best_dur = x, dur
            # 用本次事件内最好的那一次判定，减少“恰好这一版不好就漏报”
            if self.best_wav is not None:
                events.append(self._emit(self.best_score, self.best_who,
                                         self.best_dur, self.best_wav))
        return events


# ----------------------------- 录音 -----------------------------
class Recorder:
    """后台按 100ms 块采集，内部统一转 16kHz。"""

    def __init__(self, device: int | None = None):
        import sounddevice as sd

        self.sd = sd
        info = sd.query_devices(device, "input")
        self.fs = int(info["default_samplerate"] or 48000)
        self.q: queue.Queue[np.ndarray] = queue.Queue()
        self.stream = None
        for fs in (self.fs, 48000, 44100):
            try:
                self.stream = sd.InputStream(
                    samplerate=fs, channels=1, blocksize=int(fs * 0.1),
                    dtype="float32", device=device, callback=self._cb,
                )
                self.fs = fs
                break
            except Exception:  # noqa: BLE001
                continue
        if self.stream is None:
            raise SystemExit("打不开麦克风。先运行 devices 模式看设备，再用 --device 指定序号。")

    def _cb(self, indata, frames, t, status):  # noqa: ARG002
        self.q.put(indata[:, 0].copy())

    def start(self):
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()

    def read_block(self, timeout: float = 0.5) -> np.ndarray | None:
        try:
            raw = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        return to_16k(raw, self.fs)

    def drain(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break


def _db(x: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(x.astype(np.float64) ** 2) + 1e-12))


# ----------------------------- 学习门铃（一键配置） -----------------------------
LEARN_MIN_DUR = 0.35      # 裁剪后一段门铃至少多长
LEARN_MAX_DUR = 2.40      # 从触发起固定录这么久，再裁掉尾部余音
LEARN_MIN_AUDIO = 1.50    # 至少录这么久（避免“叮—咚”中间停顿被当结束）
LEARN_GATE_DB = 12.0      # 学习时门控（比底噪高 12dB 才算一次声响，减少环境误触发）
LEARN_PEAK_MIN = -40.0    # 峰值低于这个算“太小声”，要求重录
LEARN_QUIET_BLOCKS = 4    # 连续 0.4s 静音可以提前结束（但不少于 LEARN_MIN_AUDIO）
LEARN_TIMEOUT = 15.0      # 每次等待用户按门铃的最长秒数
LEARN_TAKES = 2           # 默认学 2 次
LEARN_MAX_ATTEMPTS = 8    # 最多尝试 8 次，尽量不因为一次录差就失败


def capture_event(rec: Recorder, gate_db: float = LEARN_GATE_DB,
                  timeout: float = LEARN_TIMEOUT,
                  max_dur: float = LEARN_MAX_DUR) -> np.ndarray | None:
    """等待并录下一次声音事件。

    触发后固定录 max_dur 秒（带 0.3s 预录），不够 LEARN_MIN_AUDIO 不会提前停，
    这样每次采到的长度一致，学习结果才稳定。
    """
    level_hist: list[float] = []
    pre_blocks: list[np.ndarray] = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        b = rec.read_block(timeout=0.3)
        if b is None:
            continue
        db = _db(b)
        level_hist.append(db)
        if len(level_hist) > 20:
            level_hist.pop(0)
        pre_blocks.append(b)
        if len(pre_blocks) > 3:                 # 保留 0.3s 预录
            pre_blocks.pop(0)
        floor = float(np.median(level_hist)) if len(level_hist) >= 8 else db
        if db < max(floor + gate_db, -70.0):
            continue
        buf = list(pre_blocks)
        quiet = 0
        while True:
            bb = rec.read_block(timeout=0.3)
            if bb is None:
                continue
            buf.append(bb)
            quiet = 0 if _db(bb) > floor + END_DB else quiet + 1
            dur = len(buf) * 0.1
            if dur >= max_dur:
                break
            if quiet >= LEARN_QUIET_BLOCKS and dur >= LEARN_MIN_AUDIO:
                break
        return np.concatenate(buf)
    return None


def learn_doorbell(device: int | None = None, takes: int = LEARN_TAKES,
                   progress=None, recorder_factory=None) -> dict:
    """一键学习门铃：录 N 次 → 物理质量检查 → 保存模板 → 设定安全阈值。

    原则：只因为“明显不对”（没听到/太小声/太短）才让用户重来；
    “几次录音不像”只作为提示，不作为失败条件——否则环境噪声很容易让人反复失败。
    """
    def report(kind: str, **kw):
        if progress:
            progress(kind, kw)

    rec = recorder_factory() if recorder_factory else Recorder(device)
    rec.start()
    time.sleep(0.6)
    rec.drain()

    saved: list[str] = []
    raws: list[np.ndarray] = []
    attempts = 0
    try:
        while len(saved) < takes and attempts < LEARN_MAX_ATTEMPTS:
            attempts += 1
            report("ask", index=len(saved) + 1, takes=takes, attempt=attempts)
            x = capture_event(rec)
            if x is None:
                report("warn", text="没听到声音，再按一次（确认手机在放门铃 / 门铃在响）")
                continue

            peak = 20 * np.log10(np.max(np.abs(x)) + 1e-9)
            if peak < LEARN_PEAK_MIN:
                report("warn", text=f"声音太小（{peak:.0f}dBFS），手机靠近一点再按")
                continue
            if peak > -0.5:
                report("warn", text=f"声音偏响（{peak:.0f}dBFS），建议手机稍远一点；这次先用着")

            seg = trim_active(x, rel_db=15.0, margin=0.08, min_dur=TEMPLATE_MIN)
            seg = crop_strongest(seg)
            seg_dur = len(seg) / SR
            if seg_dur < LEARN_MIN_DUR:
                report("warn", text="这一下太短了，等提示后再完整按一次门铃")
                continue
            tonal = tonal_score(seg)
            if tonal < TONAL_MIN:
                report("warn", text="这次不像门铃声（更像环境响动），请在安静一点时再按一次")
                continue

            # 相似度只做提示，不挡学习
            if raws:
                sims = [Template(saved[j]).score(feat(seg)) for j in range(len(saved))]
                if max(sims) < 0.20:
                    report("warn", text="这次和上几次差得比较多，可能混进了别的响动（已先记下）")

            path = os.path.join(SOUND_DIR, f"_learn_tmp_{attempts}.wav")
            save_wav(path, seg)
            saved.append(path)
            raws.append(load_wav(path)[0])
            report("saved", index=len(saved), path=path, dur=seg_dur, peak=peak)
    finally:
        rec.stop()

    def _cleanup(n: int):
        for p in saved[n:]:
            try:
                os.remove(p)
            except OSError:
                pass
        for extra in glob.glob(os.path.join(SOUND_DIR, "_learn_tmp_*.wav")):
            if extra not in saved[:n]:
                try:
                    os.remove(extra)
                except OSError:
                    pass

    if not saved:
        _cleanup(0)
        return {"ok": False,
                "reason": "没有录到门铃。确认声音在响、手机音量够大，再点一次学习",
                "takes": []}

    # 一致性只用于提示和阈值微调
    consistency = 1.0
    pair_table = []
    if len(raws) >= 2:
        vals = []
        for i in range(len(raws)):
            for j in range(len(raws)):
                if i == j:
                    continue
                s = Template(saved[j]).score(feat(raws[i]))
                pair_table.append((i + 1, j + 1, s))
                vals.append(s)
        consistency = min(vals)
    report("verified", worst=consistency, pairs=pair_table)

    # 阈值固定在安全区间；一致性高时稍微收紧，避免误报
    if len(saved) >= 2 and consistency >= 0.55:
        threshold = 0.62
    else:
        threshold = 0.60

    # 替换旧的模板
    for old in glob.glob(os.path.join(SOUND_DIR, "learned_*.wav")):
        try:
            os.remove(old)
        except OSError:
            pass
    final = []
    for i, p in enumerate(saved):
        dst = os.path.join(SOUND_DIR, f"learned_{i+1}.wav")
        os.replace(p, dst)
        final.append(dst)
    _cleanup(len(saved))
    save_config({"threshold": threshold})

    warning = ""
    if len(saved) == 1:
        warning = "只录到 1 次，建议之后有空再学一次，识别会更稳"
    elif consistency < 0.35:
        warning = f"几次录音差异偏大（{consistency:.2f}）；若后续漏报，重新学习一次即可"
    return {"ok": True, "takes": final, "worst": consistency,
            "threshold": threshold, "warning": warning}


def learned_refs() -> list[str]:
    """当前可用的学习结果（优先 learned_*.wav，退回 room_ref*.wav）。"""
    files = sorted(glob.glob(os.path.join(SOUND_DIR, "learned_*.wav")))
    if files:
        return files
    return sorted(glob.glob(os.path.join(SOUND_DIR, "room_ref*.wav")))


CONFIG_PATH = os.path.join(SOUND_DIR, "config.json")


def save_config(data: dict) -> None:
    try:
        import json
        os.makedirs(SOUND_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_config() -> dict:
    try:
        import json
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ----------------------------- 各模式 -----------------------------
def cmd_devices(_args):
    import sounddevice as sd

    print("输入设备（麦克风）：")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i:2d}] {d['name']}  in={d['max_input_channels']} sr={d['default_samplerate']}")
    print("\n默认输入：", sd.query_devices(kind="input")["name"])
    try:
        print("默认输出：", sd.query_devices(kind="output")["name"])
    except Exception:  # noqa: BLE001
        pass


def cmd_selftest(args):
    tpls = build_templates(args.ref)
    print("参考模板：")
    for t in tpls:
        print(f"  {os.path.basename(t.path):26s} {t.dur:5.2f}s  {t.mat.shape}")

    print("\n== 正样本（应命中）==")
    pos = []
    for t in tpls:
        x, sr = load_wav(t.path)
        s, who = analyze(to_16k(x, sr), tpls)
        pos.append(s)
        print(f"  [{'OK ' if s >= args.threshold else 'LOW'}] {os.path.basename(t.path):26s} score={s:.3f}  (best={who})")

    print("\n== 负样本（不应命中）==")
    dur = int(SR * 1.6)
    t = np.arange(dur) / SR
    rng = np.random.default_rng(7)
    negs = {
        "白噪声": rng.normal(0, 0.08, dur),
        "粉红噪声": np.cumsum(rng.normal(0, 0.01, dur)),
        "1kHz 纯音": 0.25 * np.sin(2 * np.pi * 1000 * t),
        "双音和弦": 0.2 * np.sin(2 * np.pi * 440 * t) + 0.2 * np.sin(2 * np.pi * 880 * t),
        "扫频 200-4kHz": 0.3 * np.sin(2 * np.pi * (200 * (20 ** (t / t[-1]))) * t),
        "脉冲串 4Hz": 0.6 * (np.sin(2 * np.pi * 4 * t) > 0.95),
        "敲击/响指": np.convolve(rng.normal(0, 1, dur), np.exp(-np.arange(200) / 20.0), mode="same") * 0.3,
        "低频嗡声 120Hz": 0.3 * np.sin(2 * np.pi * 120 * t),
    }
    neg_scores = {}
    for k, v in negs.items():
        s, who = analyze(v.astype(np.float32), tpls)
        neg_scores[k] = s
        print(f"  [{'!!  ' if s >= args.threshold else 'OK '}] {k:14s} score={s:.3f}  (best={who})")

    lo, hi = min(pos), max(neg_scores.values())
    print(f"\n最小正样本 {lo:.3f} | 最大负样本 {hi:.3f} | 间隔 {lo - hi:+.3f}")
    print(f"结论：阈值 {'可用' if lo - hi > 0.05 else '偏紧'}；当前 {args.threshold:.2f}")


def cmd_record_ref(args):
    """录 1~N 段“这个房间里的门铃”当模板（建议 3 段，取最稳的匹配）。"""
    import sounddevice as sd

    takes = max(1, int(args.takes))
    saved = []
    print(f"=== 录制参考音（{takes} 段）===")
    if not args.play:
        print("每段开始前会提示，请用手机播放同一段门铃（位置和音量保持一致）")
    time.sleep(1.0)

    for k in range(takes):
        rec = Recorder(args.device)
        rec.start()
        if args.play:
            print(f"[{k+1}/{takes}] 2 秒后由电脑播放 {os.path.basename(args.sound)} …")
        else:
            print(f"[{k+1}/{takes}] 2 秒后请用手机播放门铃 …")
        time.sleep(2)
        rec.drain()
        if args.play:
            x, sr = load_wav(args.sound)
            sd.play(x, sr, device=args.out_dev)
        buf, t0 = [], time.time()
        while time.time() - t0 < args.duration:
            b = rec.read_block()
            if b is not None:
                buf.append(b)
        rec.stop()
        if args.play:
            sd.wait()
        y = np.concatenate(buf)

        win = int(2.5 * SR)
        if len(y) > win:
            step = int(0.05 * SR)
            best_i, best_e = 0, -1.0
            for i in range(0, len(y) - win, step):
                e = float(np.mean(y[i:i + win].astype(np.float64) ** 2))
                if e > best_e:
                    best_i, best_e = i, e
            y = y[best_i:best_i + win]
        seg = trim_active(y, rel_db=15.0, margin=0.08, min_dur=TEMPLATE_MIN)
        seg = crop_strongest(seg)
        if takes == 1:
            path = ROOM_REF
        else:
            path = os.path.join(SOUND_DIR, f"room_ref_{k+1}.wav")
        os.makedirs(SOUND_DIR, exist_ok=True)
        save_wav(path, seg)
        peak = 20 * np.log10(np.max(np.abs(seg)) + 1e-9)
        saved.append(path)
        print(f"    已保存 {os.path.basename(path)}（{len(seg)/SR:.2f}s peak={peak:.1f}dBFS）")
        if peak < -30:
            print("    警告：声音偏小，建议手机靠近电脑或调大音量")
        time.sleep(0.8)

    print("\n录制完成：", ", ".join(os.path.basename(p) for p in saved))
    if len(saved) > 1:
        tpls = [Template(p) for p in saved]
        print("各段之间的一致性（同一门铃应该互相高分）：")
        for i in range(len(saved)):
            x, _ = load_wav(saved[i])
            s, who = analyze(x, tpls)
            print(f"  {os.path.basename(saved[i]):18s} 与模板集最高分 {s:.3f} ({who})")
    print("之后 listen / calibrate / selftest 默认使用 sounds/room_ref*.wav 当模板。")


def cmd_calibrate(args):
    tpls = build_templates(args.ref)
    rec = Recorder(args.device)
    print("=== 校准 ===")
    print("模板：" + ", ".join(os.path.basename(t.path) for t in tpls))
    print(f"1) 请保持安静 {args.quiet} 秒（采集环境底噪）…")
    rec.start()
    noise_db = []
    t0 = time.time()
    while time.time() - t0 < args.cal_quiet:
        b = rec.read_block()
        if b is None:
            continue
        noise_db.append(_db(b))
        print(f"\r    剩余 {args.cal_quiet - (time.time() - t0):4.1f}s  当前 {noise_db[-1]:6.1f}dB", end="")
    rec.stop()
    floor = float(np.median(noise_db)) if noise_db else -60.0
    gate = floor + GATE_DB
    print(f"\n    底噪 ≈ {floor:.1f}dB → 门控阈值 {gate:.1f}dB")

    print(f"\n2) 请播放门铃（{args.cal_bell:.0f} 秒内）…")
    rec = Recorder(args.device)
    rec.start()
    best, who_best = 0.0, ""
    events: list[tuple[float, float]] = []      # (score, 时长)
    buf, t0, capturing = [], time.time(), False
    while time.time() - t0 < args.cal_bell + 3:
        b = rec.read_block()
        if b is None:
            continue
        buf.append(b)
        if len(buf) > 30:
            buf.pop(0)
        db = _db(b)
        if db > gate:
            capturing = True
        if capturing and db < gate - END_DB and len(buf) > 3:
            x = np.concatenate(buf)
            s, who = analyze(x, tpls)
            events.append((s, len(x) / SR))
            if s > best:
                best, who_best = s, who
            capturing, buf = False, []
    rec.stop()
    if events:
        print("    本次听到的事件：")
        for i, (s, dur) in enumerate(events, 1):
            print(f"      #{i}  {dur:.2f}s  score={s:.3f}")
    print(f"\n    门铃最高分 ≈ {best:.3f}  ({who_best})")
    if best >= 0.70:
        suggested = max(0.55, min(0.92, best - 0.10))
        print(f"建议阈值：--threshold {suggested:.2f}   （默认 {DEFAULT_THRESHOLD:.2f}）")
        print("确定后运行： run.bat listen --threshold " + f"{suggested:.2f}")
    else:
        print("未检测到门铃。请确认：手机在放 sounds\\doorbell_classic.wav、音量够大、"
              "手机靠近电脑，然后重跑 calibrate。")


def cmd_listen(args):
    tpls = build_templates(args.ref)
    quiet = args.quiet
    logf = None
    if args.log:
        logf = open(args.log, "a", encoding="utf-8")

    def emit(msg: str, force: bool = False):
        if force or not quiet:
            print(msg, flush=True)
        if logf is not None:
            logf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            logf.flush()

    if not quiet:
        names = ", ".join(os.path.basename(t.path) for t in tpls)
        emit(f"=== 门铃识别 ===")
        emit(f"模板 : {names}")
        emit(f"阈值 : {args.threshold:.2f}   冷却 : {args.cooldown:.1f}s")
        emit("监听中（Ctrl+C 退出，命中才会打印）…")

    rec = Recorder(args.device)
    rec.start()
    det = Detector(tpls, threshold=args.threshold, gate_db=args.gate_db,
                   cooldown=args.cooldown)
    try:
        while True:
            block = rec.read_block()
            if block is None:
                continue
            for ev in det.process(block):
                if ev["type"] == "trigger":
                    if args.verbose and not quiet:
                        emit(f"[{time.strftime('%H:%M:%S')}] 有声音"
                             f"（{ev['level']:.1f}dB / 底噪 {ev['floor']:.1f}dB），分析中…")
                elif ev["type"] == "hit":
                    emit(f"[{time.strftime('%H:%M:%S')}] 命中 门铃  score={ev['score']:.3f}"
                         f"  latency={ev['latency']*1000:.0f}ms")
                elif args.verbose and not quiet:
                    emit(f"[{time.strftime('%H:%M:%S')}] 忽略  score={ev['score']:.3f}"
                         f"  (best={ev['who']}, latency={ev['latency']*1000:.0f}ms)")
    except KeyboardInterrupt:
        if not quiet:
            print("\n退出。")
    finally:
        rec.stop()
        if logf is not None:
            logf.close()


def cmd_play(args):
    import sounddevice as sd

    x, sr = load_wav(args.sound)
    print(f"播放：{args.sound}（会出声）")
    sd.play(x, sr, device=args.out_dev)
    sd.wait()


def cmd_e2e(args):
    """端到端：自己放门铃、自己听（会出声）。"""
    import sounddevice as sd

    tpls = build_templates(args.ref)
    rec = Recorder(args.device)
    rec.start()
    print("=== 端到端自测（会出声）===")
    time.sleep(2)
    rec.drain()
    x, sr = load_wav(args.sound)
    sd.play(x, sr, device=args.out_dev)
    buf, t0 = [], time.time()
    while time.time() - t0 < 4.0:
        b = rec.read_block()
        if b is not None:
            buf.append(b)
    rec.stop()
    sd.wait()
    y = np.concatenate(buf)
    s, who = analyze(y, tpls)
    peak = 20 * np.log10(np.max(np.abs(y)) + 1e-9)
    print(f"录音 {len(y)/SR:.2f}s peak={peak:.1f}dBFS  得分 {s:.3f}（{who}）阈值 {args.threshold:.2f}"
          f" → {'识别成功' if s >= args.threshold else '未达到阈值'}")


# ----------------------------- 入口 -----------------------------
def cmd_learn(args):
    """一键学习门铃（推荐流程）：按 3 次门铃，自动验证并设好阈值。"""
    print(f"=== 学习门铃（{args.takes} 次）===")
    print("每次看到提示后，按一次门铃（没有门铃就让家人按/用手机放一小段）")

    def progress(kind, kw):
        if kind == "ask":
            print(f"\n[{kw['index']}/{kw['takes']}] 请按门铃（{LEARN_TIMEOUT:.0f} 秒内）…")
        elif kind == "warn":
            print(f"  ! {kw['text']}")
        elif kind == "saved":
            print(f"  已录到第 {kw['index']} 次：{kw['dur']:.2f}s  {kw['peak']:.0f}dBFS")
        elif kind == "verified":
            print(f"\n三次一致性：最低 {kw['worst']:.2f}（越高越可靠）")
            for i, j, s in kw["pairs"][:6]:
                print(f"  第{i}段 vs 第{j}段 = {s:.2f}")

    res = learn_doorbell(args.device, args.takes, progress)
    if not res["ok"]:
        print(f"\n学习没成功：{res['reason']}")
        print("再试一次：run.bat learn")
        return
    print(f"\n学习完成。阈值 {res['threshold']:.2f}（已保存）")
    if res.get("warning"):
        print(f"提示：{res['warning']}")
    print("现在运行：run.bat listen")


def main():
    ap = argparse.ArgumentParser(description="门铃识别（单场景，纯识别版）")
    ap.add_argument("mode", choices=["devices", "learn", "selftest", "record-ref", "calibrate",
                                     "listen", "play", "e2e"])
    ap.add_argument("--ref", nargs="+", default=None,
                    help="参考音频或目录（默认：有 learned_*/room_ref* 就用它们，否则用 ./sounds）")
    ap.add_argument("--sound", default=SOUND_FILE, help="播放/演示用的门铃音频")
    ap.add_argument("--device", type=int, default=None, help="输入设备序号（见 devices）")
    ap.add_argument("--out-dev", type=int, default=None, help="播放设备序号")
    ap.add_argument("--threshold", type=float, default=None,
                    help="判定阈值（默认用学习/校准保存的值，否则 0.60）")
    ap.add_argument("--gate-db", type=float, default=GATE_DB, help="门控高于底噪多少 dB")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN, help="两次判定最小间隔（秒）")
    ap.add_argument("--quiet", action="store_true", help="静默：只输出命中")
    ap.add_argument("--verbose", action="store_true", help="输出每个被分析的声音及其得分")
    ap.add_argument("--log", default=None, help="把输出同时写入日志文件")
    ap.add_argument("--play", action="store_true", help="record-ref：由电脑播放（默认等外部播放）")
    ap.add_argument("--takes", type=int, default=LEARN_TAKES, help="learn：录几次（默认 2）")
    ap.add_argument("--duration", type=float, default=4.0, help="record-ref：每段录制秒数")
    ap.add_argument("--cal-quiet", type=float, default=6.0, help="calibrate：安静采集底噪秒数")
    ap.add_argument("--cal-bell", type=float, default=8.0, help="calibrate：等待播放门铃的秒数")
    ap.add_argument("--with-clean", action="store_true",
                    help="把 sounds 里的干净门铃音频也加入模板")
    args = ap.parse_args()
    if args.ref is None:
        refs = learned_refs()
        args.ref = refs if refs else [SOUND_DIR]
    if args.with_clean and SOUND_FILE not in args.ref:
        args.ref = list(args.ref) + [SOUND_FILE]
    if args.threshold is None:
        args.threshold = float(load_config().get("threshold", DEFAULT_THRESHOLD))

    {"devices": cmd_devices, "learn": cmd_learn, "selftest": cmd_selftest,
     "record-ref": cmd_record_ref, "calibrate": cmd_calibrate,
     "listen": cmd_listen, "play": cmd_play, "e2e": cmd_e2e}[args.mode](args)


if __name__ == "__main__":
    main()

