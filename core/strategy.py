"""core.strategy: 统一策略基类 + 注册表.

接口对标:
- QUANTAXIS QAStrategy: on_bar/on_tick, 可回测可实盘 (MIT).
- zvt Trader/TargetSelector: on_time + trade_the_targets, result_df
  (filter_result/score_result 二维索引思想) (MIT).

本项目最小实现 (Pi 友好, 无 pandas 依赖):
- generate(bars_by_code, ctx) -> list[Signal]
  bars_by_code: {code: [Bar,...]} (升序, 尾部为 T 日)
  ctx: {"date": T日, "risk_mode": ..., "universe": [(code,name,ind,mktcap)]}
- registry 全局单例, strategies/builtin.py 在 import 时自注册.
"""
from .types import Signal


class BaseStrategy:
    """所有选股/择时策略的基类."""

    name = "base"
    description = ""

    def generate(self, bars_by_code, ctx=None):
        """返回当日信号列表. 子类重写."""
        raise NotImplementedError

    def __repr__(self):
        return f"<Strategy {self.name}>"


class StrategyRegistry:
    def __init__(self):
        self._items = {}

    def register(self, cls):
        inst = cls() if isinstance(cls, type) else cls
        self._items[inst.name] = inst
        return inst

    def get(self, name):
        return self._items.get(name)

    def list(self):
        return sorted(self._items.values(), key=lambda s: s.name)

    def names(self):
        return sorted(self._items)


registry = StrategyRegistry()


def register(cls):
    """装饰器用法: @register class MyStrat(BaseStrategy): ..."""
    return registry.register(cls)
