"""静态页面、JSON API、SSE、CSV 与录音文件。

线程：`ThreadingHTTPServer` 每个请求一条线程；SSE 连接独占一条线程，直到
浏览器关闭或局域网关闭。识别与网页同进程，通过 `App` 的深接口交互。
"""

from __future__ import annotations

import json
import os
import queue
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .app import App
from .errors import ApiError

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _content_type(path: str) -> str:
    if path.endswith(".html"):
        return "text/html; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    if path.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if path.endswith(".svg"):
        return "image/svg+xml"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".wav"):
        return "audio/wav"
    if path.endswith(".ico"):
        return "image/x-icon"
    return "application/octet-stream"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    app: App


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "doorbell-web/1.0"

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    @property
    def app(self) -> App:
        return self.server.app

    def log_message(self, fmt, *args):
        return

    def _token(self) -> str | None:
        cookie = self.headers.get("Cookie") or ""
        for piece in cookie.split(";"):
            key, _, value = piece.strip().partition("=")
            if key == "db_auth":
                return value
        return None

    def _client_allowed(self) -> bool:
        return self.app.lan_allows(self.client_address[0] if self.client_address else "")

    def require_auth(self) -> bool:
        if not self.app.authorized(self._token()):
            self.send_json(401, {"error": "需要访问口令"})
            return False
        return True

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

    def send_bytes(self, status: int, body: bytes, content_type: str,
                   extra_headers=None) -> None:
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
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("请求内容不是有效的 JSON") from None
        if not isinstance(data, dict):
            raise ApiError("请求内容必须是对象")
        return data

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if not self._client_allowed():
                self.send_json(403, {"error": "局域网访问已关闭，仅本机可访问"})
                return

            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path == "/api/health":
                self.send_json(200, {
                    "app": "doorbell-web",
                    "mock": False,
                    "pid": os.getpid(),
                    "port": self.server.server_address[1],
                })
                return

            if path == "/events":
                if not self.require_auth():
                    return
                self.handle_sse()
                return

            if path == "/api/state":
                if not self.require_auth():
                    return
                self.send_json(200, self.app.bus.full_state())
                return

            if path == "/api/events":
                if not self.require_auth():
                    return
                query = parse_qs(parsed.query)
                try:
                    before = int(query.get("before", ["0"])[0] or 0)
                    limit = int(query.get("limit", ["40"])[0] or 40)
                except ValueError:
                    raise ApiError("分页参数不合法")
                self.send_json(200, self.app.bus.list_events(before, limit))
                return

            if path == "/api/export.csv":
                if not self.require_auth():
                    return
                self.send_csv()
                return

            if path.startswith("/api/recordings/"):
                if not self.require_auth():
                    return
                self.send_recording(unquote(path[len("/api/recordings/"):]))
                return

            if path.startswith("/api/"):
                self.send_json(404, {"error": "接口不存在"})
                return

            self.serve_static(path)

        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            self.send_json(500, {"error": "服务内部错误，请查看控制台"})
            print(f"[web] GET {path} 出错：{exc!r}", flush=True)

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if not self._client_allowed():
                self.send_json(403, {"error": "局域网访问已关闭，仅本机可访问"})
                return

            if path == "/api/auth":
                body = self.read_json()
                token = self.app.authorize(str(body.get("password", "")))
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
                if bool(body.get("on")):
                    self.send_json(200, self.app.start_listen())
                else:
                    self.send_json(200, self.app.stop_listen())
                return

            if path == "/api/learn":
                action = str(body.get("action", "start"))
                if action == "cancel":
                    self.send_json(200, self.app.cancel_learn())
                else:
                    self.send_json(200, self.app.start_learn(body.get("takes")))
                return

            if path == "/api/preview":
                self.send_json(200, self.app.preview(
                    cls=body.get("cls"),
                    test=bool(body.get("test")),
                    file=body.get("file"),
                ))
                return

            if path == "/api/settings":
                self.send_json(200, self.app.update_settings(body))
                return

            if path == "/api/templates/refresh":
                self.send_json(200, self.app.refresh_templates_api())
                return

            if path == "/api/iot/config":
                self.send_json(200, self.app.update_iot(body))
                return

            if path == "/api/iot/test":
                self.send_json(200, self.app.test_iot())
                return

            if path == "/api/recordings/clear":
                self.send_json(200, self.app.clear_recordings())
                return

            self.send_json(404, {"error": "接口不存在"})

        except ApiError as exc:
            self.send_json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            self.send_json(500, {"error": "服务内部错误，请查看控制台"})
            traceback.print_exc()
            print(f"[web] POST {path} 出错：{exc!r}", flush=True)

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    def handle_sse(self) -> None:
        token = self._token()
        sub = self.app.bus.subscribe()
        remote = self.client_address[0] if self.client_address else ""

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            self.wfile.write(b"retry: 3000\n\n")
            initial = json.dumps(
                self.app.bus.snapshot(), ensure_ascii=False, separators=(",", ":")
            )
            self.wfile.write(f"event: state\ndata: {initial}\n\n".encode("utf-8"))
            self.wfile.flush()

            last_sent = time.time()
            while True:
                if not self.app.lan_allows(remote) or not self.app.authorized(token):
                    break

                try:
                    payload = sub.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_sent >= 1.0:
                        stamp = json.dumps({"ts": int(time.time() * 1000)})
                        self.wfile.write(f"event: heartbeat\ndata: {stamp}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_sent = time.time()
                    continue

                self.wfile.write(payload.encode("utf-8") if isinstance(payload, str) else payload)
                self.wfile.flush()
                last_sent = time.time()

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.bus.unsubscribe(sub)
            self.close_connection = True

    # ------------------------------------------------------------------
    # 静态 / CSV / 录音
    # ------------------------------------------------------------------

    def serve_static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        if relative == "":
            relative = "index.html"

        base = STATIC_DIR.resolve()
        full = (base / relative).resolve()
        if full != base and base not in full.parents:
            self.send_json(403, {"error": "禁止访问"})
            return
        if full.is_dir():
            full = full / "index.html"
        if not full.is_file():
            self.send_json(404, {"error": "文件不存在"})
            return

        body = full.read_bytes()
        self.send_bytes(200, body, _content_type(str(full)))

    def send_csv(self) -> None:
        body = self.app.bus.csv_bytes()
        filename = "session_" + time.strftime("%Y%m%d_%H%M%S") + ".csv"
        self.send_bytes(
            200, body, "text/csv; charset=utf-8",
            [("Content-Disposition", f'attachment; filename="{filename}"')],
        )

    def send_recording(self, name: str) -> None:
        path = self.app.bus.recording_path(name)
        if path is None:
            self.send_json(404, {"error": "录音不存在或已清空"})
            return
        body = path.read_bytes()
        self.send_bytes(
            200, body, "audio/wav",
            [("Content-Disposition", f'inline; filename="{path.name}"')],
        )
