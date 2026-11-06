from __future__ import annotations

import numpy as np
import pandas as pd

from .portfolio import Book


def _rank_pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average")


def build_book(snap: pd.DataFrame, fusion_cfg: dict, port_cfg: dict,
               leverage: float, prev_long: set | None = None,
               prev_short: set | None = None) -> Book:
    """把一个交易日的截面快照转成带符号的目标权重（权益占比）。

    hedge_mode:
      btc           多头顶部 alpha，并按账本 beta 做空 BTC（与残差标签一致）
      none          纯多头杠杆
      cross_section 多头 top + 空头 bottom（截面多空，不再对冲 BTC）
    """
    df = snap.dropna(subset=["alpha", "risk"]).copy()
    df = df[(df["hist_len"] >= int(port_cfg["min_hist"]))
            & (df["turnover20"] >= float(port_cfg["min_turnover_usdt"]))]
    n = len(df)
    if n < 10:
        return Book({}, {"reason": "thin", "n": n})

    cap_n = max(3, int(n * float(port_cfg["book_fraction"])))
    n_long = min(int(port_cfg["n_long"]), cap_n)
    hedge_mode = str(port_cfg.get("hedge_mode", "cross_section"))
    n_short = (min(int(port_cfg["n_short"]), cap_n)
               if hedge_mode == "cross_section" else 0)

    pa = _rank_pct(df["alpha"]) * 2.0 - 1.0
    pr = _rank_pct(df["risk"]) * 2.0 - 1.0
    df["fused"] = pa - float(fusion_cfg["risk_penalty"]) * pr

    gate = float(fusion_cfg["risk_gate_prob"])
    q = float(fusion_cfg.get("risk_gate_quantile", 0.0) or 0.0)
    if 0.0 < q < 1.0:
        gate = min(gate, float(df["risk"].quantile(q)))

    pool = df[df["risk"] < gate].sort_values("fused", ascending=False)
    buffer = max(0, int(fusion_cfg.get("hysteresis", 0) or 0))
    if prev_long and buffer > 0:
        keep = pool[pool["code"].isin(prev_long)].head(n_long + buffer)
        new = pool[~pool["code"].isin(keep["code"])].head(n_long)
        longs = (pd.concat([keep, new]).drop_duplicates("code").head(n_long)
                 if len(keep) else pool.head(n_long))
    else:
        longs = pool.head(n_long)
    if len(longs) == 0:
        return Book({}, {"reason": "no_long", "n": n,
                         "risk_median": float(df["risk"].median())})

    weights: dict[str, float] = {}
    max_w = float(port_cfg["max_weight"])
    meta: dict = {"n": n, "n_long": int(len(longs)), "risk_median":
                  float(df["risk"].median()),
                  "hedge_mode": hedge_mode}

    if hedge_mode == "btc":
        b = 1.0
        if "beta" in longs.columns:
            b = float(np.nanmean(longs["beta"].to_numpy(dtype="float64")))
        b = float(np.clip(b if np.isfinite(b) else 1.0, 0.2, 2.5))
        long_gross = float(leverage) / (1.0 + b)
        w_long = min(long_gross / max(len(longs), 1), max_w)
        for c in longs["code"].astype(str):
            weights[c] = w_long
        long_sum = float(sum(weights.values()))
        btc = str(port_cfg.get("btc_code", "BTCUSDT"))
        weights[btc] = weights.get(btc, 0.0) - b * long_sum
        meta.update({"n_short": 1 if long_sum > 0 else 0, "beta_book": b,
                     "beta_hedge": -b * long_sum})
    elif hedge_mode == "cross_section":
        rest = df[~df["code"].isin(longs["code"])].sort_values("fused")
        if prev_short and buffer > 0 and n_short:
            keep_s = rest[rest["code"].isin(prev_short)].head(n_short + buffer)
            new_s = rest[~rest["code"].isin(keep_s["code"])].head(n_short)
            shorts = (pd.concat([keep_s, new_s]).drop_duplicates("code")
                      .head(n_short) if len(keep_s) else rest.head(n_short))
        else:
            shorts = rest.head(n_short) if n_short else df.iloc[0:0]
        side_gross = float(leverage) / 2.0 if len(shorts) else float(leverage)
        w_long = min(side_gross / max(len(longs), 1), max_w)
        w_short = (min(side_gross / max(len(shorts), 1), max_w)
                   if len(shorts) else 0.0)
        for c in longs["code"].astype(str):
            weights[c] = weights.get(c, 0.0) + w_long
        for c in shorts["code"].astype(str):
            weights[c] = weights.get(c, 0.0) - w_short
        meta.update({"n_short": int(len(shorts))})
    else:  # none：纯多头
        w_long = min(float(leverage) / max(len(longs), 1), max_w)
        for c in longs["code"].astype(str):
            weights[c] = w_long
        meta.update({"n_short": 0})

    gross = float(sum(abs(v) for v in weights.values()))
    meta["gross"] = gross
    return Book(weights, meta)


def regime_scale(risk_pct: float, cfg: dict) -> float:
    """risk_pct = 当日截面 risk 中位数在过去窗口中的分位（0~1，自适应）。"""
    if not cfg.get("enabled", True) or not np.isfinite(risk_pct):
        return 1.0
    if risk_pct >= float(cfg.get("q_extreme", 0.95)):
        return float(cfg.get("scale_extreme", 0.4))
    if risk_pct >= float(cfg.get("q_high", 0.80)):
        return float(cfg.get("scale_high", 0.7))
    return 1.0


def book_vol_scale(weights: dict[str, float], ret_df: pd.DataFrame, asof: str,
                   target_vol: float, min_scale: float,
                   max_scale: float, window: int = 30,
                   allow_upscale: bool = False) -> float:
    """用所选币最近 window 天的等权多空收益估计账本波动，做目标波动缩放。

    默认只降杠杆不抬杠杆（allow_upscale=False），避免低波动期把 gross 悄悄放大。
    """
    if target_vol <= 0 or not weights:
        return 1.0
    codes = [c for c in weights if c in ret_df.columns]
    if len(codes) < 4:
        return 1.0
    sub = ret_df.loc[:asof, codes].tail(window)
    if len(sub) < 15:
        return 1.0
    sign = np.array([np.sign(weights[c]) for c in codes], dtype="float64")
    r = sub.to_numpy(dtype="float64", na_value=0.0)
    port = (r * sign).mean(axis=1)
    vol = float(np.std(port, ddof=1) * np.sqrt(365.0))
    if not np.isfinite(vol) or vol <= 1e-6:
        return 1.0
    upper = float(max_scale) if allow_upscale else 1.0
    return float(np.clip(float(target_vol) / vol, float(min_scale), upper))
