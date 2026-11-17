#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""听障人士感知器 · 守护进程（无需管理员权限）。

登录时由“启动”文件夹或任务计划程序启动；每 10 秒探活一次网页服务，
连续 2 次失败就重新拉起 `pc_web.py`。这样即使主进程崩溃/被误关，
也会在约 20 秒内自动恢复。

看门狗自身很薄：只用标准库、只认本机 8765–8767 的 /api/health，
日志写 `sounds/logs/watchdog.log`。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "sounds" / "logs"
LOG_FILE = LOG_DIR / "watchdog.log"
PORTS = (8765, 8766, 8767)
CHECK_EVERY = 10
FAILS_BEFORE_RESTART = 2


def log(text: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {text}\n")
    except OSError:
        pass


def healthy() -> bool:
    for port in PORTS:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1.0
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("app") == "hassist-web" and not data.get("mock"):
            return True
    return False


def _pythonw() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def start_product() -> None:
    args = [_pythonw(), "pc_web.py", "--no-browser"]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            args,
            cwd=str(ROOT),
            close_fds=True,
            creationflags=flags,
        )
        log("已拉起 pc_web.py")
    except Exception as exc:  # noqa: BLE001
        log(f"拉起失败：{exc}")


def main() -> None:
    log("看门狗启动")
    # 给已有实例/开机瞬间一点时间，避免重复拉起
    time.sleep(3)
    fails = 0
    while True:
        if healthy():
            if fails:
                log("服务已恢复")
            fails = 0
        else:
            fails += 1
            if fails >= FAILS_BEFORE_RESTART:
                log(f"连续 {fails} 次探活失败，重新拉起")
                start_product()
                fails = 0
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
