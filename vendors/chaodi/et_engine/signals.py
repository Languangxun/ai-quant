# -*- coding: utf-8 -*-
"""signal_* 信号层（与服务端逐根对拍）。

已对拍：
    ma_golden/dead_5_20、ma_golden_20_60、macd_golden/dead、ma20_breakout/breakdown、
    boll_breakout_upper/breakdown_lower、volume_surge、n_day_high/low（60 日）、
    limit_up/down、consecutive_limit_ups/downs、broken_limit_up
口径存疑（策略未用，做近似）：limit_down_recovery
"""
import numpy as np

from .data import limit_pct


def _shift(a, n=1):
    out = np.full(len(a), np.nan)
    if n < len(a):
        out[n:] = a[:-n]
    return out


def _cross_up(a, b):
    out = (a > b) & (_shift(a, 1) <= _shift(b, 1))
    out[0] = False
    return np.nan_to_num(out).astype(bool)


def _cross_dn(a, b):
    out = (a < b) & (_shift(a, 1) >= _shift(b, 1))
    out[0] = False
    return np.nan_to_num(out).astype(bool)


def _rolling_prev_max(a, n):
    """前 n 根（不含当日）最大值。"""
    from numpy.lib.stride_tricks import sliding_window_view
    a = np.asarray(a, dtype=np.float64)
    m = len(a)
    out = np.full(m, np.nan)
    if m < 2:
        return out
    acc = np.maximum.accumulate(a)
    upto = min(n, m)
    out[1:upto] = acc[:upto - 1]
    if m > n:
        w = sliding_window_view(a, n)
        out[n:] = w[:m - n].max(axis=1)
    return out


def _rolling_prev_min(a, n):
    from numpy.lib.stride_tricks import sliding_window_view
    a = np.asarray(a, dtype=np.float64)
    m = len(a)
    out = np.full(m, np.nan)
    if m < 2:
        return out
    acc = np.minimum.accumulate(a)
    upto = min(n, m)
    out[1:upto] = acc[:upto - 1]
    if m > n:
        w = sliding_window_view(a, n)
        out[n:] = w[:m - n].min(axis=1)
    return out


def _streak(flags):
    out = np.zeros(len(flags), dtype=np.int64)
    run = 0
    for i, f in enumerate(flags):
        run = run + 1 if f else 0
        out[i] = run
    return out


def compute(bars, ind, symbol=None, name=""):
    """在 indicators.compute 的结果上追加信号列，返回同一 dict。"""
    c = ind["close"]
    h = ind["high"]
    low = ind["low"]
    v = ind["volume"]
    n = len(c)
    lp = limit_pct(symbol or bars.get("symbol", ""), name)

    ind["signal_ma_golden_5_20"] = _cross_up(ind["ma5"], ind["ma20"])
    ind["signal_ma_dead_5_20"] = _cross_dn(ind["ma5"], ind["ma20"])
    ind["signal_ma_golden_20_60"] = _cross_up(ind["ma20"], ind["ma60"])
    ind["signal_macd_golden"] = _cross_up(ind["macd_dif"], ind["macd_dea"])
    ind["signal_macd_dead"] = _cross_dn(ind["macd_dif"], ind["macd_dea"])
    ind["signal_ma20_breakout"] = _cross_up(c, ind["ma20"])
    ind["signal_ma20_breakdown"] = _cross_dn(c, ind["ma20"])
    ind["signal_boll_breakout_upper"] = c > ind["boll_upper"]
    ind["signal_boll_breakdown_lower"] = c < ind["boll_lower"]

    ind["signal_n_day_high"] = np.nan_to_num(
        c >= _rolling_prev_max(c, 60)).astype(bool)
    ind["signal_n_day_low"] = np.nan_to_num(
        c <= _rolling_prev_min(c, 60)).astype(bool)
    ind["signal_volume_surge"] = np.nan_to_num(
        v > 2.0 * ind["vol_ma5"]).astype(bool)

    # 涨跌停（用（乘法前复权）价近似；除权日与真实口径可能有 1 日偏差）
    prev = ind["prev_close"]
    up_price = np.round(prev * (1.0 + lp), 2)
    dn_price = np.round(prev * (1.0 - lp), 2)
    limit_up = np.nan_to_num(c >= up_price - 1e-9).astype(bool)
    limit_dn = np.nan_to_num(c <= dn_price + 1e-9).astype(bool)
    limit_up[0] = limit_dn[0] = False
    ind["signal_limit_up"] = limit_up
    ind["signal_limit_down"] = limit_dn
    ind["consecutive_limit_ups"] = _streak(limit_up)
    ind["consecutive_limit_downs"] = _streak(limit_dn)
    # 炸板：盘中触及涨停但收盘未封住
    ind["signal_broken_limit_up"] = np.nan_to_num(
        (h >= up_price - 1e-9) & (c < up_price - 1e-9)).astype(bool)
    # 跌停反包（近似：昨日跌停且今日收阳/收涨）
    ind["signal_limit_down_recovery"] = np.nan_to_num(
        _shift(limit_dn, 1).astype(bool) & (c > prev)).astype(bool)

    ind["limit_pct"] = np.full(n, lp)
    ind["turnover_rate"] = np.full(n, np.nan)   # 由 universe/scan 注入
    return ind
