#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""五市场长区间回测（基金/港/美/加密/期货）：

用 sm701_universal 策略（与模拟盘同源）做周度调仓回测，
输出 backtest/results/multi-<market>-long.json（equity 曲线），
供网站 /quant 页统一区间对比。

用法：
  python3 scripts/backtest_multi_markets.py --start 2023-01-01 --rebalance 5
  python3 scripts/backtest_multi_markets.py --markets hk,us --start 2022-01-01
"""
import argparse
import bisect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(BASE, "backtest", "results")
MARKETS = ("fund", "hk", "us", "crypto", "fut")
LABEL = {"fund": "基金", "hk": "港股", "us": "美股",
         "crypto": "加密", "fut": "期货"}
THR = {"fund": 0.52, "hk": 0.55, "us": 0.55, "crypto": 0.55, "fut": 0.55}
TOPK = {"fund": 3, "hk": 5, "us": 5, "crypto": 3, "fut": 5}
COST = 0.001  # 单边费用（调仓换手按 0.1% 估）


def _load_market(market, start, lookback=260):
    from data import unified as u
    from data.universe_filter import liquid_top
    if market == "fund":
        from data.fund import bars as fb
        uni = list(fb.universe())
        get_bars = lambda c, tail: fb.get_bars(c, tail=tail)  # noqa
    else:
        all_u = u.universe(market)
        uni = liquid_top(market, all_u,
                         lambda c, tail=150: u.get_bars(c, tail=tail),
                         limit=0 if market == "crypto" else 200)
        get_bars = lambda c, tail: u.get_bars(c, tail=tail)  # noqa
    codes = [c for c, _n in uni]
    data = {}
    for i, c in enumerate(codes):
        try:
            bs = get_bars(c, 2000)
        except Exception:
            continue
        if len(bs) >= 120:
            data[c] = bs
        if (i + 1) % 50 == 0:
            print(f"  load {market} {i+1}/{len(codes)}", flush=True)
    return data


def _run_market(market, start, end=None, rebalance=5):
    from strategies import builtin  # noqa: 注册策略
    from strategies import registry
    t0 = time.time()
    thr, topk = THR.get(market, 0.55), TOPK.get(market, 5)
    pool = _load_market(market, start)
    if not pool:
        print(f"[{market}] 无数据", flush=True)
        return None
    # 预构建日期→收盘
    px = {}
    all_dates = set()
    for c, bs in pool.items():
        px[c] = {b.date: b.close for b in bs}
        all_dates.update(px[c].keys())
    dates = sorted(d for d in all_dates if d >= start and (not end or d <= end))
    if len(dates) < 60:
        print(f"[{market}] 区间数据不足 {len(dates)} 天", flush=True)
        return None
    strat = registry.get("sm701_universal")
    nav = 1.0
    equity = []
    positions = {}   # code -> weight
    prev = None
    n_reb = 0
    for i, d in enumerate(dates):
        # 当日收益（用上一交易日收盘价到今日收盘价）
        if positions and prev:
            r = 0.0
            for c, w in positions.items():
                p0, p1 = px[c].get(prev), px[c].get(d)
                if p0 and p1:
                    r += w * (p1 / p0 - 1.0)
            nav *= (1.0 + r)
        # 周度调仓
        if i % rebalance == 0:
            snap = {}
            for c, bs in pool.items():
                ds = [b.date for b in bs]
                j = bisect.bisect_right(ds, d)
                if j < 60:
                    continue
                snap[c] = bs[max(0, j - 200):j]
            if snap:
                try:
                    sigs = strat.generate(snap, {"date": d, "threshold": thr,
                                                 "topk": topk})
                except Exception as e:
                    print(f"  [{market}] {d} 信号失败 {e}", flush=True)
                    sigs = []
                want = sigs[:topk]
                newpos = ({s.code: 1.0 / len(want) for s in want}
                          if want else {})
                # 换手成本
                turnover = 0.0
                oldset, newset = set(positions), set(newpos)
                turnover += len(newset - oldset) / max(1, max(len(positions), len(newpos)))
                turnover += len(oldset - newset) / max(1, max(len(positions), len(newpos)))
                if turnover:
                    nav *= (1.0 - COST * turnover)
                positions = newpos
                n_reb += 1
        equity.append([d, round(nav * 10000.0, 2)])
        prev = d
    out = {
        "market": market, "label": LABEL.get(market, market),
        "thr": thr, "topk": topk, "rebalance": rebalance,
        "start": dates[0], "end": dates[-1], "days": len(dates),
        "ret": equity[-1][1] / equity[0][1] - 1.0,
        "equity": equity,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    os.makedirs(RESULTS, exist_ok=True)
    p = os.path.join(RESULTS, f"multi-{market}-long.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    dur = time.time() - t0
    print(f"[{market}] {dates[0]} ~ {dates[-1]} | {len(dates)}天 "
          f"| 收益 {out['ret']*100:+.1f}% | 调仓{n_reb}次 | {dur:.0f}s", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--rebalance", type=int, default=5)
    ap.add_argument("--markets", default=",".join(MARKETS))
    ap.add_argument("--legacy-us", action="store_true",
                    help="用旧 sm701_universal 重跑美股 (会覆盖 P1 曲线); "
                         "默认美股由 gen_us_p1_artifacts.py 单独生成")
    args = ap.parse_args()
    items = [m.strip() for m in args.markets.split(",") if m.strip()]
    for m in items:
        print(f"===== {m} =====", flush=True)
        if m == "us" and not args.legacy_us:
            print("[us] 跳过: P1 曲线由 scripts/gen_us_p1_artifacts.py "
                  "生成 (--legacy-us 可强制旧口径)", flush=True)
            continue
        try:
            _run_market(m, args.start, args.end, args.rebalance)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[{m}] 失败: {e}", flush=True)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
