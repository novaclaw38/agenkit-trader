import http.client
import json
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path

import pytest
from ui_helpers import fill, make_paths, rec, write_log

from agenkit_trader.ui import server
from agenkit_trader.ui.readers import UiPaths

EVIL = "<script>alert('x')</script><img src=x onerror=1>"


@pytest.fixture
def paths(tmp_path: Path) -> UiPaths:
    return make_paths(tmp_path)


@pytest.fixture
def running(paths: UiPaths) -> Iterator[tuple[HTTPServer, int]]:
    srv = server.make_server("127.0.0.1", 0, paths)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, port
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


def req(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request(method, path)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


@pytest.mark.parametrize(
    "route",
    ["/api/status", "/api/decisions", "/api/research", "/api/risk", "/api/failure"],
)
def test_api_routes_ok_json_no_store(
    running: tuple[HTTPServer, int], route: str
) -> None:
    _, port = running
    status, h, body = req(port, "GET", route)
    assert status == 200
    assert h["content-type"].startswith("application/json")
    assert h["cache-control"] == "no-store"
    assert isinstance(json.loads(body), dict)


def test_index_page(running: tuple[HTTPServer, int]) -> None:
    _, port = running
    status, h, body = req(port, "GET", "/")
    assert status == 200 and h["content-type"].startswith("text/html")
    assert "default-src 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    text = body.decode()
    assert "http://" not in text and "https://" not in text  # no CDN
    for tab in ("Status", "Decisions", "Research", "Risk", "How could this fail"):
        assert tab in text
    assert text.count("setInterval") == 1


def test_query_string_ignored(running: tuple[HTTPServer, int]) -> None:
    _, port = running
    assert req(port, "GET", "/api/risk?x=../../etc/passwd")[0] == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_non_get_is_405(running: tuple[HTTPServer, int], method: str) -> None:
    _, port = running
    for path in ("/", "/api/status", "/nope"):
        status, h, _ = req(port, method, path)
        assert status == 405 and "GET" in h["allow"]


@pytest.mark.parametrize(
    "path",
    [
        "/nope",
        "/api",
        "/api/status/",
        "/../etc/passwd",
        "/..%2f..%2fetc/passwd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/api/../../etc/passwd",
        "/research/REPORT.json",
        "/logs/paper.jsonl",
        "/config/risk.yaml",
        "//etc/passwd",
        "/index.html",
    ],
)
def test_unknown_and_traversal_404(running: tuple[HTTPServer, int], path: str) -> None:
    _, port = running
    status, _, body = req(port, "GET", path)
    assert status == 404
    assert b"root:" not in body


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "", "::", "192.168.1.5", "localhost", "example.com", "127.0.0.2"],
)
def test_non_loopback_host_refused(host: str, paths: UiPaths) -> None:
    with pytest.raises(server.UiBindError):
        server.make_server(host, 0, paths)


def test_malicious_log_string_served_as_data_and_page_escapes(
    running: tuple[HTTPServer, int], paths: UiPaths
) -> None:
    write_log(
        paths.log,
        [
            rec(
                "2026-10-22T10:00:00+00:00",
                "order",
                {"order": {"symbol": EVIL, "side": "buy", "client_id": EVIL}},
                {"allowed": False, "reason": EVIL},
                "rejected",
            ),
        ],
    )
    _, port = running
    status, h, body = req(port, "GET", "/api/decisions")
    assert status == 200 and "json" in h["content-type"]
    assert json.loads(body)["rows"][0]["client_id"] == EVIL
    # The page renders every dynamic value through esc(); it never uses
    # innerHTML with a raw log value.
    page = req(port, "GET", "/")[2].decode()
    assert "function esc(" in page and "&lt;" in page
    assert "eval(" not in page and "document.write" not in page


def test_live_reread_no_cache(running: tuple[HTTPServer, int], paths: UiPaths) -> None:
    _, port = running
    first = json.loads(req(port, "GET", "/api/decisions")[2])
    assert first["rows"] == []
    write_log(paths.log, [fill("2026-10-22T08:00:00+00:00", "buy", 0.1)])
    second = json.loads(req(port, "GET", "/api/decisions")[2])
    assert len(second["rows"]) == 1


def test_corrupt_files_no_stack_trace_or_path(
    running: tuple[HTTPServer, int], paths: UiPaths, tmp_path: Path
) -> None:
    _, port = running
    paths.log.parent.mkdir()
    paths.log.write_text("garbage\n")
    paths.state.parent.mkdir()
    paths.state.write_text("[[[")
    (paths.config_dir / "risk.yaml").write_text("a: [")
    (paths.research_dir / "REPORT.json").write_text("{")
    for route in ("status", "decisions", "research", "risk", "failure"):
        status, _, body = req(port, "GET", f"/api/{route}")
        assert status == 200
        assert str(tmp_path).encode() not in body
        assert b"Traceback" not in body


def test_binds_loopback_only(running: tuple[HTTPServer, int]) -> None:
    srv, _ = running
    assert srv.server_address[0] == "127.0.0.1"


def test_server_is_not_threaded() -> None:
    from socketserver import ThreadingMixIn

    assert not issubclass(server.UiServer, ThreadingMixIn)
