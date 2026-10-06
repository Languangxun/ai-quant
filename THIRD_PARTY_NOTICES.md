# 第三方归因 (MIT 合规)

本项目重构参考了以下 MIT 协议开源项目, 仅借鉴分层思想与接口形状,
未直接复制其源码. 特此致谢并标注来源与许可证.

## 1. QUANTAXIS — yutiansut/QUANTAXIS (MIT)

- 地址: https://github.com/yutiansut/QUANTAXIS
- 许可证: MIT License (Copyright (c) 2016-2025 yutiansut/QUANTAXIS)
- 借鉴点:
  - QASU/QAFetch/QAData: 本地数据层 + 内存数据结构 -> 对应 `data/store.py`
    (批量读取/索引/单例连接) 与 `data/stock/cli_bridge.py` (增量更新).
  - QIFI/QAMarket: 统一账户/持仓/订单协议 -> 对应 `trading/stock_account.py`
    (StockLot/StockTrade/to_state) + `trading/orders.py` (订单生命周期日志)
    + `trading/instruments.py` (MarketModel 可插拔 T+1/T+0/涨跌停/费用).
  - QAStrategy: 回测套件基类 (on_bar, 可回测可实盘) -> 对应
    `core/strategy.py` BaseStrategy + `backtest/engine_v2.py`.
  - QAFactor/QAIndicator: 因子表达式/批量全市场 apply ->
    对应 `factors/technical.py` (numpy 向量化 12 列) 与 `strategies/builtin.py`.
  - QARS2/Arrow/Polars 零拷贝思想 -> `data/store.py` 单例连接 + 批量查询
    (Pi 上不用重依赖即提速; 需要极致性能时再引入 pyarrow/polars).
- 合规: 本项目未复制 QUANTAXIS 源码; 如未来直接引用其代码, 将保留其
  LICENSE 头并在此追加文件级说明.

## 2. zvt — zvtvz/zvt (MIT)

- 地址: https://github.com/zvtvz/zvt
- 许可证: MIT License (Copyright (c) zvtvz)
- 借鉴点:
  - TradableEntity + EntityEvent + Schema.record_data/query_data
    (provider 可插拔, 增量更新) -> 对应 `core/types.py` (Bar/Signal/
    OrderIntent) + `data/store.py` (get_bars/get_batch/trade_dates/counts)
    + `factors/morphology.py` (统一 query/signal 入口).
  - Factor 三段式 data_df/factor_df/result_df (二维索引多标的计算) ->
    对应 `factors/*` 输出约定 (compute()->{col:[...]}) 与
    `strategies/builtin.py` generate(bars_by_code, ctx)->[Signal].
  - Trader (on_time + trade_the_targets) 与 TargetSelector ->
    对应 `core/strategy.py` + `backtest/engine_v2.py` (逐日切 T 日尾部再
    generate, T+1 执行, 保证因果) 与 `sim/stock_run.py` 实盘调度.
  - DataRunner 定时增量 -> 对应 `scripts/cli` 缓存 + cron (`sim.run`/
    `night_review.py`/`gen_status.py`).
- 合规: 同上, 思想借鉴, 无源码复制.

## 3. 作者本人桌面项目 (自研, 随本仓库 adapters 同源)

- chaodi_strategies (通达信 18 策略, et_engine/): `factors/chaodi.py` 适配,
  Pi 分发拷贝见 `vendors/chaodi/`.
- stock_predict / stock_gui.py (形态相似度三档引擎, 1.7万行) 经 build_cli.py
  生成的 `scripts/cli/stock_predict.py`: `factors/morphology.py` 适配.
- market-sniper (港/美/加密超短线, indicators/features/lgbm_model):
  `factors/technical.py` 向量化写法参考, 模型见 `factors/ml.py`.
- sm701 (LGBM 44维 + TCN + 融合): `factors/ml.py` 特征表与懒加载.
- 以上均为作者本人项目, 适配层保留原文件头算法说明, 不改变口径.

## 4. 使用要求

- 本文件与各模块文件头注释共同构成归因; 分发/部署时请一并携带.
- 如引入第三方源码 (QUANTAXIS/zvt 或其他), 必须保留其 LICENSE 并在此登记.
