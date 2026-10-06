"""factors.chaodi: 通达信 18 策略适配器 (桌面 chaodi_strategies 同源).

来源: ~/桌面/chaodi_strategies/et_engine/{data,indicators,signals,strategies,
scan,universe}.py (自研) + Pi 分发 vendors/chaodi/et_engine (本次同步拷贝).
加载顺序: vendors/chaodi/et_engine > ~/桌面/chaodi_strategies/et_engine >
桌面 stock_predict/.venv. 任一可用即返回真实 18 策略结果, 否则返回 []
(调用方按空处理, 不抛错, 保证 Pi 上可降级运行).

18 策略 id (与 scan.py --list 一致):
boll_breakout/broken_board_recovery/bullish_alignment/consecutive_limit_ups/
high_turnover_surge/limit_up_momentum/low_volatility_leader/ma_golden_cross/
macd_golden/n_day_low_reversal/near_limit_up/oversold_bounce/oversold_reversal/
pullback_ma20_bounce/pullback_to_support/strong_open/trend_breakout/
volume_price_surge. 评分口径: 100*sum(w*(x-min)/(max-min)) (候选池内归一).
"""
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATES = [
    os.path.join(_BASE, "vendors", "chaodi"),
    os.path.expanduser("~/桌面/chaodi_strategies"),
    "/home/lan/桌面/chaodi_strategies",
]
_engine = None
_engine_from = ""


def _load():
    global _engine, _engine_from
    if _engine is not None:
        return _engine
    for root in _CANDIDATES:
        ee = os.path.join(root, "et_engine")
        if os.path.isdir(ee) and root not in sys.path:
            sys.path.insert(0, root)
        try:
            import importlib
            for m in ("et_engine.strategies", "et_engine.scan",
                      "et_engine.data"):
                if m in sys.modules:
                    del sys.modules[m]
            strat = importlib.import_module("et_engine.strategies")
            scan = importlib.import_module("et_engine.scan")
            data = importlib.import_module("et_engine.data")
            if hasattr(strat, "REGISTRY"):
                _engine = {"strategies": strat, "scan": scan, "data": data}
                _engine_from = root
                return _engine
        except Exception:
            continue
    return None


def available():
    return _load() is not None


def source():
    _load()
    return _engine_from


def list_strategies():
    e = _load()
    if not e:
        return []
    try:
        return e["strategies"].list_strategies()
    except Exception:
        return [{"id": k} for k in
                getattr(e["strategies"], "REGISTRY", {})]


def scan(strategy_id, top=20, as_of=None, params=None):
    """跑单个策略, 返回 [dict...] (含 code/score 等, 与 scan.py 同字段)."""
    e = _load()
    if not e:
        return []
    try:
        return e["scan"].scan_one(strategy_id, top=top, as_of=as_of,
                                  params=params or {})
    except Exception:
        # 兼容旧 scan.scan_many 签名
        try:
            out = e["scan"].scan_many([strategy_id], top=top)
            return out.get(strategy_id, []) if isinstance(out, dict) else []
        except Exception:
            return []
