# -*- coding: utf-8 -*-
"""通达信 VAR2 抄底体系（与 stock_chaodi_gui.py 同语义）。

公式：
    VAR1:=(MA(CLOSE,80)-MA(CLOSE,13)/3);
    VAR2:=(MA((CLOSE-VAR1)/VAR1,1));
    买点1: CROSS(VAR2,0) AND LOW/REF(HIGH,1)<1.012
    最佳点: COUNT(VAR2>REF(VAR2,1),3)=3 AND COUNT(VAR2<0,10)=10
            AND REF(VAR2,3)=LLV(VAR2,10)
    买点2: REF(VAR2,2)=LLV(VAR2,20) AND REF(VAR2,2)<0.071
            AND REF(VAR2,2)<REF(VAR2,1)
            AND NOT(REF(LOW,1)>REF(HIGH,2) AND LOW>REF(HIGH,1))
            AND CLOSE>REF(CLOSE,1)
    MMA:=EMA(VAR2,12)/1.428571;  MMB:=EMA(VAR2,3)
    快到底: LLV(MMB-MMA,12)>0 ? 0 : -30
    底初选股: CROSS(0,LLV(MMB-MMA,12))
    DIFF:(EMA(CLOSE,12)-EMA(CLOSE,26))/0.01; DEA:EMA(DIFF,9)
    MACD:=(DIFF-DEA)/0.5
    抄底: 快到底<0 AND CROSS(MACD,0)
"""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def _ma(a, n):
    out = np.full(a.shape, np.nan)
    if a.size >= n:
        cs = np.concatenate(([0.0], np.cumsum(a)))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _ema(a, n):
    """EMA(X,N)：从第一个有效值起递归，遇无效值中断并重置。"""
    out = np.full(a.shape, np.nan)
    k = 2.0 / (n + 1.0)
    e = None
    for i, x in enumerate(a.tolist()):
        if x != x:
            e = None
            continue
        e = x if e is None else x * k + e * (1 - k)
        out[i] = e
    return out


def _ref(a, k):
    out = np.full(a.shape, np.nan)
    if k <= 0:
        out[:] = a
    elif a.size > k:
        out[k:] = a[:-k]
    return out


def _llv(a, n):
    out = np.full(a.shape, np.nan)
    if a.size < n:
        return out
    sw = sliding_window_view(a, n)
    valid = ~np.isnan(sw).any(axis=1)
    mins = np.min(np.where(np.isnan(sw), np.inf, sw), axis=1)
    out[n - 1:] = np.where(valid, mins, np.nan)
    return out


def _count(cond, n):
    c = np.asarray(cond, dtype=float)
    out = np.full(c.shape, np.nan)
    if c.size < n:
        return out
    cs = np.concatenate(([0.0], np.cumsum(c)))
    out[n - 1:] = cs[n:] - cs[:-n]
    return out


def _cross(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.ndim == 0:
        a = np.full(b.shape, float(a))
    if b.ndim == 0:
        b = np.full(a.shape, float(b))
    return (a > b) & (_ref(a, 1) < _ref(b, 1))


def compute(bars):
    """bars: data.load_bars() 返回。返回与 bars 等长的信号/指标数组。"""
    C = np.asarray(bars["close"], dtype=float)
    H = np.asarray(bars["high"], dtype=float)
    L = np.asarray(bars["low"], dtype=float)
    n = len(C)
    if n == 0:
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        ma80 = _ma(C, 80)
        ma13 = _ma(C, 13)
        var1 = ma80 - ma13 / 3.0
        var2 = (C - var1) / var1
        v2_prev = _ref(var2, 1)
        buy1 = _cross(var2, np.zeros(n)) & ((L / _ref(H, 1)) < 1.012)
        rise3 = _count(var2 > v2_prev, 3) == 3
        neg10 = _count(var2 < 0, 10) == 10
        best = rise3 & neg10 & (_ref(var2, 3) == _llv(var2, 10))
        r1v2, r2v2 = _ref(var2, 1), _ref(var2, 2)
        gap_bad = (_ref(L, 1) > _ref(H, 2)) & (L > _ref(H, 1))
        buy2 = ((r2v2 == _llv(var2, 20)) & (r2v2 < 0.071) & (r2v2 < r1v2)
                & (~gap_bad) & (C > _ref(C, 1)))
        mma = _ema(var2, 12) / 1.428571
        mmb = _ema(var2, 3)
        llv12 = _llv(mmb - mma, 12)
        kdd = np.where(np.isnan(llv12), np.nan,
                       np.where(llv12 > 0, 0.0, -30.0))
        bottom = ~np.isnan(llv12) & (llv12 <= 0)
        di_cx = _cross(np.zeros(n), llv12)
        diff = (_ema(C, 12) - _ema(C, 26)) / 0.01
        dea = _ema(diff, 9)
        macd = (diff - dea) / 0.5
        chaodi = bottom & _cross(macd, np.zeros(n))
    return {"var1": var1, "var2": var2, "mma": mma, "mmb": mmb,
            "kdd": kdd, "bottom": bottom, "di_cx": di_cx,
            "diff": diff, "dea": dea, "macd": macd,
            "buy1": buy1, "buy2": buy2, "best": best, "chaodi": chaodi}
