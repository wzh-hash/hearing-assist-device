"""生成板子用的 4 个提示音（16 kHz / 16-bit / 单声道）。

MicroPython 层没有音调 API，只有 `speaker.play_sys_music(文件名)` 回放文件，
所以 IOT-6.1–6.4 的"短鸣 / 两声 / 长鸣 / 连续鸣"由这 4 个预置 WAV 承担（D-20）。

    python -X utf8 tools/make_board_sounds.py

输出到 `board/sounds/`；上传时把 4 个 wav 放到板子根目录（与 main.py 同级）。
"""

import math
import struct
import wave
from pathlib import Path

RATE = 16000
OUT_DIR = Path(__file__).resolve().parent.parent / "board" / "sounds"


def tone(freq, ms, volume=0.5):
    count = int(RATE * ms / 1000)
    fade = max(1, int(RATE * 0.005))
    samples = []
    for index in range(count):
        env = min(1.0, index / fade, (count - 1 - index) / fade)
        samples.append(int(32767 * volume * env * math.sin(2 * math.pi * freq * index / RATE)))
    return samples


def silence(ms):
    return [0] * int(RATE * ms / 1000)


def write_wav(name, samples):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    print(f"{path}  {len(samples) / RATE:.2f}s  {path.stat().st_size} bytes")


def main():
    write_wav("doorbell.wav", tone(880, 180))                       # 短鸣
    write_wav("knock.wav", tone(660, 90) + silence(90) + tone(660, 90))   # 两声
    write_wav("kettle.wav", tone(1000, 700))                       # 长鸣
    write_wav(
        "fire.wav",
        tone(1400, 120) + silence(80) + tone(1400, 120) + silence(80) + tone(1400, 120),
    )                                                              # 连续鸣
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
