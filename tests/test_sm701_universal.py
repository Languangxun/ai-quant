"""sm701 通用策略 + 多市场规则冒烟 (无网络依赖, 用合成 bars)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bars(code, n=80, base=10.0, drift=0.01):
    from core.types import Bar
    out = []
    px = base
    for i in range(n):
        px *= 1 + drift + (0.002 if i % 5 == 0 else -0.001)
        out.append(Bar(code=code, date=f"2026-05-{(i % 28) + 1:02d}_{i:03d}",
                       open=px * 0.99, high=px * 1.01, low=px * 0.98,
                       close=px, vol=1000 + i * 10))
    return out


def test_sm701_features_44dims():
    from factors import sm701
    row = sm701.bars_to_row(_bars("hk:00700"))
    assert row is not None and len(row) == 44
    assert all(isinstance(v, float) for v in row.values())


def test_fund_nav_features_no_vol():
    from factors import sm701
    from core.types import Bar
    bars = [Bar(code="fund:007301", date=f"d{i:03d}", open=1.0 + i * 0.001,
                high=1.0 + i * 0.001 + 0.0005, low=1.0 + i * 0.001 - 0.0005,
                close=1.0 + i * 0.001, vol=0.0) for i in range(80)]
    row = sm701.bars_to_row(bars)
    assert row and row["vr5"] == 1.0 and row["logvol"] == 0.0


def test_universal_strategy_runs():
    import strategies  # noqa: 注册
    from strategies import registry
    s = registry.get("sm701_universal")
    assert s is not None
    out = s.generate({"hk:00700": _bars("hk:00700"),
                      "us:AAPL": _bars("us:AAPL", drift=0.02)},
                     {"threshold": 0.0})  # 阈值0必出信号(验证通路)
    assert isinstance(out, list) and len(out) == 2


def test_market_rules_multi():
    from trading.instruments import MarketModel
    m = MarketModel()
    assert m.classify("hk:00700", "腾讯").kind == "hk"
    assert m.classify("hk:00700", "").t0
    assert m.classify("us:AAPL", "").kind == "us"
    assert m.classify("crypto:BTC/USDT", "").kind == "crypto"
    assert m.classify("crypto:BTC/USDT", "").lot_size == 0
    assert m.classify("fund:007301", "").kind == "fund"
    assert m.classify("fund:007301", "").limit_pct >= 0.99
    # A股回归不破
    assert m.classify("sz000725", "京东方A").kind == "stock"
    assert m.classify("sh510300", "沪深300ETF").kind == "etf"


def test_multi_account_fractional():
    from trading.multi_account import MultiAccount
    a = MultiAccount(10000, min_commission=0.0)
    t = a.buy("crypto:BTC/USDT", 50000.0, 0.01, "2026-10-07", "2026-10-07")
    assert abs(t.shares - 0.01) < 1e-9
    s = a.sell("crypto:BTC/USDT", 51000.0, 0.01, "2026-10-07")
    assert s.realized > 0
