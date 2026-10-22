"""Static symbol rules for research (config/symbol_rules.yaml) match the
testnet exchangeInfo snapshot they were taken from (no network)."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import httpx
import pytest

from agenkit_trader.backtest.runtime_policy import RuntimePolicy
from agenkit_trader.config import ConfigError, load_symbol_rules
from agenkit_trader.execution.binance_testnet import BinanceTestnetVenue
from agenkit_trader.execution.venue import SymbolRules

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config" / "symbol_rules.yaml"
SNAP = ROOT / "tests" / "execution" / "exchangeinfo_snapshot.json"


def test_committed_rules() -> None:
    r = load_symbol_rules(CFG)
    assert r == {
        "BTCUSDT": SymbolRules(tick=0.1, lot=0.0001, min_notional=50.0),
        "ETHUSDT": SymbolRules(tick=0.01, lot=0.001, min_notional=20.0),
    }


def test_rules_equal_what_the_runtime_parses_from_the_snapshot() -> None:
    snap = json.loads(SNAP.read_text())

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/fapi/v1/exchangeInfo"
        return httpx.Response(200, json=snap)

    v = BinanceTestnetVenue(transport=httpx.MockTransport(handler))
    for sym, rules in load_symbol_rules(CFG).items():
        assert v.symbol_rules(sym) == rules


@pytest.mark.parametrize(
    "text",
    [
        "BTCUSDT: {tick: 0.1, lot: 0.001}\n",
        "BTCUSDT: {tick: 0.1, lot: -1, min_notional: 5}\n",
        "BTCUSDT: {tick: .nan, lot: 0.1, min_notional: 5}\n",
        "BTCUSDT: {tick: 0.1, lot: 0.1, min_notional: 5, x: 1}\n",
        "- 1\n",
    ],
)
def test_bad_rules_file_refused(tmp_path: Path, text: str) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(text)
    with pytest.raises((ConfigError, ValueError)):
        load_symbol_rules(p)


def test_run_research_gives_each_symbol_its_rules() -> None:
    mod = runpy.run_path(str(ROOT / "scripts" / "run_research.py"))
    pols = mod["runtime_policies"](["BTCUSDT", "ETHUSDT"])
    assert isinstance(pols["BTCUSDT"], RuntimePolicy)
    assert pols["BTCUSDT"].rules == SymbolRules(0.1, 0.0001, 50.0)
    assert pols["ETHUSDT"].rules == SymbolRules(0.01, 0.001, 20.0)
    assert pols["ETHUSDT"].risk.max_risk_per_trade == 0.01
    with pytest.raises(SystemExit, match="SOLUSDT"):
        mod["runtime_policies"](["SOLUSDT"])
