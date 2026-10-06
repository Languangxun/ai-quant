"""strategies: 统一策略注册表 (回测/实盘同口径).

4 个内置策略, 分别对应桌面 4 个项目算法 (详见各文件头来源):
- chaodi_trend: chaodi 18 策略之 trend_breakout/boll_breakout (短线突破)
- morphology_tier: stock_predict 三档组合引擎 (_composite_signals)
- lgbm_prob: sm701 LightGBM 上涨概率 (需模型, 否则空信号降级)
- mix_ensemble: 上三者加权融合 (默认实盘/回测用, 替代原散落的 ensemble.py)

用法:
  from strategies import registry
  strat = registry.get("mix_ensemble")
  signals = strat.generate(bars_by_code, {"date": "2026-10-06", "risk_mode": "稳健"})
"""
from core.strategy import registry  # noqa: F401
from . import builtin  # noqa: F401  (import 即自注册)

__all__ = ["registry"]
