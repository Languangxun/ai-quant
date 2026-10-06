"""core.types: 不可变基础类型 (标准库 only).

设计参考:
- QUANTAXIS QIFI/QAData: 账户/持仓/订单统一口径 (MIT, yutiansut/QUANTAXIS).
- zvt TradableEntity + EntityEvent: 标的与事件分离 (MIT, zvtvz/zvt).
详见 THIRD_PARTY_NOTICES.md 与 docs/REFACTOR_qa_zvt.md.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Bar:
    """单根日K (后复权口径, 与 cli_bridge.db_rows 同字段)."""
    code: str
    date: str  # YYYY-MM-DD
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    vol: float = 0.0


@dataclass(frozen=True)
class Signal:
    """策略信号: T 日收盘生成, T+1 生效 (与 stock_engine 同口径)."""
    code: str
    action: str  # BUY / SELL
    score: float = 0.0
    reason: str = ""
    signal_date: str = ""
    name: str = ""


@dataclass(frozen=True)
class OrderIntent:
    """决策层给执行层的意图 (不含执行细节, 由 risk/trading 落为成交)."""
    code: str
    side: str  # BUY / SELL
    target_pct: float = 0.0  # 目标仓位占总资产 %
    amount: float = 0.0  # 目标金额 (与 target_pct 二选一)
    reason: str = ""
    confidence: float = 0.0


@dataclass
class Position:
    code: str
    shares: int = 0
    cost: float = 0.0
    price: float = 0.0
    meta: dict = field(default_factory=dict)
