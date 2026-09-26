"""測試用的假服務：真的 HTTP（`http.server` 執行緒），給只用 `urllib` 的 hook 客戶端打。

`handler(method, path, headers, body) -> (status, payload, extra_headers)`；
payload 是 dict/list（轉 JSON）或 bytes。所有請求記在 `requests`。
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Handler = Callable[[str, str, dict[str, str], Any], tuple[int, Any, dict[str, str]]]


def episodes_ok(method: str, path: str, headers: dict[str, str], body: Any):
    """`POST /v1/episodes` 全部 accepted。"""
    items = body.get("episodes", []) if isinstance(body, dict) else []
    results = [{"index": i, "status": "accepted"} for i in range(len(items))]
    return 200, {"results": results}, {}


class FakeService:
    def __init__(self, handler: Handler = episodes_ok) -> None:
        self.handler = handler
        self.requests: list[dict[str, Any]] = []
        service = self

        class _H(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                headers = {k.lower(): v for k, v in self.headers.items()}
                service.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": headers,
                        "body": body,
                    }
                )
                status, payload, extra = service.handler(
                    self.command, self.path, headers, body
                )
                data = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload).encode("utf-8")
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = _serve
            do_POST = _serve

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeService:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


class BlackHole:
    """接受連線（listen backlog）但永不回應：模擬服務卡住，客戶端只能等逾時。"""

    def __enter__(self) -> BlackHole:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        return self

    @property
    def url(self) -> str:
        host, port = self._sock.getsockname()
        return f"http://{host}:{port}"

    def __exit__(self, *exc: Any) -> None:
        self._sock.close()


def closed_port_url() -> str:
    """一個沒有人聽的 port（連線被拒）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()
    return f"http://{host}:{port}"
