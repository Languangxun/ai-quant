"""pi_send_report 多市场邮件段落测试（不发送邮件）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.pi_send_report import multi_market_report


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _state(capital, cash, code, shares, cost, price):
    return {
        "initial_capital": capital, "cash": cash,
        "last_prices": {code: price},
        "lots": {code: [{"code": code, "shares": shares, "cost": cost,
                         "buy_date": "2026-11-01",
                         "sellable_date": "2026-11-01"}]},
        "realized_pnl": 0, "trade_count": 1,
    }


def test_multi_report_with_daily_and_no_trades(tmp_path):
    base = str(tmp_path)
    _write(os.path.join(base, "sim", "state", "multi-hk.json"),
           _state(20000, 1000, "hk:00700", 10, 250, 300))
    _write(os.path.join(base, "memory", "daily", "multi-hk-2026-11-04.json"), {
        "date": "2026-11-04", "market": "hk",
        "signals": [{"code": "hk:00700", "score": 70.2}],
        "trades": [],
        "account": {
            "cash": 1000, "total_asset": 4000,
            "positions": {"hk:00700": {"pct": 75.0, "cost": 250,
                                       "price": 300, "pnl_pct": 20.0}},
        },
    })
    text = multi_market_report(base)
    assert "港股" in text
    assert "hk:00700" in text
    assert "4,000.00 HKD" in text
    assert "无交易" in text
    assert "信号: hk:00700 70.2" in text


def test_multi_report_falls_back_to_state(tmp_path):
    base = str(tmp_path)
    _write(os.path.join(base, "sim", "state", "multi-crypto.json"),
           _state(10000, 500, "crypto:BTC/USDT", 1.5, 100, 120))
    text = multi_market_report(base)
    assert "加密" in text
    assert "crypto:BTC/USDT" in text
    assert "暂无快照" in text
    assert "680.00" in text  # 500 现金 + 1.5 * 120


def test_multi_report_empty(tmp_path):
    assert multi_market_report(str(tmp_path)) == ""
