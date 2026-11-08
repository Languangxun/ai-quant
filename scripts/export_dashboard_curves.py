#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从桌面 stock_predict 的 dashboard.html 提取最新一次回测的三档净值曲线，
导出为精简 JSON，供 quant 网站 /quant 页展示（与桌面仪表盘同源）。

用法：python3 scripts/export_dashboard_curves.py
输出：data/dashboard_tiers.json（约几十 KB）
"""
import json
import os
import sys

SRC = "/home/lan/桌面/stock_predict/research/dashboard.html"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "dashboard_tiers.json")


def parse_data(path):
    src = open(path, encoding="utf-8").read()
    i = src.index("const DATA =")
    start = src.index("{", i)
    depth = 0
    end = None
    for j in range(start, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    return json.loads(src[start:end])


def main():
    if not os.path.exists(SRC):
        print(f"缺少 {SRC}")
        return 1
    data = parse_data(SRC)
    runs = data.get("runs") or []
    if not runs:
        print("dashboard 无 runs")
        return 1
    run = runs[0]
    tiers = ((run.get("results") or {}).get("all") or {}).get("tiers") or {}
    out = {
        "source": SRC,
        "generated": data.get("generated"),
        "version": run.get("version"),
        "ts": run.get("ts"),
        "segment": run.get("segment"),
        "data_end": run.get("data_end"),
        "tiers": {},
    }
    bench_added = False
    for name in ("稳健", "均衡", "激进"):
        t = tiers.get(name)
        if not t or not t.get("curve"):
            continue
        rec = {"curve": t["curve"], "dates": t.get("curve_dates") or [],
               "ann": t.get("ann"), "mdd": t.get("mdd"),
               "sharpe": t.get("sharpe"), "trades": t.get("trades"),
               "benchmark": t.get("benchmark")}
        if t.get("bench_curve") and t.get("bench_curve_dates"):
            rec["bench_curve"] = t["bench_curve"]
            rec["bench_dates"] = t["bench_curve_dates"]
            bench_added = True
        out["tiers"][name] = rec
    if not bench_added:
        # 退而求其次：用稳健档的基准
        for name, t in tiers.items():
            if t.get("bench_curve") and t.get("bench_curve_dates"):
                out["bench_curve"] = t["bench_curve"]
                out["bench_dates"] = t["bench_curve_dates"]
                out["benchmark"] = t.get("benchmark")
                break
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    size = os.path.getsize(OUT)
    print(f"导出 {OUT}（{size/1024:.0f}KB）")
    print(f"版本 {out['version']} / {out['ts']} / segment={out['segment']} / end={out['data_end']}")
    for name, t in out["tiers"].items():
        print(f"  {name}: {len(t['curve'])} 点 年化={t.get('ann')} mdd={t.get('mdd')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
