"""市场简报纯计算测试（不联网）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import market_view


def _stats(name, close, ma20, ma60, ret20, ret60, date="2026-11-05"):
    return {
        "name": name, "code": "x", "date": date, "lag_days": 0,
        "close": close, "ma20": ma20, "ma60": ma60,
        "above_ma20": close >= ma20, "above_ma60": close >= ma60,
        "ret20_pct": ret20, "ret60_pct": ret60,
    }


def test_render_weak_market():
    text, trend = market_view._render([
        _stats("上证指数", 3000, 3100, 3200, -3.2, -5.1),
        _stats("创业板指", 2000, 2200, 2300, -10.0, -18.0),
        _stats("科创50", 900, 950, 1000, -4.0, -8.0),
    ], "2026-11-05")
    assert trend == "weak"
    assert "偏弱" in text
    assert "MA20" in text and "下方" in text


def test_render_strong_market():
    text, trend = market_view._render([
        _stats("上证指数", 3300, 3200, 3100, 3.0, 6.0),
        _stats("创业板指", 2300, 2200, 2100, 4.0, 8.0),
        _stats("科创50", 1000, 950, 900, 5.0, 10.0),
    ], "2026-11-05")
    assert trend == "strong"
    assert "偏强" in text


def test_index_stats_uses_rows(monkeypatch):
    rows = []
    for i in range(70):
        rows.append({"date": f"2026-01-{i + 1:02d}",
                     "open": 100 + i, "high": 101 + i, "low": 99 + i,
                     "close": 100 + i, "vol": 1000})
    monkeypatch.setattr(market_view, "_rows", lambda code, latest=None: rows)
    s = market_view._index_stats("测试指数", "sh000001", "2026-01-70")
    assert s["close"] == 169
    assert s["above_ma20"] is True
    assert s["ret20_pct"] is not None
    assert s["lag_days"] is None
