# -*- coding: utf-8 -*-
"""18 个内置策略的本地实现（规则与云端策略逐条一致）。

评分口径（已对拍）：
    score = 100 * Σ w_f * (x_f - min_f) / (max_f - min_f)
    min/max 取自「通过过滤后的候选池」，权重见各策略 scoring。
"""
import numpy as np

DEFAULT_BOARDS = ["沪主板", "深主板", "创业板", "科创板", "北交所"]
DEFAULT_BASIC = {
    "price_min": 3.0,
    "price_max": 300.0,
    "market_cap_min": 1_000_000_000.0,
    "float_cap_min": None,
    "float_cap_max": None,
    "amount_min": 20_000_000.0,
    "amount_max": None,
    "turnover_min": None,
    "turnover_max": None,
    "exclude_st": True,
    "exclude_new_days": 30,
    "boards": list(DEFAULT_BOARDS),
}

REGISTRY = {}


def _register(spec):
    REGISTRY[spec["id"]] = spec
    return spec


def defaults(spec):
    return {p["id"]: p["default"] for p in spec["params"]}


def _sig(ind, name):
    return np.nan_to_num(ind[name]).astype(bool)


def _p(p, key, dflt):
    v = p.get(key, dflt)
    return dflt if v is None else v


def _opt(cond, enabled):
    """enabled=False 时该条件不生效（与策略源码 if 分支一致）。"""
    return cond if enabled else True


def _and(*conds):
    out = None
    for c in conds:
        out = c if out is None else (out & c)
    return out


# ---------------- 策略定义 ----------------

_register({
    "id": "boll_breakout", "name": "布林突破",
    "description": "突破布林上轨 + 放量, 强势加速信号",
    "tags": ["布林", "突破"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_boll_breakout", "label": "要求突破布林上轨",
         "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "entry_signals": ["signal_boll_breakout_upper"],
    "exit_signals": ["signal_boll_breakdown_lower"],
    "stop_loss": -0.06, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 15, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(_sig(ind, "signal_boll_breakout_upper"),
             _p(p, "require_boll_breakout", True)),
        _opt(ind["vol_ratio_5d"] >= _p(p, "vol_ratio_min", 1.5),
             _p(p, "use_volume_filter", True)),
    ),
})

_register({
    "id": "broken_board_recovery", "name": "断板反包",
    "description": "连板≥2后断板1-2天, 出现放量反包信号",
    "tags": ["涨停", "反包"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_limit_up", "label": "要求当日涨停",
         "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
        {"id": "use_change_filter", "label": "启用涨幅过滤",
         "type": "bool", "default": True},
        {"id": "change_pct_min", "label": "最低涨幅", "type": "float",
         "default": 0.03, "min": 0.01, "max": 0.10, "step": 0.01},
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "entry_signals": ["signal_limit_up"],
    "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.06, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 10, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(_sig(ind, "signal_limit_up"), _p(p, "require_limit_up", True)),
        _opt(ind["vol_ratio_5d"] >= _p(p, "vol_ratio_min", 1.5),
             _p(p, "use_volume_filter", True)),
        _opt(ind["change_pct"] > _p(p, "change_pct_min", 0.03),
             _p(p, "use_change_filter", True)),
    ),
})

_register({
    "id": "bullish_alignment", "name": "均线多头",
    "description": "MA5>MA10>MA20>MA60多头排列 + 短期动量为正",
    "tags": ["均线", "多头"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_ma_alignment", "label": "要求均线多头排列",
         "type": "bool", "default": True},
        {"id": "require_positive_momentum", "label": "要求20日动量为正",
         "type": "bool", "default": True},
    ],
    "scoring": {"momentum_60d": 0.4, "momentum_20d": 0.3, "turnover_rate": 0.3},
    "entry_signals": ["signal_ma_golden_5_20", "signal_ma_golden_20_60"],
    "exit_signals": ["signal_ma_dead_5_20", "signal_ma20_breakdown"],
    "stop_loss": -0.06, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 20, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt((ind["ma5"] > ind["ma10"]) & (ind["ma10"] > ind["ma20"])
             & (ind["ma20"] > ind["ma60"]),
             _p(p, "require_ma_alignment", True)),
        _opt(ind["momentum_20d"] > 0,
             _p(p, "require_positive_momentum", True)),
    ),
})

_register({
    "id": "consecutive_limit_ups", "name": "连板股",
    "description": "当日涨停且连续涨停 ≥ 2 天, 强势追涨",
    "tags": ["涨停", "连板"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_limit_up", "label": "要求当日涨停",
         "type": "bool", "default": True},
        {"id": "use_boards_filter", "label": "启用连板数过滤",
         "type": "bool", "default": True},
        {"id": "min_boards", "label": "最少连板数", "type": "int",
         "default": 2, "min": 1, "max": 20, "step": 1},
    ],
    "scoring": {"consecutive_limit_ups": 0.5, "change_pct": 0.3, "amount": 0.2},
    "entry_signals": ["signal_limit_up"], "exit_signals": [],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 5, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(_sig(ind, "signal_limit_up"), _p(p, "require_limit_up", True)),
        _opt(ind["consecutive_limit_ups"] >= _p(p, "min_boards", 2),
             _p(p, "use_boards_filter", True)),
    ),
})

_register({
    "id": "high_turnover_surge", "name": "高换手拉升",
    "description": "换手率 > 5% 且涨幅 > 3%, 资金活跃",
    "tags": ["换手率", "放量", "资金"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "use_turnover_filter", "label": "启用换手率过滤",
         "type": "bool", "default": True},
        {"id": "min_turnover", "label": "最低换手率%", "type": "float",
         "default": 5.0, "min": 1.0, "max": 20.0, "step": 0.5},
        {"id": "use_change_filter", "label": "启用涨幅过滤",
         "type": "bool", "default": True},
        {"id": "min_change", "label": "最低涨幅%", "type": "float",
         "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5},
    ],
    "scoring": {"turnover_rate": 0.4, "change_pct": 0.3, "momentum_5d": 0.3},
    "entry_signals": ["signal_volume_surge"],
    "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 10, "alerts": [], "limit": 50,
    "filter": lambda ind, p: _and(
        _opt(ind["turnover_rate"] > _p(p, "min_turnover", 5.0) / 100.0,
             _p(p, "use_turnover_filter", True)),
        _opt(ind["change_pct"] > _p(p, "min_change", 3.0) / 100.0,
             _p(p, "use_change_filter", True)),
    ),
})

_register({
    "id": "limit_up_momentum", "name": "连板接力",
    "description": "连板股 + 今日涨幅 > 5%, 连板接力追踪",
    "tags": ["涨停", "连板", "接力"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "use_change_filter", "label": "启用涨幅过滤",
         "type": "bool", "default": True},
        {"id": "min_change", "label": "最低涨幅%", "type": "float",
         "default": 5.0, "min": 2.0, "max": 15.0, "step": 0.5},
        {"id": "use_boards_filter", "label": "启用连板数过滤",
         "type": "bool", "default": True},
        {"id": "min_boards", "label": "最少连板", "type": "int",
         "default": 1, "min": 1, "max": 10, "step": 1},
    ],
    "scoring": {"consecutive_limit_ups": 0.4, "change_pct": 0.3, "amount": 0.3},
    "entry_signals": ["signal_limit_up"], "exit_signals": [],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 5, "alerts": [], "limit": 50,
    "filter": lambda ind, p: _and(
        _opt(ind["change_pct"] > _p(p, "min_change", 5.0) / 100.0,
             _p(p, "use_change_filter", True)),
        _opt(ind["consecutive_limit_ups"] >= _p(p, "min_boards", 1),
             _p(p, "use_boards_filter", True)),
    ),
})

_register({
    "id": "low_volatility_leader", "name": "低波动龙头",
    "description": "20 日动量为正 + 年化波动 < 30% + MA20 上方",
    "tags": ["低波动", "龙头"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_positive_momentum", "label": "要求20日动量为正",
         "type": "bool", "default": True},
        {"id": "use_volatility_filter", "label": "启用波动率过滤",
         "type": "bool", "default": True},
        {"id": "vol_max", "label": "最大年化波动", "type": "float",
         "default": 0.30, "min": 0.05, "max": 1.0, "step": 0.01},
        {"id": "require_above_ma20", "label": "要求收盘价在MA20上方",
         "type": "bool", "default": True},
    ],
    "scoring": {"momentum_60d": 0.4, "momentum_20d": 0.3, "turnover_rate": 0.3},
    "entry_signals": ["signal_ma20_breakout"],
    "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 30, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(ind["momentum_20d"] > 0,
             _p(p, "require_positive_momentum", True)),
        _opt(ind["annual_vol_20d"] < _p(p, "vol_max", 0.30),
             _p(p, "use_volatility_filter", True)),
        _opt(ind["close"] > ind["ma20"],
             _p(p, "require_above_ma20", True)),
    ),
})

_register({
    "id": "ma_golden_cross", "name": "MA 金叉",
    "description": "MA5 上穿 MA20 当日触发,量能配合",
    "tags": ["均线", "金叉"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_ma_golden", "label": "要求MA5上穿MA20",
         "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.2, "min": 0.5, "max": 5.0, "step": 0.1},
        {"id": "require_above_ma60", "label": "要求收盘价在MA60上方",
         "type": "bool", "default": True},
    ],
    "scoring": {"momentum_20d": 0.5, "vol_ratio_5d": 0.3, "change_pct": 0.2},
    "entry_signals": ["signal_ma_golden_5_20"],
    "exit_signals": ["signal_ma_dead_5_20"],
    "stop_loss": -0.06, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 15, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(_sig(ind, "signal_ma_golden_5_20"),
             _p(p, "require_ma_golden", True)),
        _opt(ind["vol_ratio_5d"] >= _p(p, "vol_ratio_min", 1.2),
             _p(p, "use_volume_filter", True)),
        _opt(ind["close"] > ind["ma60"],
             _p(p, "require_above_ma60", True)),
    ),
})

_register({
    "id": "macd_golden", "name": "MACD 金叉放量",
    "description": "MACD 金叉当日 + 量能放大",
    "tags": ["MACD", "金叉", "放量"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_macd_golden", "label": "要求MACD金叉",
         "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    ],
    "scoring": {"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "entry_signals": ["signal_macd_golden"],
    "exit_signals": ["signal_macd_dead"],
    "stop_loss": -0.07, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 20, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(_sig(ind, "signal_macd_golden"),
             _p(p, "require_macd_golden", True)),
        _opt(ind["vol_ratio_5d"] >= _p(p, "vol_ratio_min", 1.5),
             _p(p, "use_volume_filter", True)),
    ),
})

_register({
    "id": "n_day_low_reversal", "name": "新低反转",
    "description": "触及 60 日新低后当日收阳放量，反转信号",
    "tags": ["反转", "新低"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_n_day_low", "label": "要求60日新低",
         "type": "bool", "default": True},
        {"id": "require_bullish_candle", "label": "要求收阳",
         "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "entry_signals": ["signal_n_day_low"],
    "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.06, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 15, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(_sig(ind, "signal_n_day_low"), _p(p, "require_n_day_low", True)),
        _opt(ind["close"] > ind["open"],
             _p(p, "require_bullish_candle", True)),
        _opt(ind["vol_ratio_5d"] >= _p(p, "vol_ratio_min", 1.5),
             _p(p, "use_volume_filter", True)),
    ),
})

_register({
    "id": "near_limit_up", "name": "逼近涨停",
    "description": "涨幅 > 7% 且距涨停 < 3%, 追涨信号",
    "tags": ["涨停", "追涨"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "use_change_filter", "label": "启用涨幅过滤",
         "type": "bool", "default": True},
        {"id": "min_change", "label": "最低涨幅%", "type": "float",
         "default": 7.0, "min": 3.0, "max": 15.0, "step": 1.0},
        {"id": "use_limit_gap_filter", "label": "启用距涨停空间过滤",
         "type": "bool", "default": True},
        {"id": "limit_gap", "label": "距涨停空间%", "type": "float",
         "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5},
    ],
    "scoring": {"change_pct": 0.5, "amount": 0.3, "momentum_5d": 0.2},
    "entry_signals": [], "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 5, "alerts": [], "limit": 50,
    "filter": lambda ind, p: _and(
        _opt(ind["change_pct"] > _p(p, "min_change", 7.0) / 100.0,
             _p(p, "use_change_filter", True)),
        _opt(ind["change_pct"] < ind["limit_pct"]
             - _p(p, "limit_gap", 3.0) / 100.0,
             _p(p, "use_limit_gap_filter", True)),
    ),
})

_register({
    "id": "oversold_bounce", "name": "超跌反弹",
    "description": "RSI14 < 30超卖区 + 当日收阳 + 放量, 抄底信号",
    "tags": ["超跌", "反弹", "RSI"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "use_rsi_filter", "label": "启用RSI过滤",
         "type": "bool", "default": True},
        {"id": "rsi_max", "label": "RSI上限", "type": "float",
         "default": 30.0, "min": 10.0, "max": 50.0, "step": 1.0},
        {"id": "require_bullish_candle", "label": "要求收阳",
         "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.2, "min": 0.5, "max": 5.0, "step": 0.1},
    ],
    "scoring": {"change_pct": 0.3, "vol_ratio_5d": 0.3,
                "momentum_5d": 0.2, "rsi_14": 0.2},
    "entry_signals": [], "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 15,
    "alerts": [{"field": "rsi_14", "op": "<", "value": 25,
                "message": "RSI极度超卖"}],
    "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(ind["rsi_14"] < _p(p, "rsi_max", 30.0),
             _p(p, "use_rsi_filter", True)),
        _opt(ind["close"] > ind["open"],
             _p(p, "require_bullish_candle", True)),
        _opt(ind["vol_ratio_5d"] >= _p(p, "vol_ratio_min", 1.2),
             _p(p, "use_volume_filter", True)),
    ),
})

_register({
    "id": "oversold_reversal", "name": "超跌反转",
    "description": "RSI14 < 30超卖 + 涨幅 > 1% + 站上MA5, 超卖反转信号",
    "tags": ["超跌", "反弹", "RSI"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "use_rsi_filter", "label": "启用RSI过滤",
         "type": "bool", "default": True},
        {"id": "rsi_max", "label": "RSI上限", "type": "float",
         "default": 30.0, "min": 10.0, "max": 50.0, "step": 1.0},
        {"id": "use_change_filter", "label": "启用涨幅过滤",
         "type": "bool", "default": True},
        {"id": "min_change", "label": "最低涨幅%", "type": "float",
         "default": 1.0, "min": 0.5, "max": 5.0, "step": 0.5},
        {"id": "require_above_ma5", "label": "要求收盘价在MA5上方",
         "type": "bool", "default": True},
    ],
    "scoring": {"change_pct": 0.4, "rsi_14": 0.3, "vol_ratio_5d": 0.3},
    "entry_signals": [], "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 15,
    "alerts": [{"field": "rsi_14", "op": "<", "value": 25,
                "message": "RSI极度超卖"}],
    "limit": 50,
    "filter": lambda ind, p: _and(
        _opt(ind["rsi_14"] < _p(p, "rsi_max", 30.0),
             _p(p, "use_rsi_filter", True)),
        _opt(ind["change_pct"] > _p(p, "min_change", 1.0) / 100.0,
             _p(p, "use_change_filter", True)),
        _opt(ind["close"] > ind["ma5"],
             _p(p, "require_above_ma5", True)),
    ),
})

_register({
    "id": "pullback_ma20_bounce", "name": "均线回踩反弹",
    "description": "价格在MA20附近(±2%)且MA5>MA20>MA60多头排列, 回踩买入",
    "tags": ["回踩", "均线", "反弹"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "use_ma20_proximity", "label": "启用MA20附近过滤",
         "type": "bool", "default": True},
        {"id": "ma_proximity", "label": "MA偏离度%", "type": "float",
         "default": 2.0, "min": 0.5, "max": 5.0, "step": 0.5},
        {"id": "require_ma_alignment", "label": "要求MA5>MA20>MA60",
         "type": "bool", "default": True},
        {"id": "require_positive_change", "label": "要求当日上涨",
         "type": "bool", "default": True},
    ],
    "scoring": {"momentum_60d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "entry_signals": ["signal_ma_golden_5_20"],
    "exit_signals": ["signal_ma20_breakdown", "signal_ma_dead_5_20"],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 15, "alerts": [], "limit": 50,
    "filter": lambda ind, p: _and(
        _opt((ind["close"] > ind["ma20"] * (1 - _p(p, "ma_proximity", 2.0) / 100.0))
             & (ind["close"] < ind["ma20"] * (1 + _p(p, "ma_proximity", 2.0) / 100.0)),
             _p(p, "use_ma20_proximity", True)),
        _opt((ind["ma5"] > ind["ma20"]) & (ind["ma20"] > ind["ma60"]),
             _p(p, "require_ma_alignment", True)),
        _opt(ind["change_pct"] > 0,
             _p(p, "require_positive_change", True)),
    ),
})

_register({
    "id": "pullback_to_support", "name": "缩量回踩",
    "description": "回踩MA20附近 + 缩量 + 中期趋势向上",
    "tags": ["回踩", "支撑"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "use_ma20_proximity", "label": "启用MA20附近过滤",
         "type": "bool", "default": True},
        {"id": "ma_proximity", "label": "均线偏离度", "type": "float",
         "default": 0.02, "min": 0.01, "max": 0.05, "step": 0.005},
        {"id": "use_volume_filter", "label": "启用缩量过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_max", "label": "最大量比", "type": "float",
         "default": 0.8, "min": 0.2, "max": 1.5, "step": 0.1},
        {"id": "require_above_ma60", "label": "要求收盘价在MA60上方",
         "type": "bool", "default": True},
        {"id": "require_positive_momentum", "label": "要求20日动量为正",
         "type": "bool", "default": True},
    ],
    "scoring": {"momentum_60d": 0.4, "momentum_20d": 0.3, "turnover_rate": 0.3},
    "entry_signals": ["signal_ma_golden_5_20"],
    "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 20, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt((ind["close"] > ind["ma20"] * (1 - _p(p, "ma_proximity", 0.02)))
             & (ind["close"] < ind["ma20"] * (1 + _p(p, "ma_proximity", 0.02))),
             _p(p, "use_ma20_proximity", True)),
        _opt(ind["vol_ratio_5d"] < _p(p, "vol_ratio_max", 0.8),
             _p(p, "use_volume_filter", True)),
        _opt(ind["close"] > ind["ma60"],
             _p(p, "require_above_ma60", True)),
        _opt(ind["momentum_20d"] > 0,
             _p(p, "require_positive_momentum", True)),
    ),
})

_register({
    "id": "strong_open", "name": "强势高开",
    "description": "高开 > 3% 且收盘高于开盘价, 集合竞价强势",
    "tags": ["高开", "强势"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "use_open_gap_filter", "label": "启用高开过滤",
         "type": "bool", "default": True},
        {"id": "min_open_gap", "label": "最低高开%", "type": "float",
         "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5},
        {"id": "require_close_above_open", "label": "要求收盘高于开盘",
         "type": "bool", "default": True},
        {"id": "use_change_filter", "label": "启用涨幅过滤",
         "type": "bool", "default": True},
        {"id": "min_change", "label": "最低涨幅%", "type": "float",
         "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5},
    ],
    "scoring": {"change_pct": 0.4, "amplitude": 0.2, "amount": 0.4},
    "entry_signals": [], "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.05, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 10, "alerts": [], "limit": 50,
    "filter": lambda ind, p: _and(
        _opt(ind["open"] > ind["prev_close"]
             * (1 + _p(p, "min_open_gap", 3.0) / 100.0),
             _p(p, "use_open_gap_filter", True)),
        _opt(ind["close"] > ind["open"],
             _p(p, "require_close_above_open", True)),
        _opt(ind["change_pct"] > _p(p, "min_change", 3.0) / 100.0,
             _p(p, "use_change_filter", True)),
    ),
})

_register({
    "id": "trend_breakout", "name": "趋势突破",
    "description": "MA60 上方 + 60 日新高 + 量能 ≥ 2 倍均量",
    "tags": ["趋势", "突破", "放量"],
    "basic_filter": {**DEFAULT_BASIC, "price_min": 5.0, "price_max": 200.0,
                     "market_cap_min": 2_000_000_000.0,
                     "amount_min": 100_000_000.0, "exclude_new_days": 60},
    "params": [
        {"id": "require_above_ma60", "label": "要求收盘价在MA60上方",
         "type": "bool", "default": True},
        {"id": "require_n_day_high", "label": "要求60日新高",
         "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 2.0, "min": 0.5, "max": 10.0, "step": 0.1},
    ],
    "scoring": {"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "entry_signals": ["signal_n_day_high"],
    "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.08, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 20,
    "alerts": [{"field": "signal_volume_surge", "message": "放量异动"}],
    "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(ind["close"] > ind["ma60"], _p(p, "require_above_ma60", True)),
        _opt(_sig(ind, "signal_n_day_high"),
             _p(p, "require_n_day_high", True)),
        _opt(ind["vol_ratio_5d"] >= _p(p, "vol_ratio_min", 2.0),
             _p(p, "use_volume_filter", True)),
    ),
})

_register({
    "id": "volume_price_surge", "name": "量价齐升",
    "description": "突破 MA20 + 放量 + 收阳",
    "tags": ["量价", "突破"], "basic_filter": dict(DEFAULT_BASIC),
    "params": [
        {"id": "require_ma20_breakout", "label": "要求突破MA20",
         "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤",
         "type": "bool", "default": True},
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 2.0, "min": 0.5, "max": 10.0, "step": 0.1},
        {"id": "require_bullish_candle", "label": "要求收阳",
         "type": "bool", "default": True},
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "entry_signals": ["signal_ma20_breakout"],
    "exit_signals": ["signal_ma20_breakdown"],
    "stop_loss": -0.06, "take_profit": None, "trailing_stop": None,
    "max_hold_days": 15, "alerts": [], "limit": 100,
    "filter": lambda ind, p: _and(
        _opt(_sig(ind, "signal_ma20_breakout"),
             _p(p, "require_ma20_breakout", True)),
        _opt(ind["vol_ratio_5d"] >= _p(p, "vol_ratio_min", 2.0),
             _p(p, "use_volume_filter", True)),
        _opt(ind["close"] > ind["open"],
             _p(p, "require_bullish_candle", True)),
    ),
})


def list_strategies():
    return list(REGISTRY.values())


def get(sid):
    return REGISTRY[sid]
