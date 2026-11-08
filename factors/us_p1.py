"""factors.us_p1: 美股 P1 48 维特征 builder (qfq + 收益波动率 + 美元成交额 + 长周期).

来源: ~/桌面/sm701/train_us_p0.py / walk_us_p1.py (2026-11-08)
- 训练/验证口径: us_daily_qfq 前复权, 2018-2023 训练, 2024-2025 早停,
  walk-forward 2026 折模型用于实盘 (us_p1_full_h60_test2026.txt);
- 特征 48 维 = 原 tech40 (vol 改为收益率标准差, logvol 改为美元成交额)
  + ret120/ret250/mom12_1/dist_high250/vol120/dvolr20 + 对应截面 rank;
- 截面 rank 必须在同一交易日的全市场 (标普500) 上计算, 本模块按
  bars[-1].date 分组做 pct rank, 缺失保留 NaN (LightGBM 原生缺失).

注意: 与 A 股 44 维 sm701 口径不同, 仅用于 us: 且模型存在时;
缺模型/缺 qfq 数据时上层自动回退旧的 production 三周期模型.
"""
import math

import numpy as np

FEATURES_US_P1 = [
    "ret1", "ret5", "ret10", "ret20", "ret60",
    "ma5r", "ma10r", "ma20r", "ma60r", "ma5_20", "ma20_60",
    "vol20", "vol60", "volr", "rsi6", "rsi14",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "bollpos", "hilo20", "atr_n",
    "amp", "gap", "cpos",
    "vr5", "vr20", "logvol", "updays20",
    "r_ret5", "r_ret20", "r_ret60", "r_ma20r", "r_rsi14",
    "r_vr20", "r_vol20", "r_logvol",
    "ret120", "ret250", "mom12_1", "dist_high250", "vol120", "dvolr20",
    "r_mom12_1", "r_dist_high250",
]
RANK_KEYS = ["ret5", "ret20", "ret60", "ma20r", "rsi14", "vr20",
             "vol20", "logvol", "mom12_1", "dist_high250"]
MIN_BARS = 260


def _nan():
    return float("nan")


def _last(x):
    try:
        v = float(x[-1])
        return v if math.isfinite(v) else _nan()
    except Exception:
        return _nan()


def _roll_mean(x, n):
    out = np.full(len(x), np.nan, dtype=float)
    if len(x) >= n:
        out[n - 1:] = np.convolve(x, np.ones(n) / n, mode="valid")
    return out


def _roll_std(x, n):
    out = np.full(len(x), np.nan, dtype=float)
    if len(x) >= n:
        sw = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = sw.std(axis=1, ddof=1)
    return out


def _roll_min(x, n):
    out = np.full(len(x), np.nan, dtype=float)
    if len(x) >= n:
        sw = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = sw.min(axis=1)
    return out


def _roll_max(x, n):
    out = np.full(len(x), np.nan, dtype=float)
    if len(x) >= n:
        sw = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = sw.max(axis=1)
    return out


def _ema(x, span):
    k = 2.0 / (span + 1.0)
    out = np.empty(len(x), dtype=float)
    if not len(x):
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1.0 - k)
    return out


def _ewm_com(x, com=2.0):
    a = 1.0 / (1.0 + com)
    out = np.empty(len(x), dtype=float)
    if not len(x):
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * a + out[i - 1] * (1.0 - a)
    return out


def _pct(a, b):
    try:
        if b and math.isfinite(b):
            return a / b - 1.0
    except Exception:
        pass
    return _nan()


def _series(bars):
    c = np.array([float(getattr(b, "close", 0) or 0) for b in bars],
                 dtype=float)
    o = np.array([float(getattr(b, "open", 0) or 0) for b in bars],
                 dtype=float)
    h = np.array([float(getattr(b, "high", 0) or 0) for b in bars],
                 dtype=float)
    lo = np.array([float(getattr(b, "low", 0) or 0) for b in bars],
                  dtype=float)
    v = np.array([float(getattr(b, "vol", 0) or 0) for b in bars],
                 dtype=float)
    raw = np.array([float(getattr(b, "raw_close", 0) or 0) for b in bars],
                   dtype=float)
    raw = np.where(raw > 0, raw, c)
    return c, o, h, lo, v, raw


def _raw_row(bars):
    """单只 -> 48维原始值 (r_* 先留空, 由 build_rows 截面补)."""
    n = len(bars)
    if n < MIN_BARS:
        return None
    c, o, h, lo, v, raw = _series(bars)
    if not (c[-1] > 0):
        return None
    r = {k: _nan() for k in FEATURES_US_P1}

    ret1 = np.full(n, np.nan, dtype=float)
    ret1[1:] = c[1:] / c[:-1] - 1.0
    for k, nn in (("ret1", 1), ("ret5", 5), ("ret10", 10),
                  ("ret20", 20), ("ret60", 60), ("ret120", 120),
                  ("ret250", 250)):
        if n > nn and c[-nn - 1] > 0:
            r[k] = c[-1] / c[-nn - 1] - 1.0

    ma = {k: _last(_roll_mean(c, k)) for k in (5, 10, 20, 60)}
    if ma[5] == ma[5]:
        r["ma5r"] = _pct(c[-1], ma[5])
    if ma[10] == ma[10]:
        r["ma10r"] = _pct(c[-1], ma[10])
    if ma[20] == ma[20]:
        r["ma20r"] = _pct(c[-1], ma[20])
    if ma[60] == ma[60]:
        r["ma60r"] = _pct(c[-1], ma[60])
    if ma[5] == ma[5] and ma[20] == ma[20]:
        r["ma5_20"] = _pct(ma[5], ma[20])
    if ma[20] == ma[20] and ma[60] == ma[60]:
        r["ma20_60"] = _pct(ma[20], ma[60])

    # 修正: 收益率波动率 (ddof=1), 不是收盘价标准差
    std20 = _last(_roll_std(ret1, 20))
    std60 = _last(_roll_std(ret1, 60))
    std120 = _last(_roll_std(ret1, 120))
    r["vol20"], r["vol60"], r["vol120"] = std20, std60, std120
    if std20 == std20 and std60 == std60:
        r["volr"] = std20 / (std60 + 1e-9)

    up = np.where(ret1 > 0, ret1, 0.0)
    dn = np.where(ret1 < 0, -ret1, 0.0)
    for nn, key in ((6, "rsi6"), (14, "rsi14")):
        fu = _last(_roll_mean(up, nn))
        fd = _last(_roll_mean(dn, nn))
        if fu == fu and fd == fd:
            r[key] = 100.0 * fu / (fu + fd + 1e-9)

    e12 = _ema(c, 12)
    e26 = _ema(c, 26)
    dif = e12 - e26
    dea = _ema(dif, 9)
    cc = c[-1] if c[-1] else 1.0
    r["macd_dif"] = dif[-1] / cc
    r["macd_dea"] = dea[-1] / cc
    r["macd_hist"] = (dif[-1] - dea[-1]) * 2.0 / cc

    llv = _roll_min(lo, 9)
    hhv = _roll_max(h, 9)
    rsv = (c - llv) / (hhv - llv + 1e-9) * 100.0
    rsv = np.nan_to_num(rsv, nan=50.0)
    k_all = _ewm_com(rsv, 2.0)
    d_all = _ewm_com(k_all, 2.0)
    r["kdj_k"], r["kdj_d"] = k_all[-1], d_all[-1]
    r["kdj_j"] = 3.0 * k_all[-1] - 2.0 * d_all[-1]

    pstd20 = _last(_roll_std(c, 20))
    if pstd20 == pstd20 and ma[20] == ma[20]:
        r["bollpos"] = (c[-1] - ma[20]) / (2.0 * pstd20 + 1e-9)
    cmin20 = _last(_roll_min(c, 20))
    cmax20 = _last(_roll_max(c, 20))
    if cmin20 == cmin20 and cmax20 == cmax20:
        r["hilo20"] = (c[-1] - cmin20) / (cmax20 - cmin20 + 1e-9)

    prev = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum.reduce([np.abs(h - lo), np.abs(h - prev),
                            np.abs(lo - prev)])
    atr = _last(_roll_mean(tr, 14))
    if atr == atr:
        r["atr_n"] = atr / cc
    if prev[-1] == prev[-1] and prev[-1] > 0:
        r["amp"] = (h[-1] - lo[-1]) / prev[-1]
        r["gap"] = o[-1] / prev[-1] - 1.0
    r["cpos"] = (c[-1] - lo[-1]) / (h[-1] - lo[-1] + 1e-9)

    # 美元成交额 (未复权 close * 成交量; 缺 raw_close 时用 qfq close)
    dvol = raw * v
    dv5 = _last(_roll_mean(dvol, 5))
    dv20 = _last(_roll_mean(dvol, 20))
    if dv5 == dv5 and dv5 > 0:
        r["vr5"] = dvol[-1] / (dv5 + 1e-9)
    if dv20 == dv20 and dv20 > 0:
        r["vr20"] = dvol[-1] / (dv20 + 1e-9)
        r["dvolr20"] = dvol[-1] / (dv20 + 1e-9) - 1.0
    if dvol[-1] >= 0:
        r["logvol"] = math.log1p(dvol[-1])

    upd = np.where(np.isnan(ret1), np.nan, (ret1 > 0).astype(float))
    r["updays20"] = _last(_roll_mean(upd, 20))

    if n > 21 and n > 252 and c[-253] > 0:
        r["mom12_1"] = c[-22] / c[-253] - 1.0
    hh250 = _last(_roll_max(h, 250))
    if hh250 == hh250 and hh250 > 0:
        r["dist_high250"] = c[-1] / hh250 - 1.0
    return r


def _pct_rank(values):
    """numpy 版 pct rank (平均秩, NaN 保留), 与 pandas rank(pct=True) 同向."""
    a = np.asarray(values, dtype=float)
    out = np.full(len(a), np.nan, dtype=float)
    ok = np.isfinite(a)
    if not ok.any():
        return out
    x = a[ok]
    order = np.argsort(x, kind="mergesort")
    sx = x[order]
    ranks = np.empty(len(x), dtype=float)
    i = 0
    while i < len(x):
        j = i + 1
        while j < len(x) and sx[j] == sx[i]:
            j += 1
        avg = (i + 1 + j) / 2.0  # 1-based 平均秩
        ranks[order[i:j]] = avg
        i = j
    out[ok] = ranks / len(x)
    return out


def build_rows(bars_by_code, min_bars=MIN_BARS):
    """{code: [Bar]} -> {code: 48维 dict}. 不足 min_bars 的代码剔除.

    截面 rank 按 bars[-1].date 分组; 同一天只应传入该市场全 universe
    (实盘/回测均传标普500全量), 否则 rank 与训练口径会偏.
    """
    dates = {}
    rows = {}
    for code, bars in (bars_by_code or {}).items():
        if not bars or len(bars) < min_bars:
            continue
        row = _raw_row(bars)
        if row is None:
            continue
        d = str(getattr(bars[-1], "date", ""))
        rows[code] = row
        dates.setdefault(d, []).append(code)
    for d, codes in dates.items():
        for key in RANK_KEYS:
            vals = [rows[c].get(key, _nan()) for c in codes]
            rk = _pct_rank(vals)
            for c, v in zip(codes, rk):
                rows[c]["r_" + key] = float(v)
    return rows
