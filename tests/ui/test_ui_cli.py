import pytest

from agenkit_trader import cli
from agenkit_trader.ui import server


def test_ui_refuses_non_loopback_host(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ui", "--host", "0.0.0.0"]) == cli.EXIT_REFUSED
    assert "127.0.0.1" in capsys.readouterr().err


def test_ui_defaults() -> None:
    a = cli.build_parser().parse_args(["ui"])
    assert (a.host, a.port) == ("127.0.0.1", 8765)


def test_ui_bad_port(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ui", "--port", "99999"]) == cli.EXIT_REFUSED


def test_ui_calls_serve_with_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int]] = []

    def fake_serve(host: str, port: int) -> None:
        seen.append((host, port))
        raise KeyboardInterrupt

    monkeypatch.setattr(server, "serve", fake_serve)
    assert cli.main(["ui", "--port", "9123"]) == 0
    assert seen == [("127.0.0.1", 9123)]
