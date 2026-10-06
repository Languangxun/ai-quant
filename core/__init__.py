"""core: 轻量事件/类型层.

借鉴 QUANTAXIS QAData/QAStrategy 的分层思想与 zvt TradableEntity/Event 模型,
把「标的/行情/信号/订单」收敛为不可变 dataclass, 供 factors/strategies/
backtest/live 共用. 仅标准库, Pi 上零依赖.
"""
from .strategy import BaseStrategy, StrategyRegistry, registry
from .types import Bar, OrderIntent, Position, Signal

__all__ = ["Bar", "Signal", "OrderIntent", "Position",
           "BaseStrategy", "StrategyRegistry", "registry"]
