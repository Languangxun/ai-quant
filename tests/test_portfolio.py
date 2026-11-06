from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cquant.portfolio import Book, SimConfig, simulate


def _px(opens, lows=None, highs=None, closes=None):
    dates = list(opens.index)
    cols = list(opens.columns)
    close = closes if closes is not None else opens.copy()
    low = lows if lows is not None else opens.copy()
    high = highs if highs is not None else opens.copy()
    return {
        "open": opens, "high": high, "low": low, "close": close,
    }


def test_leverage_gross_exposure():
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    opens = pd.DataFrame({"A": [100.0, 100.0, 100.0]}, index=dates)
    px = _px(opens)
    books = {"2024-01-01": Book({"A": 2.0})}
    cfg = SimConfig(capital=1000.0, fee_rate=0.0, slippage=0.0,
                    funding_daily=0.0)
    daily, trades, stats = simulate(px, books, dates, cfg)
    row = daily[daily["date"] == "2024-01-02"].iloc[0]
    assert row["leverage"] == pytest.approx(2.0, abs=1e-6)
    assert stats["liquidation_count"] == 0


def test_fees_reduce_equity():
    dates = ["2024-01-01", "2024-01-02"]
    opens = pd.DataFrame({"A": [100.0, 100.0]}, index=dates)
    px = _px(opens)
    books = {"2024-01-01": Book({"A": 1.0})}
    cfg = SimConfig(capital=1000.0, fee_rate=0.001, slippage=0.0,
                    funding_daily=0.0)
    daily, _, stats = simulate(px, books, dates, cfg)
    assert stats["fee_paid"] == pytest.approx(1.0, abs=1e-6)
    assert daily["equity"].iloc[-1] == pytest.approx(999.0, abs=1e-6)


def test_liquidation_on_crash():
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    opens = pd.DataFrame({"A": [100.0, 100.0, 100.0]}, index=dates)
    lows = pd.DataFrame({"A": [100.0, 55.0, 100.0]}, index=dates)
    closes = pd.DataFrame({"A": [100.0, 60.0, 60.0]}, index=dates)
    px = _px(opens, lows=lows, closes=closes)
    books = {"2024-01-01": Book({"A": 3.0})}
    cfg = SimConfig(capital=1000.0, fee_rate=0.0, slippage=0.0,
                    funding_daily=0.0, maintenance_rate=0.01)
    daily, _, stats = simulate(px, books, dates, cfg)
    assert stats["liquidation_count"] == 1
    assert daily["liquidated"].sum() == 1
    assert daily["equity"].iloc[-1] <= 1000.0


def test_flat_when_no_signal():
    dates = ["2024-01-01", "2024-01-02"]
    opens = pd.DataFrame({"A": [100.0, 101.0]}, index=dates)
    px = _px(opens)
    daily, _, stats = simulate(px, {}, dates, SimConfig(capital=1000.0,
                                                        funding_daily=0.0))
    assert stats["liquidation_count"] == 0
    assert daily["equity"].iloc[-1] == pytest.approx(1000.0)
    assert daily["gross"].abs().sum() == 0


def test_perf_metrics():
    from cquant.metrics import perf
    n = 365
    r = 2.0 ** (1.0 / n) - 1.0
    eq = np.cumprod(np.r_[1.0, np.full(n, 1.0 + r)])
    m = perf(pd.Series(eq))
    assert m["ann_return"] == pytest.approx(1.0, abs=0.02)
    assert m["mdd"] == pytest.approx(0.0, abs=1e-9)
