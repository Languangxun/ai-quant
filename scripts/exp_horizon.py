#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DYN_W_HORIZON 实验 driver：CLI 加载后在线改 CFG，再跑回测。

用法：python3 exp_horizon.py 5 full2024
  参数1 = HORIZON 天数；参数2 = 变体标签（full2024 / mid2026）
"""
import sys, os

sys.path.insert(0, "/home/lan/桌面/ai-quant")
H = int(sys.argv[1]) if len(sys.argv) > 1 else 5
TAG = sys.argv[2] if len(sys.argv) > 2 else "full2024"

from data.stock import cli_bridge as bridge
cli = bridge.load()
cli.CFG.DYN_W_HORIZON = H            # 维度对错判定视界（默认 1 → 实验共/H）
cli.CFG.DYN_W_ALPHA = 0.02
print(f"[exp] DYN_W_HORIZON={H} tag={TAG}", flush=True)

from backtest.stock_engine import StockBacktest

kw = dict(start="2024-01-01", limit=0) if TAG == "full2024" \
    else dict(start="2026-01-01", limit=800)
bt = StockBacktest(**kw)
bt.run()
print(json.dumps(bt.stats, ensure_ascii=False, indent=2, default=str)) if (json := __import__("json")) else None
path = bt.save(f"exp-h{H}-{TAG}")
print("结果:", path, flush=True)
