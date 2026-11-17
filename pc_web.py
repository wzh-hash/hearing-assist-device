#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""听障人士感知器 · 网页入口。

一个进程里同时跑：识别线程、学习线程、播放、IoT 上报、HTTP/SSE。
双击 `run_gui.bat` 就是启动它；经典桌面窗口由 `run_gui_classic.bat` 启动。
开机自启时用 `pythonw.exe` 隐藏运行，日志写入 `sounds/logs/product.log`。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for extra in (ROOT, ROOT / "src", ROOT / "lib"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from web.app import App                  # noqa: E402
from web.http_api import Handler, Server  # noqa: E402

DEFAULT_PORT = 8765
HEALTH_TIMEOUT = 0.5
LOG_DIR = ROOT / "sounds" / "logs"
LOG_FILE = LOG_DIR / "product.log"


def ensure_streams() -> None:
    """pythonw 隐藏运行时 stdout/stderr 为 None：把输出写到日志文件。

    日志超过 2 MB 时轮转一份（product.log.1），保留最近两代，避免长期运行撑爆磁盘。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
            backup = LOG_DIR / "product.log.1"
            try:
                backup.unlink()
            except OSError:
                pass
            LOG_FILE.rename(backup)
        handle = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def probe_existing(ports: tuple[int, ...]) -> int | None:
    """发现已有网页实例就返回端口；预览服务（mock=true）不算。"""
    for port in ports:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=HEALTH_TIMEOUT
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("app") == "doorbell-web" and not data.get("mock"):
            return port
    return None


def bind_server(host: str, port: int) -> Server:
    last_error: OSError | None = None
    for candidate in (port, port + 1, port + 2):
        try:
            return Server((host, candidate), Handler)
        except OSError as exc:
            last_error = exc
    raise SystemExit(
        f"端口 {port} 到 {port + 2} 都被占用，无法启动网页。\n"
        f"请关掉占用端口的程序，或用 --port 换一个端口。\n（{last_error}）"
    )


def main() -> None:
    ensure_streams()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动 听障人士感知器", flush=True)
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description="听障人士感知器 · 网页控制台")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="起始端口；被占用时自动 +1 / +2")
    parser.add_argument("--no-lan", action="store_true",
                        help="只绑定 127.0.0.1，关闭局域网访问")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动后不自动打开浏览器")
    args = parser.parse_args()

    existing = probe_existing((args.port, args.port + 1, args.port + 2))
    if existing is not None:
        url = f"http://127.0.0.1:{existing}/"
        print(f"已有实例在运行：{url}")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        return

    host = "127.0.0.1" if args.no_lan else "0.0.0.0"
    server = bind_server(host, args.port)

    app = App(no_lan=args.no_lan)
    server.app = app
    port = server.server_address[1]
    ip = lan_ip()
    local = f"http://127.0.0.1:{port}"
    lan = local if args.no_lan else f"http://{ip}:{port}"
    app.set_net(local, lan, port)
    app.start()

    serving = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="doorbell-http",
        daemon=True,
    )
    serving.start()

    if not args.no_browser:
        try:
            webbrowser.open(local)
        except Exception:  # noqa: BLE001
            pass

    print("")
    print("  听障人士感知器 · 网页控制台")
    print("  " + "-" * 52)
    print(f"  本机    {local}")
    if app.lan_enabled and not args.no_lan:
        if ip.startswith("127."):
            print("  局域网  未检测到内网地址（可能是防火墙或没连 Wi-Fi）")
        else:
            print(f"  局域网  {lan}")
    elif args.no_lan:
        print("  局域网  已关闭（--no-lan，仅本机）")
    else:
        print("  局域网  已关闭（可在设置里开启）")
    print(f"  模板    {len(app.template_names)} 个；阈值 {app.config['threshold']:.2f}")
    print("  " + "-" * 52)
    if args.no_lan:
        print("  本地测试模式：手机无法打开（--no-lan）。")
    elif app.lan_enabled:
        print("  手机与电脑连同一 Wi-Fi，打开上面的局域网地址即可。")
    else:
        print("  局域网已关闭，手机无法打开；可在页面「设置」里开启。")
    print("  首次启动如果 Windows 防火墙弹窗，请点「允许」。")
    print("  关闭页面不会停止识别；Ctrl+C 停止服务。")
    print("")

    try:
        while serving.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  正在停止…")
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        print("  已停止。")


if __name__ == "__main__":
    main()
