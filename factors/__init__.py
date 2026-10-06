"""factors: 统一因子层 (numpy 向量化, 可选 pandas).

整合桌面 4 个项目算法为同一输入输出:
- technical: ai-quant data/features/technical.py (简单 MA/趋势) +
  market-sniper/market_sniper/indicators.py (MA/EMA/MACD/BOLL/KDJ/RSI/ATR/DMI/OBV/CCI/量比)
- chaodi: chaodi_strategies/et_engine (18 通达信策略, 见 factors/chaodi.py)
- morphology: stock_predict/stock_gui.py 三档组合引擎 + _composite_signals
  (见 factors/morphology.py, 经 data/stock/cli_bridge 调用)
- ml: sm701 lgbm_train_backtest.py (LightGBM 上涨概率, 44 维特征) +
  market-sniper lgbm_model.py (港股行业分组, 28 维) (见 factors/ml.py, 懒加载)

输出约定 (对标 zvt factor_df/result_df 二维思想, 但用轻量 dict):
  compute(bars) -> {"col": [float...], ...} 长度与 bars 对齐, 尾部为最新.
"""
