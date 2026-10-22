"""Multiple-testing bookkeeping: the trial counter and the deflated Sharpe.

Every parameter setting ever backtested (every grid point, every strategy,
every symbol) is a *trial*. ``TrialCounter`` persists the cumulative count so
that significance is always adjusted for everything that was tried, not just
for the current run.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from pathlib import Path
from statistics import NormalDist

DEFAULT_TRIALS_PATH = Path("research/trials.json")
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a fsynced temp file and ``os.replace``.

    Readers see either the old or the new content, never a partial file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class TrialCounter:
    """Cumulative count of variants evaluated, stored as ``{"total": n}``.

    The file is the source of truth: ``total`` re-reads it and ``add``
    read-modify-writes it atomically, so separate instances (and separate
    runs) agree. Concurrent writers are serialised: ``add`` holds an exclusive
    ``fcntl.flock`` on the sibling file ``<name>.lock`` for the whole
    read-add-replace, so no increment is lost (POSIX only; the lock file is
    left in place).
    """

    def __init__(self, path: Path = DEFAULT_TRIALS_PATH) -> None:
        self.path = Path(path)

    @property
    def total(self) -> int:
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text())
            total = data["total"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ValueError(f"{self.path}: corrupt trial counter ({e})") from e
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ValueError(f"{self.path}: invalid total {total!r}")
        return total

    def add(self, n: int) -> int:
        """Record ``n`` (> 0) more trials; return the new total."""
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            raise ValueError(f"trial increment must be a positive int, got {n!r}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        with open(lock, "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                new = self.total + n
                atomic_write_text(self.path, json.dumps({"total": new}) + "\n")
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return new


def expected_max_sharpe(n_trials: int, n_obs: int) -> float:
    """Expected maximum of ``n_trials`` per-period Sharpe estimates under H0.

    ``SR0 = sqrt(V) * ((1-g) * Z(1 - 1/N) + g * Z(1 - 1/(N e)))`` with ``g``
    the Euler-Mascheroni constant (Bailey & Lopez de Prado 2014, eq. for
    E[max]). ``V`` is the variance of the Sharpe estimates across trials;
    since it is not observed here we use the sampling variance of a
    per-period Sharpe estimate under the null (true SR = 0), ``1/(T-1)``.
    ``N = 1`` gives 0 (no selection, the test reduces to the PSR).
    """
    if n_trials == 1:
        return 0.0
    g = EULER_GAMMA
    z1 = _N.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(1.0 / (n_obs - 1)) * ((1.0 - g) * z1 + g * z2)


def deflated_sharpe_pvalue(
    sharpe: float, n_trials: int, n_obs: int, skew: float, kurt: float
) -> float:
    """One-sided p-value of the Deflated Sharpe Ratio (Bailey & Lopez de Prado).

    Units: ``sharpe`` is the **per-period** (per-bar, NOT annualised) Sharpe
    ratio of ``n_obs`` returns; divide an annualised Sharpe by
    ``sqrt(periods_per_year)`` first. ``skew`` is the sample skewness and
    ``kurt`` the **raw** (non-excess) kurtosis of those returns (normal = 3).
    ``n_trials`` is the cumulative number of variants tried (>= 1).

    ``DSR = Phi((SR - SR0) * sqrt(T-1) / sqrt(1 - skew*SR + (kurt-1)/4 * SR^2))``
    with ``SR0 = expected_max_sharpe(n_trials, T)``; returns ``1 - DSR``, the
    probability of seeing this Sharpe if the best of ``n_trials`` skill-less
    strategies were selected. It grows with ``n_trials``.
    """
    for name, v in (("sharpe", sharpe), ("skew", skew), ("kurt", kurt)):
        if not math.isfinite(v):
            raise ValueError(f"{name} must be finite, got {v!r}")
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials!r}")
    if n_obs < 2:
        raise ValueError(f"n_obs must be >= 2, got {n_obs!r}")
    var = 1.0 - skew * sharpe + (kurt - 1.0) / 4.0 * sharpe**2
    if var <= 0:
        raise ValueError(f"non-positive Sharpe variance term {var!r}")
    sr0 = expected_max_sharpe(int(n_trials), n_obs)
    z = (sharpe - sr0) * math.sqrt(n_obs - 1) / math.sqrt(var)
    return 1.0 - _N.cdf(z)
