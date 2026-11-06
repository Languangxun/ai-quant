from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import data as data_mod
from . import fusion, metrics, models, report
from .audit import prepare
from .portfolio import SimConfig, simulate


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_snapshot(preds: pd.DataFrame, ctx: dict,
                   smooth_days: int = 1,
                   exclude: set | None = None) -> pd.DataFrame:
    df = ctx["df"]
    base = df[["date", "code", "close", "hist_len", "turnover20"]].copy()
    base["code"] = base["code"].astype(str)
    base["beta"] = ctx["beta"].to_numpy(dtype="float64")
    snap = preds.merge(base, on=["date", "code"], how="left")
    snap = snap.dropna(subset=["close"])
    if exclude:
        snap = snap[~snap["code"].isin(exclude)]
    snap = snap.sort_values(["code", "date"])
    if smooth_days > 1:
        for col in ("alpha", "risk"):
            snap[col] = snap.groupby("code", sort=False)[col].transform(
                lambda s: s.rolling(smooth_days, min_periods=1).mean())
    return snap


def _variant_cfgs(cfg: dict) -> dict[str, dict]:
    base = copy.deepcopy(cfg["fusion"])
    variants = {"fused": base}
    reg_off = copy.deepcopy(base["regime"])
    reg_off["enabled"] = False
    variants["alpha_only"] = {**copy.deepcopy(base), "risk_penalty": 0.0,
                              "risk_gate_prob": 1.01, "regime": reg_off}
    variants["risk_overlay"] = {**copy.deepcopy(base), "risk_penalty": 0.0}
    return variants


def make_books(by_date: dict, rebal_dates: list[str], cfg: dict,
               fusion_cfg: dict, leverage: float,
               ret_df: pd.DataFrame) -> dict:
    p = cfg["portfolio"]
    port = {**p}
    lev = min(float(leverage), float(p["max_leverage"]))
    max_scale = max(1.0, float(p["max_leverage"]) / max(lev, 1e-9))
    books = {}
    prev_long: set = set()
    prev_short: set = set()
    for d in rebal_dates:
        g = by_date.get(d)
        if g is None or g.empty:
            continue
        book = fusion.build_book(g, fusion_cfg, port, lev,
                                 prev_long=prev_long, prev_short=prev_short)
        if not book.weights:
            continue
        books[d] = book
        prev_long = {c for c, w in book.weights.items() if w > 0}
        prev_short = {c for c, w in book.weights.items() if w < 0}

    # regime：用调仓日的中位数风险在回看窗口内的分位（自适应，不依赖概率标定）
    reg_cfg = fusion_cfg["regime"]
    lookback = max(5, int(reg_cfg.get("lookback", 40)))
    ordered = sorted(books)
    meds = np.array([books[d].meta.get("risk_median", np.nan) for d in ordered])
    for i, d in enumerate(ordered):
        book = books[d]
        window = meds[max(0, i - lookback + 1):i + 1]
        window = window[np.isfinite(window)]
        if len(window) >= 12 and np.isfinite(meds[i]):
            pct = float((window <= meds[i]).mean())
        else:
            pct = np.nan
        rs = fusion.regime_scale(pct, reg_cfg)

        # --- 自适应 vol_target：最近 60 日年化波动，只降不升 ---
        # 计算最近 60 日等权账本收益的年化波动率
        codes = [c for c in book.weights if c in ret_df.columns]
        if len(codes) >= 4:
            sub = ret_df.loc[:d, codes].tail(60)
            if len(sub) >= 30:
                r_eq = sub.mean(axis=1)  # 等权收益
                vol_60 = float(np.std(r_eq, ddof=1) * np.sqrt(365.0))
                if not np.isfinite(vol_60) or vol_60 <= 1e-6:
                    vol_60 = None
            else:
                vol_60 = None
        else:
            vol_60 = None

        if vol_60 is not None:
            # 阈值：< 25% -> 25%；> 45% -> 30%；否则不变。保持 allow_upscale=False 的上限行为。
            if vol_60 < 0.25:
                target_vol_adj = 0.25
            elif vol_60 > 0.45:
                target_vol_adj = 0.30
            else:
                target_vol_adj = float(p["vol_target"])
        else:
            target_vol_adj = float(p["vol_target"])

        vs = fusion.book_vol_scale(
            book.weights, ret_df, d, target_vol_adj,
            float(p["min_vol_scale"]), max_scale,
            allow_upscale=bool(p.get("vol_upscale", False)))
        scale = rs * vs
        book.weights = {c: w * scale for c, w in book.weights.items()}
        gross = sum(abs(w) for w in book.weights.values())
        if gross > float(p["max_leverage"]):
            k = float(p["max_leverage"]) / gross
            book.weights = {c: w * k for c, w in book.weights.items()}
            gross *= k
        book.meta.update({"regime_pct": pct, "regime_scale": rs,
                          "vol_scale": vs, "gross": gross,
                          "vol_60h": vol_60 if vol_60 is not None else np.nan,
                          "target_vol_adj": target_vol_adj})
    return books


def run(cfg: dict, source: str | None = None, years: list[int] | None = None,
        start: str | None = None, end: str | None = None,
        leverage: float | None = None, ablations: bool | None = None,
        out_dir: str | None = None,
        reuse_from: str | None = None) -> dict:
    t0 = time.time()
    out = Path(out_dir or cfg["output"]["dir"])
    (out / "models").mkdir(parents=True, exist_ok=True)
    cfg = dict(cfg)
    cfg["output"] = {**cfg["output"], "dir": str(out)}

    ctx = prepare(cfg)
    df = ctx["df"]
    log(f"数据: {df.attrs['n_codes']} 币 / {df.attrs['n_dates']} 日 / {len(df):,} 行")

    src = source or cfg["model"]["source"]
    yrs = years or cfg["model"]["test_years"]
    if reuse_from:
        src_dir = Path(reuse_from)
        log(f"复用预测: {src_dir}")
        preds = pd.read_csv(src_dir / "predictions_oos.csv.gz")
        diag_df = pd.read_csv(src_dir / "model_diagnostics.csv")
        preds.to_csv(out / "predictions_oos.csv.gz", index=False,
                     compression="gzip")
        diag_df.to_csv(out / "model_diagnostics.csv", index=False)
    else:
        log(f"生成样本外预测 (source={src}, years={yrs}) ...")
        if src == "walkforward":
            preds, diag = models.walkforward(ctx, cfg, yrs)
        elif src == "pretrained":
            preds, diag = models.pretrained(ctx, cfg, yrs)
        else:
            raise ValueError(f"未知 source: {src}")
        preds.to_csv(out / "predictions_oos.csv.gz", index=False,
                     compression="gzip")
        diag_df = pd.DataFrame(diag)
        diag_df.to_csv(out / "model_diagnostics.csv", index=False)

    snap = build_snapshot(preds, ctx,
                          int(cfg["portfolio"].get("signal_smooth_days", 1)),
                          set(cfg["data"].get("exclude_codes") or []))
    by_date = {d: g for d, g in snap.groupby("date")}
    px = data_mod.price_pivots(df)
    ret_df = px["close"].pct_change()

    all_dates = [str(d) for d in px["close"].index]
    pred_dates = sorted(preds["date"].unique())
    sim_start = max(start or cfg["backtest"]["start"], pred_dates[0])
    sim_end = min(end or cfg["backtest"]["end"] or all_dates[-1],
                  pred_dates[-1], all_dates[-1])
    sim_dates = [d for d in all_dates if sim_start <= d <= sim_end]
    if len(sim_dates) < 60:
        raise RuntimeError(f"回测区间过短: {sim_start}~{sim_end} ({len(sim_dates)} 日)")
    step = max(1, int(cfg["portfolio"]["rebalance_days"]))
    rebal = sim_dates[::step]
    log(f"回测 {sim_dates[0]} ~ {sim_dates[-1]} ({len(sim_dates)} 日), 调仓 {len(rebal)} 次")

    p = cfg["portfolio"]
    lev = float(leverage if leverage is not None else p["leverage"])
    simcfg = SimConfig(capital=float(p["capital"]),
                       fee_rate=float(p["fee_rate"]),
                       slippage=float(p["slippage"]),
                       funding_daily=float(p["funding_daily"]),
                       maintenance_rate=float(p["maintenance_rate"]),
                       liq_fee=float(p["liq_fee"]),
                       liq_cooldown_days=int(p["liq_cooldown_days"]),
                       max_leverage=float(p["max_leverage"]),
                       funding_mode=str(p.get("funding_mode", "net")))

    variants = _variant_cfgs(cfg)
    primary_mode = str(p["hedge_mode"])
    plan: list[tuple[str, dict, str]] = []
    if not (ablations if ablations is not None else cfg["backtest"]["ablations"]):
        variants = {"fused": variants["fused"]}
        plan = [("fused", variants["fused"], primary_mode)]
    else:
        for name, fus in variants.items():
            if name == "fused":
                plan.append(("fused", fus, primary_mode))
                if primary_mode != "none":
                    plan.append(("fused_long", fus, "none"))
                if primary_mode != "cross_section":
                    plan.append(("fused_xs", fus, "cross_section"))
            else:
                plan.append((f"{name}_long", fus, "none"))

    results = {}
    summary_rows = []
    for name, fus, hm in plan:
        cfg_v = {**cfg, "portfolio": {**p, "hedge_mode": hm}}
        books = make_books(by_date, rebal, cfg_v, fus, lev, ret_df)
        daily, trades, stats = simulate(px, books, sim_dates, simcfg)
        m = metrics.summary_row(name, daily, stats)
        summary_rows.append(m)
        results[name] = {"daily": daily, "trades": trades, "stats": stats,
                         "metrics": m, "n_books": len(books)}
        daily.to_csv(out / f"daily_{name}.csv", index=False)
        log(f"  {name:18s} ann={m['ann_return']:+.2%} sharpe={m['sharpe']:.2f} "
            f"mdd={m['mdd']:.2%} gross={m['avg_gross']:.2f} liq={m['liquidations']}")

    summary = pd.DataFrame(summary_rows)

    # 杠杆敏感度：关闭波动目标，单独观察杠杆本身的收益/回撤/强平边界
    lev_rows = []
    for lv in cfg["backtest"]["leverage_sweep"]:
        cfg_v = {**cfg, "portfolio": {**p, "hedge_mode": primary_mode,
                                      "vol_target": 0.0}}
        books = make_books(by_date, rebal, cfg_v, variants["fused"], float(lv), ret_df)
        daily, trades, stats = simulate(px, books, sim_dates, simcfg)
        row = metrics.summary_row(f"fixed_L{lv:g}", daily, stats)
        row["target_leverage"] = float(lv)
        lev_rows.append(row)
        log(f"  杠杆 {lv:g}x: ann={row['ann_return']:+.2%} sharpe={row['sharpe']:.2f} "
            f"mdd={row['mdd']:.2%} liq={row['liquidations']}")
    lev_df = pd.DataFrame(lev_rows)

    # 基准：BTC 买入持有 / 等权币池
    close = px["close"].loc[sim_dates]
    btc = close["BTCUSDT"] if "BTCUSDT" in close.columns else close.iloc[:, 0]
    btc_eq = float(p["capital"]) * btc / btc.iloc[0]
    ew_ret = close.pct_change().mean(axis=1).fillna(0.0)
    ew_eq = float(p["capital"]) * (1.0 + ew_ret).cumprod()
    bench = pd.DataFrame({"date": sim_dates,
                          "btc": btc_eq.to_numpy(),
                          "equal_weight": ew_eq.to_numpy()})
    bench_m = {"btc": metrics.perf(pd.Series(bench["btc"].to_numpy())),
               "equal_weight": metrics.perf(pd.Series(bench["equal_weight"].to_numpy()))}

    primary = "fused" if "fused" in results else summary["variant"].iloc[0]
    primary_daily = results[primary]["daily"]
    yearly = metrics.yearly(primary_daily["equity"], primary_daily["date"])

    latest = pd.DataFrame()
    if rebal:
        cfg_v = {**cfg, "portfolio": {**p, "hedge_mode": primary_mode}}
        last_book = make_books(by_date, [rebal[-1]], cfg_v, variants["fused"],
                               lev, ret_df).get(rebal[-1])
        g = by_date.get(rebal[-1])
        if last_book is not None and g is not None:
            latest = g[g["code"].isin(last_book.weights)].copy()
            latest["weight"] = latest["code"].map(last_book.weights)
            latest["notional"] = latest["weight"] * float(p["capital"])
            latest = latest[["date", "code", "close", "alpha", "risk", "weight",
                             "notional"]].sort_values("weight", ascending=False)

    summary.to_csv(out / "backtest_summary.csv", index=False)
    yearly.to_csv(out / "yearly_primary.csv", index=False)
    lev_df.to_csv(out / "leverage_sweep.csv", index=False)
    primary_daily.to_csv(out / "daily_primary.csv", index=False)
    latest.to_csv(out / "latest_targets.csv", index=False)
    merged = primary_daily.merge(bench, on="date", how="left")
    merged.to_csv(out / "equity_curve.csv", index=False)
    for name, r in results.items():
        r["trades"].to_csv(out / f"trades_{name}.csv", index=False)

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": src,
        "data_start": str(df["date"].min()),
        "data_end": str(df["date"].max()),
        "n_codes": int(df.attrs["n_codes"]),
        "sim_start": sim_dates[0],
        "sim_end": sim_dates[-1],
        "n_days": len(sim_dates),
        "rebalances": len(rebal),
        "horizon": int(cfg["model"]["horizon"]),
        "leverage": lev,
        "capital": float(p["capital"]),
        "live_broker": False,
    }
    with open(out / "run_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    res = {"meta": meta, "summary": summary, "yearly": yearly,
           "leverage": lev_df, "diagnostics": diag_df,
           "benchmark": bench_m, "latest": latest,
           "primary_daily": primary_daily, "results": results,
           "primary": primary}
    report.write_report(out / "report.md", res, cfg)
    log(f"完成，用时 {time.time() - t0:.1f}s，输出 {out}")
    return res
