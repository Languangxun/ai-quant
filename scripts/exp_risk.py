#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风险参数变体实验：在今日最优稳健档上做 buy_th/cooldown 微调对比。

用法：python3 exp_risk.py <buy_th> <cooldown> <tag>
例：python3 scripts/exp_risk.py 2 8 mid2026
"""
import sys, os, json

sys.path.insert(0, "/home/lan/桌面/ai-quant")
BUY_TH = int(sys.argv[1]) if len(sys.argv) > 1 else 2
COOLDOWN = int(sys.argv[2]) if len(sys.argv) > 2 else 8
TAG = sys.argv[3] if len(sys.argv) > 3 else "mid2026"

from data.stock import cli_bridge as bridge
cli = bridge.load()
cli.CFG.RISK_PARAMS["稳健"]["buy_th"] = BUY_TH
cli.CFG.RISK_PARAMS["稳健"]["cooldown"] = COOLDOWN
cli.CFG.RISK_PARAMS["稳健"]["sell_cooldown"] = COOLDOWN
print(f"[exp] 稳健档 buy_th={BUY_TH} cooldown={COOLDOWN} tag={TAG}", flush=True)

from backtest.stock_engine import StockBacktest

kw = dict(start="2024-01-01", limit=0) if TAG == "full2024" \
    else dict(start="2026-01-01", limit=800)
bt = StockBacktest(**kw)
bt.run()
print(json.dumps(bt.stats, ensure_ascii=False, indent=2, default=str))
path = bt.save(f"exp-bt{BUY_TH}-cd{COOLDOWN}-{TAG}")
print("结果:", path, flush=True)
