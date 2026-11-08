#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成美股 P1 上线产物 (桌面 sm701 -> ai-quant/quant).

产物:
  backtest/results/multi-us-long.json   网站 /quant 净值曲线 (2023 起)
  logs/us_p1_signal.csv                 最新交易日 503 只概率 (sim 可选)
  vendors/sm701/production/us_p1/       2026 折模型 (h20/h60)

口径:
- 2023-2025 用 walk_us_p1.py 的 walk-forward 样本外预测;
- 2026 用 us_p1_full_h60_test2026.txt (训练 2018-2024 / 早停 2025);
- 组合: 每 5 个交易日按预测 Top5 等权调仓, 单边成本 0.1%, 收盘价记账;
- 曲线只用于网站横向展示, 与 sim 的 T+1 成交口径略有差异.

用法:
  python3 scripts/gen_us_p1_artifacts.py
  python3 scripts/gen_us_p1_artifacts.py --copy-quant  # scp 产物到 100.126.30.51
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SM701 = os.path.expanduser("~/桌面/sm701")
OUT = os.path.join(BASE, "backtest", "results")
SIG = os.path.join(BASE, "logs", "us_p1_signal.csv")
MODEL_DIR = os.path.join(BASE, "vendors", "sm701", "production", "us_p1")
QUANT = "lan@100.126.30.51"
TOP_K = 5
REBALANCE = 5
COST = 0.001
START = "2023-01-01"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _p1_predictions():
    """2023-2026 样本外预测 (walk file + 2026 折模型补最近 3 个月)."""
    import pandas as pd
    sys.path.insert(0, SM701)
    import train_us_p0 as p0  # noqa

    old_feats = json.load(open(os.path.join(
        SM701, "production", "features_tech.json")))["features"]
    full_feats = old_feats + ["ret120", "ret250", "mom12_1",
                              "dist_high250", "vol120", "dvolr20",
                              "r_mom12_1", "r_dist_high250"]
    log("加载 qfq 数据并计算 full 48 维特征 ...")
    df = p0.load_bars()
    X = p0.compute_new_feats(df)[full_feats]
    log(f"特征 {X.shape[0]:,} 行 x {X.shape[1]} 列")

    # 2023-2026 walk-forward 样本外预测 (h60 折文件)
    parts = []
    for y in (2023, 2024, 2025, 2026):
        p = os.path.join(SM701, "markets_output",
                         f"us_p1_pred_h60_test{y}.csv.gz")
        if os.path.exists(p):
            d = pd.read_csv(p)
            parts.append(d[["date", "code", "p"]])
    walk = pd.concat(parts, ignore_index=True) if parts else None

    # 2026 折模型: 补 walk 文件末尾缺失的最近 h60 天, 并产出最新信号
    import lightgbm as lgb
    mpath = os.path.join(SM701, "markets_output",
                         "us_p1_full_h60_test2026.txt")
    booster = lgb.Booster(model_file=mpath)
    names = list(booster.feature_name() or full_feats)
    recent = df[df["date"] >= "2026-01-01"].copy()
    Xr = X.loc[recent.index, names]
    recent["p"] = booster.predict(Xr)
    recent = recent[["date", "code", "p"]]

    if walk is not None:
        # walk 优先 (真正的 2026 OOS); 仅用模型补 walk 最后日期之后
        last = walk["date"].max()
        extra = recent[recent["date"] > last]
        pred = pd.concat([walk, extra], ignore_index=True)
    else:
        pred = recent
    pred = pred.drop_duplicates(["date", "code"], keep="last")
    pred = pred.sort_values(["date", "code"]).reset_index(drop=True)
    return df, pred


def _equity(df, pred):
    """每 REBALANCE 日 TopK 等权, 收盘价记账, 0.1% 单边换手成本."""
    import numpy as np
    import pandas as pd
    px = df.pivot(index="date", columns="code", values="close").sort_index()
    dates = [d for d in sorted(pred["date"].unique()) if d >= START]
    dates = [d for d in dates if d in px.index]
    by_date = {d: g for d, g in pred.groupby("date")}
    nav, pos, prev, eq, nreb = 1.0, {}, None, [], 0
    for i, d in enumerate(dates):
        if pos and prev is not None:
            r = 0.0
            for c, w in pos.items():
                try:
                    p0 = float(px.at[prev, c])
                    p1 = float(px.at[d, c])
                except (KeyError, ValueError, TypeError):
                    continue
                if p0 > 0 and p1 > 0:
                    r += w * (p1 / p0 - 1.0)
            nav *= 1.0 + r
        if i % REBALANCE == 0:
            g = by_date.get(d)
            sel = (g.nlargest(TOP_K, "p")["code"].tolist()
                   if g is not None and len(g) else [])
            new = {c: 1.0 / len(sel) for c in sel} if sel else {}
            old = set(pos)
            turn = (len(set(new) - old) + len(old - set(new))) / max(
                1, max(len(old), len(new)))
            if turn:
                nav *= 1.0 - COST * turn
            pos = new
            nreb += 1
        eq.append([d, round(nav * 10000.0, 2)])
        prev = d
    if not eq:
        return None
    peak, mdd = eq[0][1], 0.0
    for _, v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return {
        "market": "us", "label": "美股", "thr": 0.0, "topk": TOP_K,
        "rebalance": REBALANCE, "start": eq[0][0], "end": eq[-1][0],
        "days": len(eq), "ret": round(eq[-1][1] / eq[0][1] - 1.0, 6),
        "mdd": round(mdd, 4), "equity": eq,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": "us_p1_full_h60_test2026.txt",
        "note": "P1 walk-forward h60 Top5 周度调仓; 成本0.1%单边",
    }


def _signal_csv(pred):
    import pandas as pd
    last = pred["date"].max()
    d = pred[pred["date"] == last].copy()
    d = d.sort_values("p", ascending=False)
    d["code"] = "us:" + d["code"].astype(str)
    d = d.rename(columns={"p": "prob"})[["date", "code", "prob"]]
    return d, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy-quant", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(os.path.dirname(SIG), exist_ok=True)

    df, pred = _p1_predictions()
    out = _equity(df, pred)
    if not out:
        raise SystemExit("回测曲线为空")
    p = os.path.join(OUT, "multi-us-long.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    log(f"曲线 {out['start']}~{out['end']} 收益 {out['ret']*100:+.1f}% "
        f"MDD {out['mdd']*100:.1f}% -> {p}")

    sig, last = _signal_csv(pred)
    sig.to_csv(SIG, index=False, float_format="%.6f")
    log(f"信号 {last} {len(sig)} 只 -> {SIG}")

    os.makedirs(MODEL_DIR, exist_ok=True)
    for fn in ("us_p1_full_h60_test2026.txt",
               "us_p1_full_h20_test2026.txt"):
        src = os.path.join(SM701, "markets_output", fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(MODEL_DIR, fn))
    log(f"模型 -> {MODEL_DIR}")

    if a.copy_quant:
        for local, remote in (
                (p, "~/ai-quant/backtest/results/multi-us-long.json"),
                (SIG, "~/ai-quant/logs/us_p1_signal.csv")):
            subprocess.run(["scp", "-q", "-i",
                            os.path.expanduser("~/.ssh/id_ed25519_quant"),
                            local, f"{QUANT}:{remote}"], check=True)
        log("已 scp 到 quant")


if __name__ == "__main__":
    main()
