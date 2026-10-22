import json
import math

import pandas as pd

from agenkit_trader.backtest.stats import Stats
from agenkit_trader.validation.gate import Verdict
from agenkit_trader.validation.report import write_report
from agenkit_trader.validation.sweep import SweepResult


def _v(name: str, passed: bool, pf: float = 1.5) -> Verdict:
    st = Stats(1.2, 0.1, 0.55, pf, 40, 0.2)
    return Verdict(
        passed=passed,
        reasons=[] if passed else ["hold-out Sharpe 0.10 < min 0.5"],
        train=st,
        holdout=st,
        cost_2x=st,
        regimes={"trend": st, "range": st},
        name=name,
        edge="edge text",
        symbol="BTCUSDT",
        interval="1h",
        params={"thr": 0.5},
        allowed_regimes=["range", "trend"],
        flags=[],
        pvalue=0.01,
        n_trials=12,
        sweep=SweepResult(
            pd.DataFrame({"thr": [0.5], "score": [1.0]}),
            True,
            {"thr": 0.5},
            1.0,
            [],
            "",
        ),
        ruined=False,
    )


def test_report_and_deployed(tmp_path) -> None:
    path = tmp_path / "out" / "report.md"
    write_report([_v("good", True, pf=math.inf), _v("bad", False)], path)
    text = path.read_text()
    header = (
        "| Strategy | Edge | Market | Timeframe | Sharpe | Max Drawdown | Trades "
        "| Cost Sensitivity | Verdict |"
    )
    assert header in text
    assert "REJECT" in text and "hold-out Sharpe 0.10" in text  # reasons kept
    deployed = json.loads((path.parent / "deployed.json").read_text())
    assert deployed == [
        {
            "name": "good",
            "symbol": "BTCUSDT",
            "interval": "1h",
            "params": {"thr": 0.5},
            "allowed_regimes": ["range", "trend"],
        }
    ]
    evidence = json.loads(path.with_suffix(".json").read_text())
    pf = evidence[0]["holdout"]
    assert pf["profit_factor"] is None and pf["profit_factor_nonfinite"] == "inf"
    assert evidence[1]["reasons"] == ["hold-out Sharpe 0.10 < min 0.5"]


def test_nothing_passes_overwrites_stale_deployed(tmp_path) -> None:
    path = tmp_path / "report.md"
    (tmp_path / "deployed.json").write_text('[{"name": "stale"}]')
    write_report([_v("bad", False)], path)
    assert json.loads((tmp_path / "deployed.json").read_text()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "deployed.json",
        "report.json",
        "report.md",
    ]
