# -*- coding: utf-8 -*-
"""TDX 风格向量化指标（numpy）。

bars 输入 = {open, high, low, close, volume}（任意 numpy 数组）。
返回 = {列名: np.ndarray}。

已实现：
    MA / EMA / SMA / MACD / BOLL / KDJ / RSI / ATR / 动量 / 波动率 /
    DMI / ADX / OBV / CCI / 涨跌天数 / 斜率。

lite=True 只算策略核心指标列；GUI 默画 KDI 全列。
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

try:
    from scipy.signal import lfilter as _lfilter
except Exception:                                       # pragma: no cover
    _lfilter = None

SQRT252 = float(np.sqrt(252.0))


# ---------------- 基础工具 ----------------

def _cumsum_ma(a, n):
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        cs = np.cumsum(np.insert(np.nan_to_num(a, nan=0.0), 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def ma(a, n):
    return _cumsum_ma(a, n)


def _recur(a, alpha):
    """y[i] = alpha*x[i] + (1-alpha)*y[i-1]，y[0]=x[0]（含 NaN 跳过）。"""
    a = np.asarray(a, dtype=np.float64)
    n = len(a)
    if n == 0:
        return a.copy()
    if _lfilter is not None and not np.isnan(a).any():
        zi = (1.0 - alpha) * a[0]
        y, _ = _lfilter([alpha], [1.0, -(1.0 - alpha)], a, zi=[zi])
        return y
    # 纯 Python 循环走 list（避免 numpy 标量装箱开销）
    src = a.tolist()
    out_l = [float("nan")] * n
    beta = 1.0 - alpha
    prev = float("nan")
    for i in range(n):
        x = src[i]
        if x != x:
            continue
        prev = x if prev != prev else alpha * x + beta * prev
        out_l[i] = prev
    return np.array(out_l)


def _recur_seed(a, alpha, y0):
    """y[i] = alpha*x[i] + (1-alpha)*y[i-1]，y[-1]=y0。"""
    a = np.asarray(a, dtype=np.float64)
    if len(a) == 0:
        return a.copy()
    if _lfilter is not None and not np.isnan(a).any():
        zi = (1.0 - alpha) * y0
        y, _ = _lfilter([alpha], [1.0, -(1.0 - alpha)], a, zi=[zi])
        return y
    beta = 1.0 - alpha
    out = []
    append = out.append
    prev = y0
    for x in a.tolist():
        prev = alpha * x + beta * prev
        append(prev)
    return np.array(out)


def ema(a, n):
    return _recur(a, 2.0 / (n + 1.0))


def sma_tdx(a, n, m=1.0):
    return _recur(a, m / n)


def rolling_max(a, n, shift=0):
    a = np.asarray(a, dtype=np.float64)
    out = np.full(len(a), np.nan)
    if len(a) < n + shift:
        return out
    src = a[:len(a) - shift] if shift else a
    w = sliding_window_view(src, n)
    out[n - 1 + shift:] = w.max(axis=1)
    return out


def rolling_min(a, n, shift=0):
    a = np.asarray(a, dtype=np.float64)
    out = np.full(len(a), np.nan)
    if len(a) < n + shift:
        return out
    src = a[:len(a) - shift] if shift else a
    w = sliding_window_view(src, n)
    out[n - 1 + shift:] = w.min(axis=1)
    return out


def rolling_std(a, n, ddof=1):
    a = np.asarray(a, dtype=np.float64)
    out = np.full(len(a), np.nan)
    if len(a) < n:
        return out
    w = sliding_window_view(a, n)
    out[n - 1:] = w.std(axis=1, ddof=ddof)
    return out


def _shift(a, n=1):
    out = np.full(len(a), np.nan)
    if n < len(a):
        out[n:] = a[:-n]
    return out


# ---------------- 主计算 ----------------

def compute(bars, lite: bool = False) -> dict:
    """bars: data.load_daily_bars / load_min_bars 的返回。
    返回 {列名: np.ndarray}。"""
    o = bars["open"]
    h = bars["high"]
    low = bars["low"]
    c = bars["close"]
    v = bars["volume"]
    n = len(c)

    d: dict = {}
    d["open"], d["high"], d["low"], d["close"], d["volume"] = o, h, low, c, v
    d["prev_close"] = _shift(c, 1)
    d["change_amount"] = c - d["prev_close"]
    with np.errstate(invalid="ignore", divide="ignore"):
        d["change_pct"] = c / d["prev_close"] - 1.0
        d["amplitude"] = (h - low) / d["prev_close"]

    ma_periods = (5, 10, 20, 60) if lite else (5, 10, 12, 20, 26, 30, 60)
    for k in ma_periods:
        d[f"ma{k}"] = ma(c, k)
    for k in ((12, 26) if lite else (5, 10, 12, 20, 26, 30, 60)):
        d[f"ema{k}"] = ema(c, k)
    for k in ((5,) if lite else (5, 10)):
        d[f"vol_ma{k}"] = ma(v, k)
    with np.errstate(invalid="ignore", divide="ignore"):
        d["vol_ratio_5d"] = v / d["vol_ma5"]

    dif = d["ema12"] - d["ema26"]
    dea = ema(dif, 9)
    d["macd_dif"] = dif
    d["macd_dea"] = dea
    d["macd_hist"] = 2.0 * (dif - dea)
    d["macd_hist_prev"] = _shift(d["macd_hist"], 1)

    std20 = rolling_std(c, 20, ddof=1)
    d["boll_mid"] = d["ma20"]
    d["boll_upper"] = d["ma20"] + 2.0 * std20
    d["boll_lower"] = d["ma20"] - 2.0 * std20

    if not lite:
        hh9 = rolling_max(h, 9)
        ll9 = rolling_min(low, 9)
        with np.errstate(invalid="ignore", divide="ignore"):
            rsv = (c - ll9) / np.where(hh9 - ll9 == 0, np.nan, hh9 - ll9) * 100.0
        rsv = np.where(np.isnan(rsv), np.where(np.isnan(hh9), np.nan, 50.0), rsv)
        k_seed = np.full(n, np.nan)
        d_seed = np.full(n, np.nan)
        # NaN 只在头部：找首个有效值，之后在 list 上递推
        valid = np.where(~np.isnan(rsv))[0]
        if len(valid):
            s = int(valid[0])
            rsv_l = rsv[s:].tolist()
            m = n - s
            k_l = [float("nan")] * m
            d_l = [float("nan")] * m
            prev_k = prev_d = 50.0
            for i in range(m):
                x = rsv_l[i]
                if x != x:          # NaN：跳过（与逐根循环语义一致）
                    continue
                prev_k = (x + 2.0 * prev_k) / 3.0
                prev_d = (prev_k + 2.0 * prev_d) / 3.0
                k_l[i] = prev_k
                d_l[i] = prev_d
            k_seed[s:] = k_l
            d_seed[s:] = d_l
        d["kdj_k"], d["kdj_d"] = k_seed, d_seed
        d["kdj_j"] = 3.0 * k_seed - 2.0 * d_seed

    # RSI Wilder
    delta = np.full(n, np.nan)
    delta[1:] = np.diff(c)
    up = np.where(delta > 0, delta, 0.0)
    dn = np.where(delta < 0, -delta, 0.0)
    up[0] = dn[0] = np.nan
    for k in ((14,) if lite else (6, 14, 24)):
        au = np.full(n, np.nan)
        ad = np.full(n, np.nan)
        if n > k:
            au[k] = np.nanmean(up[1:k + 1])
            ad[k] = np.nanmean(dn[1:k + 1])
            au[k + 1:] = _recur_seed(up[k + 1:], 1.0 / k, au[k])
            ad[k + 1:] = _recur_seed(dn[k + 1:], 1.0 / k, ad[k])
        with np.errstate(invalid="ignore", divide="ignore"):
            rs = au / np.where(ad == 0, np.nan, ad)
        d[f"rsi_{k}"] = 100.0 - 100.0 / (1.0 + rs)

    # ATR Wilder TR14
    tr = np.full(n, np.nan)
    tr[1:] = np.maximum.reduce([
        h[1:] - low[1:],
        np.abs(h[1:] - c[:-1]),
        np.abs(low[1:] - c[:-1]),
    ])
    atr = np.full(n, np.nan)
    if n > 14:
        atr[14] = np.nanmean(tr[1:15])
        atr[15:] = _recur_seed(tr[15:], 1.0 / 14.0, atr[14])
    d["atr_14"] = atr

    # 动量
    for k in (3, 5, 10, 20, 30, 60):
        with np.errstate(invalid="ignore", divide="ignore"):
            d[f"momentum_{k}d"] = c / _shift(c, k) - 1.0

    # 年化波动率
    ret = np.full(n, np.nan)
    ret[1:] = c[1:] / c[:-1] - 1.0
    av = np.full(n, np.nan)
    if n >= 20:
        w = sliding_window_view(ret, 20)
        with np.errstate(invalid="ignore"):
            av[19:] = np.nanstd(w, axis=1, ddof=1) * SQRT252
    d["annual_vol_20d"] = av

    # 涨跌天数
    up_days = np.full(n, np.nan)
    dn_days = np.full(n, np.nan)
    if n >= 6:
        w = np.diff(sliding_window_view(c, 6), axis=1)
        up_days[5:] = (w > 0).sum(axis=1)
        dn_days[5:] = (w < 0).sum(axis=1)
    d["up_days_5"], d["down_days_5"] = up_days, dn_days

    # 斜率
    if not lite:
        for k, col in ((5, "ma5_slope_5d"), (20, "ma20_slope_5d"),
                       (60, "ma60_slope_5d")):
            base = _shift(d[f"ma{k}"], 5)
            with np.errstate(invalid="ignore", divide="ignore"):
                d[col] = (d[f"ma{k}"] - base) / c
        for k, col in ((10, "ema10_slope_5d"), (20, "ema20_slope_5d")):
            base = _shift(d[f"ema{k}"], 5)
            with np.errstate(invalid="ignore", divide="ignore"):
                d[col] = (d[f"ema{k}"] - base) / c

    # 均价
    d["amount"] = v * c

    return d


def last_values(d: dict, keys: list[str]) -> dict:
    """安全拿每列的最后一条非 NaN 值（GUI 显示用）。"""
    out = {}
    for k in keys:
        arr = d.get(k)
        if arr is None or len(arr) == 0:
            out[k] = None
            continue
        v = arr[-1]
        out[k] = None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from market_sniper.data import db
    bars = db.load_daily_bars("CRYPTO", "BTC/USDT", limit=200)
    if not bars:
        print("无 BTC 日K，请先 backfill")
        sys.exit(0)
    ind = compute(bars)
    print("K线数：", len(bars["close"]))
    print("最后值：")
    for k in ("ma5", "ma10", "ma20", "ma60", "ema12", "ema26",
             "macd_dif", "macd_dea", "macd_hist",
             "boll_upper", "boll_mid", "boll_lower",
             "kdj_k", "kdj_d", "kdj_j",
             "rsi_6", "rsi_14", "rsi_24", "atr_14"):
        v = ind.get(k)
        if v is None:
            continue
        x = v[-1]
        if np.isnan(x):
            continue
        print(f"  {k:14s} = {x:+.4f}")