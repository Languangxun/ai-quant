#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消融选型回测实验：每只股票用它自己消融胜出的策略/档位生成买卖信号。

数据源：伙伴机（或本机）logs/ablation-<日期>.json 里 per_code.picked ---
  per-code {mode: label} 三档冠军（周末全 A 消融 PowerShell 产物）。
策略生成：直接调度 stock_predict CLI 的 _sig_* 族函数（MACD/KDJ/RSI/布林/
MA趋势/L1形态/L2行业/筹码/板块轮动/量比/LGBM/sm701/异动/多维评分），
与 GUI"逐股自动选型"完全同源。无消融记录的股票回退 composite 稳健。

用法：python3 scripts/exp_ablation_backtest.py <标签> [区间=2024|2026] [json路径]
例：  python3 scripts/exp_ablation_backtest.py ablation150 2026
"""
import sys, os, json, glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TAG = sys.argv[1] if len(sys.argv) > 1 else "ablation"
REGION = sys.argv[2] if len(sys.argv) > 2 else "2026"
JSON_PATH = (sys.argv[3] if len(sys.argv) > 3
             else sorted(glob.glob("/home/lan/ai-quant/logs/ablation-*.json")
                         + glob.glob(os.path.dirname(os.path.abspath(__file__))
                                     + "/../logs/ablation-*.json"),
                         key=os.path.getmtime, reverse=True)[0])
print(f"[exp-ablation] picks={JSON_PATH} region={REGION}", flush=True)

from data.stock import cli_bridge as bridge
cli = bridge.load()
ALGO_LABEL = cli.ALGO_LABEL
INV = {v: k for k, v in ALGO_LABEL.items()}
SPECIAL_INDUSTRY = {"l2_industry"}
SPECIAL_CODE = {"sm701", "analog"}

try:
    doc = json.load(open(JSON_PATH, encoding="utf-8"))
except OSError as e:
    raise SystemExit(f"消融JSON不可读: {e}")
per = {x["code"]: (x.get("picked") or {})
       for x in (doc.get("per_code") or [])}
print(f"[exp-ablation] per_code 覆盖 {len(per)} 只", flush=True)

_picks_low = {c.lower(): v for c, v in per.items()}
_ind_cache = {}

def _industry(code):
    if code not in _ind_cache:
        with cli.db_conn() as conn:
            r = conn.execute("select industry from stocks where code=?",
                             (code,)).fetchone()
        _ind_cache[code] = (r[0] if r else "") or ""
    return _ind_cache[code]

def _best_pick(code):
    p = _picks_low.get(code.lower()) or {}
    best = None
    for mode, info in p.items():
        if not info or info.get("val_ann") is None:
            continue
        if best is None or (info.get("val_ann") or -9) > (best[1].get("val_ann") or -9):
            best = (mode, info)
    return best

def signal_fn(rows, code):
    best = _best_pick(code)
    if not best:
        return bridge.signals(rows, "稳健")
    mode, info = best
    label = (info.get("label") or "").rsplit("·", 1)[0]
    key = INV.get(label)
    rp = cli.CFG.RISK_PARAMS.get(mode) or cli.CFG.RISK_PARAMS["稳健"]
    try:
        if key == "composite":
            sigs = cli._composite_signals(rows, rp)
        elif key in SPECIAL_INDUSTRY:
            sigs = cli._sig_l2_industry(rows, industry=_industry(code))
        elif key in SPECIAL_CODE:
            sigs = (cli._sig_sm701(rows, code) if key == "sm701"
                    else cli._sig_analog(rows, code))
        else:
            fn = getattr(cli, f"_sig_{key}")
            if key in ("sector_rot", "l1_pattern", "l2_industry"):
                import inspect as _insp
                kw = {}
                if "industry" in _insp.signature(fn).parameters:
                    kw["industry"] = _industry(code)
                sigs = fn(rows, **kw)
            else:
                sigs = fn(rows)
        return sigs or []
    except Exception as e:
        print(f"[exp-ablation] {code} {label} 生成失败 {type(e).__name__}: {e}",
              flush=True)
        return bridge.signals(rows, "稳健")

from backtest.stock_engine import StockBacktest
kw = dict(start="2024-01-01", limit=0) if REGION == "2024" \
    else (dict(start="2024-01-01", limit=0) if REGION == "full2024"
          else dict(start="2026-01-01", limit=800))
bt = StockBacktest(**kw)
bt.custom_signal_fn = signal_fn
bt.run()
print(json.dumps(bt.stats, ensure_ascii=False, indent=2, default=str))
path = bt.save(f"exp-abl-{TAG}")
print("结果:", path, flush=True)
