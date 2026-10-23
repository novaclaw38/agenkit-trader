import httpx
import pytest

from agenkit_trader.data import archive


def test_real_fetches_are_refused(tmp_path: object) -> None:
    with pytest.raises(AssertionError, match="real network"):
        archive._http_get("https://data.binance.vision/x")
    with pytest.raises(AssertionError, match="real network"):
        httpx.get("https://example.com")
    with pytest.raises(AssertionError, match="real network"):
        httpx.Client().get("https://example.com")


def test_mock_transport_still_works() -> None:
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert c.get("https://example.com").status_code == 200
