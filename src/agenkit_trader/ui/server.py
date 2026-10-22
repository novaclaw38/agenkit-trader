"""Read-only local HTTP server (stdlib only, single-threaded, GET only).

Binds 127.0.0.1 only. Serves a fixed whitelist of routes; no part of the
request selects a file. Every request re-reads its files; nothing is cached.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from agenkit_trader.ui import readers
from agenkit_trader.ui.page import PAGE
from agenkit_trader.ui.readers import UiPaths

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

Route = Callable[[UiPaths], dict[str, Any]]
API: dict[str, Route] = {
    "/api/status": readers.read_status,
    "/api/decisions": readers.read_decisions,
    "/api/research": readers.read_research,
    "/api/risk": readers.read_risk,
    "/api/failure": readers.read_failure,
}


class UiBindError(ValueError):
    """Refused to bind: only 127.0.0.1 is allowed."""


class UiServer(HTTPServer):
    paths: UiPaths


class Handler(BaseHTTPRequestHandler):
    server: UiServer
    server_version = "agenkit-ui"
    sys_version = ""

    def _send(self, status: int, ctype: str, body: bytes, **extra: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        """Only 127.0.0.1 or 127.0.0.1:<bound port> (blocks DNS rebinding)."""
        host = self.headers.get("Host")
        port = self.server.server_address[1]
        return host in (LOOPBACK, f"{LOOPBACK}:{port}")

    def _misdirected(self) -> None:
        self._send(421, "text/plain; charset=utf-8", b"misdirected request\n")

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._misdirected()
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif path in API:
            try:
                data = API[path](self.server.paths)
            except Exception:  # noqa: BLE001 - never leak a trace or a path
                data = {"error": "this view could not be built"}
            body = json.dumps(readers.clean(data), allow_nan=False, default=str).encode(
                "utf-8"
            )
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def _method_not_allowed(self) -> None:
        if not self._host_ok():
            return self._misdirected()
        self._send(
            405, "text/plain; charset=utf-8", b"read-only: GET only\n", Allow="GET"
        )

    do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815
    do_OPTIONS = _method_not_allowed  # noqa: N815

    def log_message(self, format: str, *args: Any) -> None:
        return  # no request logging: nothing to leak, nothing written


def make_server(host: str, port: int, paths: UiPaths | None = None) -> UiServer:
    if host != LOOPBACK:
        raise UiBindError(f"refusing to bind {host!r}: only {LOOPBACK} is allowed")
    srv = UiServer((host, port), Handler)
    srv.paths = paths or UiPaths.default()
    return srv


def serve(host: str, port: int) -> None:
    srv = make_server(host, port)
    print(f"agenkit ui: http://{LOOPBACK}:{srv.server_address[1]}/ (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
