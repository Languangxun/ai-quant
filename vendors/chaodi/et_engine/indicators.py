# -*- coding: utf-8 -*-
"""TDX 语义指标引擎（numpy 向量化，与服务端口径逐项对拍）。

已对拍一致的指标：
    ma/ema/vol_ma/macd/boll(ddof=1)/kdj(seed=50)/rsi(Wilder)/atr(Wilder)
    wr(取负)/momentum/change_pct/change_amount/amplitude/annual_vol_20d(ddof=1)
    up_days_5/down_days_5/vol_ratio_5d/ema&ma slope
服务端口径存疑（策略未使用，做近似）：cci_14/obv/sar/high_60d/vwap_deviation/
    ma20_deviation/ma20_atr_dist/atr_percentile_120/plus_di/minus_di/adx_14
"""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

try:
    from scipy.signal import lfilter as _lfilter
except Exception:                                       # pragma: no cover
    _lfilter = None

SQRT252 = float(np.sqrt(252.0))


# ---------------- 基础工具 ----------------

def _cumsum_ma(a, n):
    """滚动均值（前 n-1 根为 NaN）。"""
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        cs = np.cumsum(np.insert(np.nan_to_num(a, nan=0.0), 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def ma(a, n):
    return _cumsum_ma(a, n)


def _recur(a, alpha):
    """一阶递推 y[i] = alpha*x[i] + (1-alpha)*y[i-1]，y[0]=x[0]。
    有 scipy 时用 lfilter（C 速度），否则退回 Python 循环。"""
    a = np.asarray(a, dtype=np.float64)
    n = len(a)
    if n == 0:
        return a.copy()
    if _lfilter is not None and not np.isnan(a).any():
        zi = (1.0 - alpha) * a[0]
        y, _ = _lfilter([alpha], [1.0, -(1.0 - alpha)], a, zi=[zi])
        return y
    out = np.full(n, np.nan)
    prev = np.nan
    for i in range(n):
        x = a[i]
        if np.isnan(x):
            continue
        prev = x if np.isnan(prev) else alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def _recur_seed(a, alpha, y0):
    """y[i] = alpha*x[i] + (1-alpha)*y[i-1]，初始 y[-1]=y0。"""
    a = np.asarray(a, dtype=np.float64)
    if len(a) == 0:
        return a.copy()
    if _lfilter is not None and not np.isnan(a).any():
        zi = (1.0 - alpha) * y0
        y, _ = _lfilter([alpha], [1.0, -(1.0 - alpha)], a, zi=[zi])
        return y
    out = np.empty(len(a))
    prev = y0
    for i, x in enumerate(a):
        prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def ema(a, n):
    """通达信 EMA(X,N)：alpha=2/(N+1)，首值起递推（与 pandas ewm(adjust=False) 等价）。"""
    return _recur(a, 2.0 / (n + 1.0))


def sma_tdx(a, n, m=1.0):
    """通达信 SMA(X,N,M)：Y = (M*X + (N-M)*Y')/N，首值起递推。"""
    return _recur(a, m / n)


def rolling_max(a, n, shift=0):
    """截至 i-shift 的前 n 根最大值（默认含当日）。"""
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

def compute(bars, lite=False):
    """bars: data.load_bars() 的返回。返回 {列名: np.ndarray}。

    lite=True 时只计算策略/信号所需列（全市场扫描用，速度约快 3 倍）。
    """
    o = bars["open"]
    h = bars["high"]
    low = bars["low"]
    c = bars["close"]
    v = bars["volume"]
    n = len(c)

    d = {}
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
        # 通达信 KDJ 以 50 为种子（首根有效 RSV 前 K=D=50）
        k_seed = np.full(n, np.nan)
        d_seed = np.full(n, np.nan)
        prev_k = prev_d = 50.0
        for i in range(n):
            if np.isnan(rsv[i]):
                continue
            prev_k = (rsv[i] + 2.0 * prev_k) / 3.0
            prev_d = (prev_k + 2.0 * prev_d) / 3.0
            k_seed[i], d_seed[i] = prev_k, prev_d
        d["kdj_k"], d["kdj_d"] = k_seed, d_seed
        d["kdj_j"] = 3.0 * k_seed - 2.0 * d_seed

    # RSI（Wilder）
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

    # ATR（Wilder TR14）
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

    if not lite:
        # WR（服务端取负）
        for k in (14, 28):
            hh = rolling_max(h, k)
            ll = rolling_min(low, k)
            with np.errstate(invalid="ignore", divide="ignore"):
                d[f"wr_{k}"] = (-(hh - c)
                                / np.where(hh - ll == 0, np.nan, hh - ll)
                                * 100.0)

    # 动量 / 波动
    for k in (3, 5, 10, 20, 30, 60):
        with np.errstate(invalid="ignore", divide="ignore"):
            d[f"momentum_{k}d"] = c / _shift(c, k) - 1.0
    ret = np.full(n, np.nan)
    ret[1:] = c[1:] / c[:-1] - 1.0
    av = np.full(n, np.nan)
    if n >= 20:
        w = sliding_window_view(ret, 20)
        with np.errstate(invalid="ignore"):
            av[19:] = np.nanstd(w, axis=1, ddof=1) * SQRT252
    d["annual_vol_20d"] = av

    if not lite:
        # 斜率（服务端口径：(x[i] - x[i-5]) / close[i]）
        for k, col in ((5, "ma5_slope_5d"), (20, "ma20_slope_5d"),
                       (60, "ma60_slope_5d")):
            base = _shift(d[f"ma{k}"], 5)
            with np.errstate(invalid="ignore", divide="ignore"):
                d[col] = (d[f"ma{k}"] - base) / c
        for k, col in ((10, "ema10_slope_5d"), (20, "ema20_slope_5d")):
            base = _shift(d[f"ema{k}"], 5)
            with np.errstate(invalid="ignore", divide="ignore"):
                d[col] = (d[f"ema{k}"] - base) / c

    # 涨跌天数（最近 5 个涨跌日，含当日）
    up_days = np.full(n, np.nan)
    dn_days = np.full(n, np.nan)
    if n >= 6:
        w = np.diff(sliding_window_view(c, 6), axis=1)
        up_days[5:] = (w > 0).sum(axis=1)
        dn_days[5:] = (w < 0).sum(axis=1)
    d["up_days_5"], d["down_days_5"] = up_days, dn_days

    # 均价（策略 basic_filter/评分用）
    amount = v * 100.0 * c
    d["amount"] = amount

    if lite:
        return d

    # 高低点（60 日）
    d["high_60d"] = rolling_max(h, 60)
    d["low_60d"] = rolling_min(low, 60)

    # 偏离（服务端口径存疑，策略未用，做近似）
    with np.errstate(invalid="ignore", divide="ignore"):
        d["ma20_deviation"] = (c / d["ma20"] - 1.0) * 100.0
        d["ma20_atr_dist"] = (c - d["ma20"]) / atr
    d["vwap_deviation"] = np.full(n, np.nan)

    # OBV（近似：累计带符号成交量）
    sign = np.sign(np.diff(c, prepend=c[0]))
    sign[0] = 0.0
    d["obv"] = np.cumsum(sign * v)
    d["obv_ma5"] = ma(d["obv"], 5)

    # CCI（近似，向量化）
    tp = (h + low + c) / 3.0
    matp = ma(tp, 14)
    mad = np.full(n, np.nan)
    if n >= 14:
        w = sliding_window_view(tp, 14)
        mad[13:] = np.abs(w - matp[13:, None]).mean(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        d["cci_14"] = (tp - matp) / np.where(mad == 0, np.nan, 0.015 * mad)

    # DMI（Wilder，近似）
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    plus_dm[1:] = np.where((h[1:] - h[:-1]) > (low[:-1] - low[1:]),
                           np.maximum(h[1:] - h[:-1], 0.0), 0.0)
    minus_dm[1:] = np.where((low[:-1] - low[1:]) > (h[1:] - h[:-1]),
                            np.maximum(low[:-1] - low[1:], 0.0), 0.0)
    atr_s = np.full(n, np.nan)
    pdm_s = np.full(n, np.nan)
    mdm_s = np.full(n, np.nan)
    if n > 14:
        atr_s[14] = np.nansum(tr[1:15])
        pdm_s[14] = plus_dm[1:15].sum()
        mdm_s[14] = minus_dm[1:15].sum()
        for i in range(15, n):
            atr_s[i] = atr_s[i - 1] - atr_s[i - 1] / 14 + tr[i]
            pdm_s[i] = pdm_s[i - 1] - pdm_s[i - 1] / 14 + plus_dm[i]
            mdm_s[i] = mdm_s[i - 1] - mdm_s[i - 1] / 14 + minus_dm[i]
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = 100.0 * pdm_s / np.where(atr_s == 0, np.nan, atr_s)
        mdi = 100.0 * mdm_s / np.where(atr_s == 0, np.nan, atr_s)
        dx = (100.0 * np.abs(pdi - mdi)
              / np.where((pdi + mdi) == 0, np.nan, pdi + mdi))
    d["plus_di"], d["minus_di"] = pdi, mdi
    adx = np.full(n, np.nan)
    idx = np.where(~np.isnan(dx))[0]
    if len(idx) >= 14:
        start = idx[0]
        adx[start + 13] = np.nanmean(dx[start:start + 14])
        for i in range(start + 14, n):
            if np.isnan(dx[i]):
                continue
            adx[i] = (adx[i - 1] * 13 + dx[i]) / 14.0
    d["adx_14"] = adx

    # SAR（近似，策略未用）
    d["sar"] = _sar(h, low)

    # ATR 分位（120 日）
    pct = np.full(n, np.nan)
    if n >= 120:
        w = sliding_window_view(atr, 120)
        valid = ~np.isnan(w)
        total = valid.sum(axis=1)
        cnt = ((w <= atr[119:][:, None]) & valid).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            vals = cnt / np.where(total == 0, np.nan, total) * 100.0
        pct[119:] = vals
    d["atr_percentile_120"] = pct

    return d


def _sar(high, low, af_step=0.02, af_max=0.2):
    n = len(high)
    sar = np.full(n, np.nan)
    if n < 2:
        return sar
    rising = high[1] >= high[0]
    af = af_step
    ep = high[0] if rising else low[0]
    sar[0] = low[0] if rising else high[0]
    for i in range(1, n):
        prev = sar[i - 1]
        cur = prev + af * (ep - prev)
        if rising:
            lo1 = low[i - 1]
            lo2 = low[i - 2] if i >= 2 else lo1
            cur = min(cur, lo1, lo2)
            if low[i] < cur:
                rising = False
                cur = ep
                ep = low[i]
                af = af_step
            elif high[i] > ep:
                ep = high[i]
                af = min(af + af_step, af_max)
        else:
            hi1 = high[i - 1]
            hi2 = high[i - 2] if i >= 2 else hi1
            cur = max(cur, hi1, hi2)
            if high[i] > cur:
                rising = True
                cur = ep
                ep = high[i]
                af = af_step
            elif low[i] < ep:
                ep = low[i]
                af = min(af + af_step, af_max)
        sar[i] = cur
    return sar
