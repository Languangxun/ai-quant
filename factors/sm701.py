"""factors.sm701: sm701 44维通用特征 builder (多市场复用, 无 pandas 依赖).

来源: ~/桌面/sm701/lgbm_train_backtest.py _compute_feats (44维, 训练2018-2023).
本模块把 pandas groupby 逻辑降为纯 Python 单序列 O(n) 计算, 供
基金(NAV无量) / 港(hk:) / 美(us:) / 加密(crypto:) 直接复用:

- 输入: bars=[Bar...] 升序 (fund NAV 请先经 data.fund.bars 转为 OHLC, vol=0 可接受).
- 市场环境 mkt1/mkt5/mkt20/breadth5 与横截面 r_*: 有 universe 上下文时传入
  market_ctx={mkt1,mkt5,mkt20,breadth5, ranks:{k: pct}}, 无则回退 0 / 0.5.
- 输出: dict 44维 (缺失填 0.0, 与 LightGBM 训练时 fillna 口径一致).

注意域外推: 模型在 A股上训练, 港美加密基金只是暂代信号, 阈值建议
config/markets.yaml 按市场分别设 (默认 cn 0.55 / fund 0.52 / hk-us-crypto 0.55).
"""
import math

FEATURES_SM701 = [
    "ret1", "ret5", "ret10", "ret20", "ret60",
    "ma5r", "ma10r", "ma20r", "ma60r", "ma5_20", "ma20_60",
    "vol20", "vol60", "volr", "rsi6", "rsi14",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "bollpos", "hilo20", "atr_n",
    "vr5", "vr20", "logvol", "amp", "gap", "cpos", "updays20",
    "mkt1", "mkt5", "mkt20", "breadth5",
    "r_ret5", "r_ret20", "r_ret60", "r_ma20r", "r_rsi14",
    "r_vr20", "r_vol20", "r_logvol",
]

_R_KEYS = ["ret5", "ret20", "ret60", "ma20r", "rsi14", "vr20", "vol20", "logvol"]


def _f(x, d=0.0):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return d
        return v
    except Exception:
        return d


def _sma(vals, n, i):
    if i + 1 < n:
        return None
    s = 0.0
    for k in range(i - n + 1, i + 1):
        s += vals[k]
    return s / n


def _ema_series(vals, span):
    k = 2.0 / (span + 1.0)
    out = [0.0] * len(vals)
    out[0] = vals[0]
    for i in range(1, len(vals)):
        out[i] = vals[i] * k + out[i - 1] * (1 - k)
    return out


def _ewm_com_series(vals, com=2):
    a = 1.0 / (1.0 + com)
    out = [0.0] * len(vals)
    out[0] = vals[0]
    for i in range(1, len(vals)):
        out[i] = vals[i] * a + out[i - 1] * (1 - a)
    return out


def build_row(closes, highs=None, lows=None, vols=None, opens=None,
              market_ctx=None):
    """对单只序列算 T 日(尾) 44维. 长度<65 返回 None (与 LgbmProb 门槛一致)."""
    n = len(closes)
    if n < 65:
        return None
    c = [_f(x) for x in closes]
    h = [_f(x, c[i] if c[i] else 0.0) for i, x in
         enumerate(highs if highs is not None else closes)]
    lo = [_f(x, c[i] if c[i] else 0.0) for i, x in
          enumerate(lows if lows is not None else closes)]
    o = [_f(x, c[i] if c[i] else 0.0) for i, x in
         enumerate(opens if opens is not None else closes)]
    # fund NAV 无量: 用极小量避免除零, vr 回退 1.0, logvol=0
    raw_v = [_f(x) for x in (vols if vols is not None else [0.0] * n)]
    has_vol = any(v > 0 for v in raw_v)
    v = [x if x > 0 else 1e-9 for x in raw_v]
    i = n - 1

    def pct(a, b):
        return a / b - 1.0 if b else 0.0

    r = {}
    r["ret1"] = pct(c[i], c[i - 1]) if i >= 1 else 0.0
    r["ret5"] = pct(c[i], c[i - 5]) if i >= 5 else 0.0
    r["ret10"] = pct(c[i], c[i - 10]) if i >= 10 else 0.0
    r["ret20"] = pct(c[i], c[i - 20]) if i >= 20 else 0.0
    r["ret60"] = pct(c[i], c[i - 60]) if i >= 60 else 0.0

    ma5 = _sma(c, 5, i)
    ma10 = _sma(c, 10, i)
    ma20 = _sma(c, 20, i)
    ma60 = _sma(c, 60, i)
    r["ma5r"] = pct(c[i], ma5) if ma5 else 0.0
    r["ma10r"] = pct(c[i], ma10) if ma10 else 0.0
    r["ma20r"] = pct(c[i], ma20) if ma20 else 0.0
    r["ma60r"] = pct(c[i], ma60) if ma60 else 0.0
    r["ma5_20"] = pct(ma5, ma20) if ma5 and ma20 else 0.0
    r["ma20_60"] = pct(ma20, ma60) if ma20 and ma60 else 0.0

    # 波动: 20/60日收盘 std (sm701 原口径, 非年化)
    def _std(a, b):
        m = sum(c[a:b + 1]) / (b - a + 1)
        return math.sqrt(sum((x - m) ** 2 for x in c[a:b + 1]) / (b - a + 1))
    std20 = _std(i - 19, i) if i >= 19 else 0.0
    std60 = _std(i - 59, i) if i >= 59 else 0.0
    r["vol20"] = std20
    r["vol60"] = std60
    r["volr"] = std20 / (std60 + 1e-9) if std60 else 0.0

    # RSI: 简单滚动均值口径 (与 sm701 _rm 一致, 非 Wilder)
    def _rsi(nn):
        if i + 1 < nn + 1:
            return 50.0
        up = sum(max(c[k] - c[k - 1], 0.0) for k in range(i - nn + 1, i + 1)) / nn
        dn = sum(max(c[k - 1] - c[k], 0.0) for k in range(i - nn + 1, i + 1)) / nn
        if up + dn <= 0:
            return 50.0
        return 100.0 * up / (up + dn + 1e-9)
    r["rsi6"] = _rsi(6)
    r["rsi14"] = _rsi(14)

    # MACD: ema12/26, dea ewm9, 除以收盘归一
    e12 = _ema_series(c, 12)
    e26 = _ema_series(c, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = _ema_series(dif, 9)
    cc = c[i] if c[i] else 1.0
    r["macd_dif"] = dif[i] / cc
    r["macd_dea"] = dea[i] / cc
    r["macd_hist"] = (dif[i] - dea[i]) * 2.0 / cc

    # KDJ(9): RSV + ewm(com=2)
    llv = min(lo[max(0, i - 8):i + 1])
    hhv = max(h[max(0, i - 8):i + 1])
    rsv_all = []
    for k in range(n):
        ll = min(lo[max(0, k - 8):k + 1])
        hh = max(h[max(0, k - 8):k + 1])
        rsv_all.append((c[k] - ll) / (hh - ll + 1e-9) * 100.0)
    k_all = _ewm_com_series(rsv_all, 2)
    d_all = _ewm_com_series(k_all, 2)
    r["kdj_k"] = k_all[i]
    r["kdj_d"] = d_all[i]
    r["kdj_j"] = 3.0 * k_all[i] - 2.0 * d_all[i]

    # BOLL/位置
    cmin20 = min(c[i - 19:i + 1]) if i >= 19 else c[i]
    cmax20 = max(c[i - 19:i + 1]) if i >= 19 else c[i]
    r["bollpos"] = (c[i] - (ma20 or c[i])) / (2.0 * std20 + 1e-9) if std20 else 0.0
    r["hilo20"] = (c[i] - cmin20) / (cmax20 - cmin20 + 1e-9)

    # ATR/形态
    prev = c[i - 1] if i >= 1 else c[i]
    trs = []
    for k in range(max(1, i - 13), i + 1):
        pc = c[k - 1] if k >= 1 else c[k]
        trs.append(max(abs(h[k] - lo[k]), abs(h[k] - pc), abs(lo[k] - pc)))
    atr = sum(trs) / len(trs) if trs else 0.0
    r["atr_n"] = atr / (c[i] or 1.0)
    r["amp"] = (h[i] - lo[i]) / (prev or 1.0)
    r["gap"] = o[i] / (prev or 1.0) - 1.0 if prev else 0.0
    r["cpos"] = (c[i] - lo[i]) / (h[i] - lo[i] + 1e-9)

    # 量
    if has_vol:
        ma_v5 = sum(v[i - 4:i + 1]) / 5 if i >= 4 else v[i]
        ma_v20 = sum(v[i - 19:i + 1]) / 20 if i >= 19 else sum(v[:i + 1]) / (i + 1)
        r["vr5"] = v[i] / (ma_v5 + 1e-9)
        r["vr20"] = v[i] / (ma_v20 + 1e-9)
        r["logvol"] = math.log1p(raw_v[i])
    else:
        r["vr5"] = 1.0
        r["vr20"] = 1.0
        r["logvol"] = 0.0
    r["updays20"] = sum(1 for k in range(max(1, i - 19), i + 1)
                        if c[k] > c[k - 1]) / 20.0

    # 市场环境 + 横截面: 有 ctx 用 ctx, 无则中性回退
    # (mkt=0, breadth=0.5, rank=0.5; breadth 0.0 会被模型判为极端空头, 不可用0)
    ctx = market_ctx or {}
    for k in ("mkt1", "mkt5", "mkt20"):
        r[k] = _f(ctx.get(k, 0.0))
    r["breadth5"] = _f(ctx.get("breadth5", 0.5), 0.5)
    ranks = ctx.get("ranks") or {}
    for k in _R_KEYS:
        rk = "r_" + k
        r[rk] = _f(ranks.get(k, ranks.get(rk, 0.5)), 0.5)

    return {k: _f(r.get(k, 0.0)) for k in FEATURES_SM701}


def bars_to_row(bars, market_ctx=None):
    """[Bar] -> 44维 dict. 不足65根返回 None."""
    if not bars or len(bars) < 65:
        return None
    return build_row(
        [b.close for b in bars],
        [b.high for b in bars],
        [b.low for b in bars],
        [b.vol for b in bars],
        [b.open for b in bars],
        market_ctx,
    )


def dict_rows_to_row(rows, market_ctx=None):
    """[{date,open,high,low,close,vol}] -> 44维 dict."""
    if not rows or len(rows) < 65:
        return None
    return build_row(
        [r.get("close", 0) for r in rows],
        [r.get("high", 0) for r in rows],
        [r.get("low", 0) for r in rows],
        [r.get("vol", 0) for r in rows],
        [r.get("open", 0) for r in rows],
        market_ctx,
    )
