"""data.fund.bars: 场外基金 NAV -> [Bar] (sm701 通用因子输入).

场外联接C 无 OHLC/成交量, 为复用 sm701 44维 (纯价量结构):
  open=上一日NAV, high=max(open,close), low=min(open,close), vol=0.
sm701 builder 对 vol=0 已做回退 (vr=1, logvol=0), 趋势/动量/RSI/MACD
不受影响; mkt/breadth 取 0, r_* 取 0.5.

universe(): FUND_MAP 10只 -> [("fund:007301", name)].
get_bars(): 优先 EastMoney pingzhongdata, 失败返回 [] (不抛错).
"""
import os

from core.types import Bar

_cache = {}


def universe():
    try:
        from data.fund.fund_mapping import FUND_MAP
        return [(f"fund:{info.fund_code}", f"{sym}{info.fund_name}")
                for sym, info in FUND_MAP.items()]
    except Exception:
        return []


def _nav_history(fund_code, use_cache=True):
    key = str(fund_code)
    if use_cache and key in _cache:
        return _cache[key]
    try:
        from data.fund.fund_provider import EastMoneyPingZhongProvider
        cp = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
            "sim", "state", "fund_nav_cache.json")
        pts = EastMoneyPingZhongProvider(cache_path=cp).get_history(key)
        out = [{"date": p.date, "nav": float(p.nav)} for p in pts]
        _cache[key] = out
        return out
    except Exception:
        return []


def get_bars(mcode_or_code, tail=None):
    """fund:007301 或 007301 -> [Bar] 升序."""
    code = str(mcode_or_code or "")
    if code.startswith("fund:"):
        code = code.split(":", 1)[1]
    hist = _nav_history(code)
    if not hist:
        return []
    bars = []
    prev = hist[0]["nav"]
    for h in hist:
        nav = h["nav"] or prev
        bars.append(Bar(code=f"fund:{code}", date=h["date"], open=prev,
                        high=max(prev, nav), low=min(prev, nav),
                        close=nav, vol=0.0))
        prev = nav
    if tail:
        bars = bars[-int(tail):]
    return bars


def get_batch(codes, tail=None):
    return {c: get_bars(c, tail=tail) for c in (codes or [])}
