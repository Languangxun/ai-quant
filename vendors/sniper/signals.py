# -*- coding: utf-8 -*-
"""信号计算层（示例算法：BOLL 回归 + ATR 止损参考）。

纯函数、无 GUI 依赖，供 /api/signal（浏览器插件）与 GUI 共用。
正式算法落地后按同签名注册进 SIGNAL_ALGOS 即可被 API 调用：

    def my_algo(bars: dict, **params) -> dict
        return {"algo": "my", "params": {...}, "signals": [...],
                "last_close": float, "bars": int, "position": str}

信号不是独立 BS 点，而是**仓位动作序列**（状态机输出）：
    美股/加密（可做空）：open_long 开多 / close_long 平多 /
                        open_short 开空 / close_short 平空
    HK（只做多头）    ：buy 买 / sell 卖
signals 按时间升序，相邻动作即仓位切换；"position" 为序列结束后的仓位。

示例策略逻辑：
    收盘跌破 BOLL 下轨后收回带内 → 转多（开多 / 空头则平空）；
    收盘升破 BOLL 上轨后收回带内 → 转空（开空 / 多头则平多；HK 只平多）。
"""
from __future__ import annotations

import datetime

import numpy as np

SIGNAL_ALGOS: dict = {}

# 动作展示元数据：dir=up 红色在上 / down 绿色在下；hollow=空心三角（平仓动作）
ACTION_META = {
    "open_long":   {"label": "开多", "dir": "up",   "hollow": False},
    "close_long":  {"label": "平多", "dir": "down", "hollow": True},
    "open_short":  {"label": "开空", "dir": "down", "hollow": False},
    "close_short": {"label": "平空", "dir": "up",   "hollow": True},
    "buy":         {"label": "买",   "dir": "up",   "hollow": False},
    "sell":        {"label": "卖",   "dir": "down", "hollow": False},
}


def action_label(side: str) -> str:
    return ACTION_META.get(side, {}).get("label", side)


def ts_to_ms(s) -> int | None:
    """'YYYY-MM-DD' 或 ISO（含 +00:00）→ epoch ms；失败返回 None。"""
    if not s:
        return None
    s = str(s)
    try:
        if len(s) == 10:
            dt = datetime.datetime.strptime(s, "%Y-%m-%d")
        else:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan, dtype=np.float64)
    if len(x) >= n:
        out[n - 1:] = np.convolve(x, np.ones(n, dtype=float) / n, mode="valid")
    return out


def _roll_std(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan, dtype=np.float64)
    if n <= 1:
        return out
    cs = np.cumsum(np.concatenate(([0.0], x * x)))
    sm = np.cumsum(np.concatenate(([0.0], x)))
    for i in range(n - 1, len(x)):
        s = sm[i + 1] - sm[i + 1 - n]
        q = cs[i + 1] - cs[i + 1 - n]
        out[i] = max(0.0, q / n - (s / n) ** 2) ** 0.5
    return out


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    prev_c = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return _sma(tr, n)


def compute_boll_atr(bars: dict, *, n: int = 20, k: float = 2.0,
                     atr_n: int = 14, max_signals: int = 200,
                     market: str = "US") -> dict:
    """BOLL 回归 + 仓位状态机。bars = db.load_daily_bars / load_min_bars 输出。"""
    c = np.asarray(bars["close"], dtype=np.float64)
    h = np.asarray(bars["high"], dtype=np.float64)
    l = np.asarray(bars["low"], dtype=np.float64)
    dates = bars.get("dates") or bars.get("ts") or []

    mid = _sma(c, n)
    sd = _roll_std(c, n)
    upper = mid + k * sd
    lower = mid - k * sd
    atr = _atr(h, l, c, atr_n)

    allow_short = market in ("US", "CRYPTO")

    def emit(i, side):
        ts = str(dates[i]) if i < len(dates) else ""
        a = float(atr[i])
        sig = {
            "ts": ts, "ts_ms": ts_to_ms(ts) or 0,
            "side": side, "price": round(float(c[i]), 6),
            "atr": round(a, 6),
            "reason": (f"BOLL{'下' if side in ('open_long', 'buy', 'close_short') else '上'}"
                       f"轨回归 ATR{atr_n}={a:.4f}"),
        }
        if side in ("open_long", "buy"):
            sig["stop"] = round(float(c[i] - k * a), 6)
        elif side in ("open_short",):
            sig["stop"] = round(float(c[i] + k * a), 6)
        signals.append(sig)

    signals: list[dict] = []
    pos = "flat"                      # flat / long / short
    for i in range(max(1, n), len(c)):
        if np.isnan(upper[i]) or np.isnan(atr[i]) or np.isnan(c[i - 1]):
            continue
        up_reg = c[i - 1] < lower[i - 1] and c[i] > lower[i]   # 下轨收回 → 转多
        dn_reg = c[i - 1] > upper[i - 1] and c[i] < upper[i]   # 上轨收回 → 转空
        if up_reg:
            if pos == "flat":
                emit(i, "open_long" if allow_short else "buy")
                pos = "long"
            elif pos == "short":
                emit(i, "close_short")
                pos = "flat"
        if dn_reg:
            if pos == "flat" and allow_short:
                emit(i, "open_short")
                pos = "short"
            elif pos == "long":
                emit(i, "close_long" if allow_short else "sell")
                pos = "flat"

    return {
        "algo": "boll_atr",
        "params": {"n": n, "k": k, "atr_n": atr_n, "allow_short": allow_short},
        "signals": signals[-max_signals:],
        "last_close": round(float(c[-1]), 6) if len(c) else None,
        "bars": len(c),
        "position": {"flat": "空仓", "long": "多头", "short": "空头"}[pos],
    }


SIGNAL_ALGOS["boll_atr"] = compute_boll_atr


def compute_lgbm(bars: dict, market: str = "HK", **params) -> dict:
    """LightGBM 买卖点（港股；日线/分时自动选对应模型）。"""
    from market_sniper.lgbm_model import run_algo
    return run_algo(bars, market=market, **params)


SIGNAL_ALGOS["lgbm"] = compute_lgbm
