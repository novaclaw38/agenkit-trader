import sys
from pathlib import Path

# Make tests/fakes.py importable from nested test packages.
_TESTS = str(Path(__file__).parent)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)


import httpx  # noqa: E402
import pytest  # noqa: E402


class RealNetworkAttempt(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _forbid_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that reaches the real network (archive downloads, httpx
    over a real transport). Tests inject fakes or ``httpx.MockTransport``
    (not affected) and may re-patch these names themselves."""
    from agenkit_trader.data import archive

    def refuse(*a: object, **k: object) -> object:
        raise RealNetworkAttempt("test attempted a real network fetch")

    monkeypatch.setattr(archive, "_http_get", refuse)
    monkeypatch.setattr(httpx, "get", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
