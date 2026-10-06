"""factors.technical: numpy 向量化指标 (Pi 零重依赖, 只有 numpy).

来源:
- 本地 data/features/technical.py 的 MA5/MA20/趋势思想 (自研).
- market-sniper/market_sniper/indicators.py 的向量化写法
  (MA/EMA/MACD/BOLL/KDJ/RSI/ATR/量比等, 自研, 见 vendors/sniper/).
- chaodi_strategies/et_engine/indicators.py 的 TDX 语义
  (MA/EMA/BOLL/KDJ/RSI/ATR Wilder, 自研, 见 vendors/chaodi/).

本文件只保留回测/选股最高频的 12 列, 全部 O(n) 向量化,
避免原 TechnicalFeature 逐点 statistics.stdev 的 O(n^2):
  ma5/ma20/ema12/ema26/macd_dif/macd_dea/macd_hist/
  rsi14/atr14/boll_up/boll_mid/boll_low/vol_ratio
输入为 close/high/low/vol 的 list 或 numpy 数组, 输出等长 float list,
前置不足周期为 None (与 CLI _composite_precompute 的因果一致: 只用 t 及以前).
"""
import math

try:
    import numpy as np
    _HAS_NP = True
except Exception:
    np = None
    _HAS_NP = False


def _ema_np(a, n):
    k = 2.0 / (n + 1.0)
    out = np.empty_like(a, dtype=float)
    out[0] = a[0]
    for i in range(1, len(a)):
        out[i] = a[i] * k + out[i - 1] * (1 - k)
    return out


def _ma_np(a, n):
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        cs = np.cumsum(np.insert(a, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _rsi_wilder_np(close, n=14):
    d = np.diff(close, prepend=close[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = np.empty_like(close, dtype=float)
    ad = np.empty_like(close, dtype=float)
    au[0], ad[0] = up[0], dn[0]
    for i in range(1, len(close)):
        au[i] = (au[i - 1] * (n - 1) + up[i]) / n
        ad[i] = (ad[i - 1] * (n - 1) + dn[i]) / n
    rs = np.divide(au, ad, out=np.zeros_like(au), where=ad != 0)
    rsi = 100 - 100 / (1 + rs)
    rsi[ad == 0] = 100.0
    return rsi


def _atr_wilder_np(high, low, close, n=14):
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev),
                                           np.abs(low - prev)))
    atr = np.empty_like(tr)
    atr[0] = tr[0]
    for i in range(1, len(tr)):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def compute(closes, highs=None, lows=None, vols=None):
    """批量算 12 列指标. 返回 {col: [float|None]}."""
    if not closes or len(closes) < 2:
        return {}
    if not _HAS_NP:
        # 无 numpy 退化: 只算 MA5/MA20/趋势 (兼容极简 Pi 环境)
        def ma(a, n):
            return sum(a[-n:]) / n if len(a) >= n else a[-1]
        c = float(closes[-1])
        return {"ma5": [None] * (len(closes) - 1) + [ma(closes, 5)],
                "ma20": [None] * (len(closes) - 1) + [ma(closes, 20)],
                "close": list(closes)}
    c = np.asarray(closes, dtype=float)
    h = np.asarray(highs if highs is not None else closes, dtype=float)
    lo = np.asarray(lows if lows is not None else closes, dtype=float)
    v = np.asarray(vols if vols is not None else [1.0] * len(c), dtype=float)
    ma5 = _ma_np(c, 5)
    ma20 = _ma_np(c, 20)
    e12 = _ema_np(c, 12)
    e26 = _ema_np(c, 26)
    dif = e12 - e26
    dea = _ema_np(dif, 9)
    hist = dif - dea
    rsi = _rsi_wilder_np(c, 14)
    atr = _atr_wilder_np(h, lo, c, 14)
    mid = _ma_np(c, 20)
    std = np.array([np.std(c[max(0, i - 19):i + 1], ddof=1)
                    if i >= 1 else 0.0 for i in range(len(c))])
    up = mid + 2 * std
    low_b = mid - 2 * std
    vma5 = _ma_np(v, 5)
    vr = np.divide(v, vma5, out=np.ones_like(v), where=vma5 > 0)

    def clean(a):
        return [None if (x is None or (isinstance(x, float)
                        and (math.isnan(x) or math.isinf(x)))) else float(x)
                for x in a]
    return {"ma5": clean(ma5), "ma20": clean(ma20),
            "ema12": clean(e12), "ema26": clean(e26),
            "macd_dif": clean(dif), "macd_dea": clean(dea),
            "macd_hist": clean(hist), "rsi14": clean(rsi),
            "atr14": clean(atr), "boll_up": clean(up),
            "boll_mid": clean(mid), "boll_low": clean(low_b),
            "vol_ratio": clean(vr)}
