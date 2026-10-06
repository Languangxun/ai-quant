"""factors.morphology: 形态相似度三档引擎适配器 (stock_predict 同源).

来源: ~/桌面/stock_predict/stock_gui.py (唯一算法源, 自研, 约1.7万行) 经
build_cli.py 生成的 scripts/cli/stock_predict.py (已入库, 本仓库随包分发).
经 data/stock/cli_bridge.py 调用 _composite_signals / daily_pick_score.

本适配器把 CLI 的逐股信号转为 core.types.Signal 列表, 供 strategies 统一消费:
- signals_for(code, risk_mode) -> [(i, date, BUY/SELL, reason)]
- score_of(reason) -> int (解析 reason 尾部 "(nn)" 评分, 与 stock_engine 同正则)
"""
import re

_SCORE_RE = re.compile(r"\((-?\d+)\)")


def score_of(reason):
    m = _SCORE_RE.search(reason or "")
    return int(m.group(1)) if m else 0


def signals_for(code, risk_mode="稳健"):
    from data.stock import cli_bridge as bridge
    rows = bridge.db_rows(code)
    if len(rows) < 100:
        return []
    try:
        return bridge.signals(rows, risk_mode)
    except Exception:
        return []


def bars_signals(bars_by_code, risk_mode="稳健"):
    """批量版: bars_by_code={code: [Bar...]} -> {code: [(i,date,act,reason)]}.

    为避免重复读库, 若 bars>=min_bars 则直接用内存 bars 调 CLI 纯函数;
    CLI 缺失时返回 {}.
    """
    try:
        from data.stock import cli_bridge as bridge
        cli = bridge.load()
        params = bridge.risk_params(risk_mode)
    except Exception:
        return {}
    out = {}
    for code, bars in (bars_by_code or {}).items():
        rows = [{"date": b.date, "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "vol": b.vol}
                for b in bars]
        if len(rows) < 100:
            continue
        try:
            out[code] = cli._composite_signals(rows, params)
        except Exception:
            continue
    return out
