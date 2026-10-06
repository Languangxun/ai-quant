# -*- coding: utf-8 -*-
"""LightGBM 特征工程：训练 / 推理共用（纯 numpy，无 sklearn 依赖）。

build_features(bars, peer=None) -> (names, X, dates)
    bars : db.load_daily_bars 输出
    peer : dict {date: {feature: value}} 行业指数特征（可选，训练端注入；
           推理端由 lgbm_model 按日期对齐后注入）
    X    : (n, len(names)) float64，含 NaN（LightGBM 原生处理）
"""
from __future__ import annotations

import numpy as np

FEATURE_NAMES = [
    "ret1", "ret5", "ret20",
    "ma5_ratio", "ma10_ratio", "ma20_ratio", "ma30_ratio", "ma60_ratio",
    "macd_dif_pct", "macd_dea_pct", "macd_hist_pct",
    "rsi_6", "rsi_14", "rsi_24",
    "kdj_k", "kdj_d", "kdj_j",
    "boll_b", "boll_bw", "atr_pct",
    "vol_ratio", "amplitude", "up_days_5",
    "peer_ret5", "peer_ret20", "peer_breadth", "rs5", "rs20",
]

PEER_NAMES = ["peer_ret5", "peer_ret20", "peer_breadth"]


def _shift(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    if n < len(x):
        out[n:] = x[:-n]
    return out


def build_features(bars: dict, peer: dict | None = None,
                   peer_default: dict | None = None):
    """返回 (names, X, dates)。peer 缺失的日期用 peer_default（最近值）。"""
    from market_sniper import indicators as ind_mod

    d = ind_mod.compute(bars)
    c = np.asarray(d["close"], dtype=np.float64)
    dates = [str(x) for x in (bars.get("dates") or bars.get("ts") or [])]
    n = len(c)
    out: dict[str, np.ndarray] = {}

    def put(name, arr):
        out[name] = np.asarray(arr, dtype=np.float64)

    with np.errstate(invalid="ignore", divide="ignore"):
        put("ret1", d["change_pct"])
        put("ret5", c / _shift(c, 5) - 1.0)
        put("ret20", c / _shift(c, 20) - 1.0)
        for k in (5, 10, 20, 30, 60):
            put(f"ma{k}_ratio", c / np.asarray(d[f"ma{k}"]) - 1.0)
        for k in ("macd_dif", "macd_dea", "macd_hist"):
            put(f"{k}_pct", np.asarray(d[k]) / c)
        for k in ("rsi_6", "rsi_14", "rsi_24"):
            put(k, d[k])
        for k in ("kdj_k", "kdj_d", "kdj_j"):
            put(k, d[k])
        span = np.asarray(d["boll_upper"]) - np.asarray(d["boll_lower"])
        put("boll_b", (c - np.asarray(d["boll_lower"])) / span)
        put("boll_bw", span / np.asarray(d["boll_mid"]))
        put("atr_pct", np.asarray(d["atr_14"]) / c)
        put("vol_ratio", np.clip(d["vol_ratio_5d"], 0, 20))
        put("amplitude", d["amplitude"])
        put("up_days_5", np.asarray(d["up_days_5"]) / 5.0)
        for k in PEER_NAMES:
            put(k, np.full(n, np.nan))
        put("rs5", np.full(n, np.nan))
        put("rs20", np.full(n, np.nan))

    if peer is not None or peer_default is not None:
        default = peer_default or {}
        for i, dt in enumerate(dates):
            row = (peer or {}).get(dt) or default
            for k in PEER_NAMES:
                v = row.get(k)
                if v is not None and np.isfinite(v):
                    out[k][i] = float(v)
            if np.isfinite(out["ret5"][i]) and np.isfinite(out["peer_ret5"][i]):
                out["rs5"][i] = out["ret5"][i] - out["peer_ret5"][i]
            if np.isfinite(out["ret20"][i]) and np.isfinite(out["peer_ret20"][i]):
                out["rs20"][i] = out["ret20"][i] - out["peer_ret20"][i]

    X = np.column_stack([out[k] for k in FEATURE_NAMES])
    return list(FEATURE_NAMES), X, dates
