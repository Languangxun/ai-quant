"""strategies: 统一策略注册表 (回测/实盘同口径).

5 个内置策略, 分别对应桌面 4 个项目算法 (详见各文件头来源):
- chaodi_trend: chaodi 18 策略之 trend_breakout/boll_breakout (短线突破)
- morphology_tier: stock_predict 三档组合引擎 (_composite_signals)
- lgbm_prob: sm701 LightGBM 上涨概率 (需模型, 否则空信号降级; A股44维近似版)
- mix_ensemble: 上三者加权融合 (默认A股实盘/回测用, 替代原散落的 ensemble.py)
- sm701_universal: sm701 44维全量跨市场复用 (fund/hk/us/crypto暂代因子,
  阈值见 config/markets.yaml, 回测 sim 用 backtest.multi_engine / sim.multi_run)

用法:
  from strategies import registry
  strat = registry.get("mix_ensemble")
  signals = strat.generate(bars_by_code, {"date": "2026-10-06", "risk_mode": "稳健"})
  uni = registry.get("sm701_universal")
  signals = uni.generate({"hk:00700": bars}, {"threshold": 0.55})
"""
from core.strategy import registry  # noqa: F401
from . import builtin  # noqa: F401  (import 即自注册)

__all__ = ["registry"]
