import json
import math
import multiprocessing as mp
from statistics import NormalDist

import pytest

from agenkit_trader.validation.trials import TrialCounter, deflated_sharpe_pvalue


def test_counter_persists_across_instances(tmp_path) -> None:
    p = tmp_path / "research" / "trials.json"
    c = TrialCounter(p)
    assert c.total == 0
    c.add(5)
    c.add(3)
    assert TrialCounter(p).total == 8
    TrialCounter(p).add(2)
    assert c.total == 10  # re-read from disk, never a stale in-memory count
    assert json.loads(p.read_text())["total"] == 10
    names = sorted(x.name for x in p.parent.iterdir())
    assert names == ["trials.json", "trials.json.lock"]  # no temp file left


@pytest.mark.parametrize("n", [0, -1])
def test_counter_rejects_non_positive(tmp_path, n: int) -> None:
    with pytest.raises(ValueError):
        TrialCounter(tmp_path / "t.json").add(n)


def test_counter_rejects_corrupt_file(tmp_path) -> None:
    p = tmp_path / "t.json"
    p.write_text("{not json")
    with pytest.raises(ValueError):
        TrialCounter(p).total  # noqa: B018


def test_pvalue_single_trial_is_psr() -> None:
    # N=1: SR0 = 0 and p = 1 - Phi(SR*sqrt(T-1)/sqrt(1 - g3*SR + (g4-1)/4*SR^2))
    # SR=0.1, T=1001, normal returns: z = 0.1*sqrt(1000)/sqrt(1+0.005)
    z = 0.1 * math.sqrt(1000) / math.sqrt(1.005)
    assert z == pytest.approx(3.15439, abs=1e-4)
    p = deflated_sharpe_pvalue(0.1, 1, 1001, 0.0, 3.0)
    assert p == pytest.approx(1 - NormalDist().cdf(3.15439), abs=1e-6)
    assert p == pytest.approx(0.000804, abs=2e-6)


def test_pvalue_hand_computed_ten_trials() -> None:
    # SR0 = sqrt(1/(T-1)) * ((1-g)*Z(1-1/N) + g*Z(1-1/(N e)))
    # T=1001, N=10: Z(0.9)=1.281552, Z(1-1/(10e))=Z(0.963212)=1.789242
    g = 0.5772156649
    z1 = 1.2815516
    z2 = NormalDist().inv_cdf(1 - 1 / (10 * math.e))
    assert z2 == pytest.approx(1.789242, abs=1e-5)
    sr0 = math.sqrt(1 / 1000) * ((1 - g) * z1 + g * z2)
    assert sr0 == pytest.approx(0.049793, abs=1e-5)
    # skew -0.5, kurt 6 change the denominator
    den = math.sqrt(1 - (-0.5) * 0.1 + (6 - 1) / 4 * 0.01)
    z = (0.1 - sr0) * math.sqrt(1000) / den
    expected = 1 - NormalDist().cdf(z)
    assert expected == pytest.approx(0.061747, abs=1e-5)
    assert deflated_sharpe_pvalue(0.1, 10, 1001, -0.5, 6.0) == pytest.approx(
        expected, abs=1e-6
    )


def test_pvalue_worsens_with_trials() -> None:
    ps = [deflated_sharpe_pvalue(0.08, n, 2000, 0.0, 3.0) for n in (1, 10, 100, 1e4)]
    assert all(a < b for a, b in zip(ps, ps[1:], strict=False))
    assert ps[0] < 0.05 < ps[-1]


@pytest.mark.parametrize(
    "args",
    [
        (0.1, 0, 100, 0.0, 3.0),
        (0.1, 1, 1, 0.0, 3.0),
        (float("nan"), 1, 100, 0.0, 3.0),
        (0.1, 1, 100, float("inf"), 3.0),
    ],
)
def test_pvalue_rejects_bad_input(args) -> None:
    with pytest.raises(ValueError):
        deflated_sharpe_pvalue(*args)


def _hammer(path: str, n: int) -> None:
    c = TrialCounter(path)  # type: ignore[arg-type]
    for _ in range(n):
        c.add(1)


def test_counter_exact_under_concurrent_processes(tmp_path) -> None:
    p = tmp_path / "trials.json"
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_hammer, args=(str(p), 200)) for _ in range(2)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(60)
        assert pr.exitcode == 0
    assert TrialCounter(p).total == 400
