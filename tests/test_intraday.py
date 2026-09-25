"""日内多时点：最小下单额自适应 + 买入限额 + 当日汇总合并。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import intraday
from trading.stock_account import StockAccount
from trading.stock_executor import StockExecutor


def test_min_order_scales_with_capital():
    acc = StockAccount(20000)
    ex = StockExecutor(acc, max_positions=5, max_position_pct=20,
                       min_order_amount=5000, min_order_pct=5)
    assert ex.min_amount_for(20000) == 1000
    t = ex.buy("sh600900", 28.36, "2026-09-28", target_pct=14,
               prices={"sh600900": 28.36})
    assert not isinstance(t, str) and t.side == "BUY"
    assert t.shares == 100


def test_tiny_order_still_rejected():
    acc = StockAccount(20000)
    ex = StockExecutor(acc, min_order_amount=5000, min_order_pct=5)
    r = ex.buy("sh600900", 28.36, "2026-09-28", target_pct=2,
               prices={"sh600900": 28.36})
    assert isinstance(r, str) and "金额不足" in r


def test_skip_buy_once_per_code_and_daily_cap():
    existing = {"trades": [{"side": "BUY", "code": "sh600900"}]}
    cfg = {"once_per_code_per_day": True, "max_buys_per_day": 3}
    assert intraday.skip_buy({"code": "sh600900"}, existing, cfg) == "今日已买入"
    assert intraday.skip_buy({"code": "sz000725"}, existing, cfg) is None
    cfg2 = {"once_per_code_per_day": False, "max_buys_per_day": 1}
    assert (intraday.skip_buy({"code": "sz000725"}, existing, cfg2)
            == "今日买入已达上限")


def test_merge_summary_accumulates():
    existing = {
        "orders": [{"code": "a"}],
        "trades": [{"side": "BUY", "code": "a"}],
        "sessions": [{"time": "10:00"}],
    }
    session = {
        "time": "11:00", "time_iso": "2026-09-28T11:00:00",
        "date": "2026-09-28", "market_view": "震荡",
        "orders": [{"code": "b"}],
        "trades": [{"side": "SELL", "code": "b"}],
    }
    out = intraday.merge_summary(existing, session, {"cash": 1},
                                 [{"code": "b"}])
    assert len(out["sessions"]) == 2
    assert [o["code"] for o in out["orders"]] == ["a", "b"]
    assert [t["code"] for t in out["trades"]] == ["a", "b"]
    assert out["market_view"] == "震荡"
    assert out["candidates"] == [{"code": "b"}]


def test_render_review_multi_session():
    summary = {
        "account": {"total_asset": 20000.0, "cash": 20000.0,
                    "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                    "positions": {}},
        "sessions": [
            {"time": "10:00", "market_view": "早盘试探",
             "orders": [{"action": "BUY", "code": "sh600900",
                         "position": 10, "reason": "评分高"}],
             "trades": []},
            {"time": "14:45", "market_view": "尾盘确认",
             "orders": [], "trades": [],
             "skipped": ["sh600900 今日已买入"]},
        ],
    }
    text = intraday.render_review("2026-09-28", summary)
    assert "### 10:00" in text and "### 14:45" in text
    assert "当日时点数：2" in text
    assert "跳过：sh600900 今日已买入" in text
    assert "空仓" in text
