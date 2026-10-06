# 重构说明: 对标 QUANTAXIS / zvt 的高效分层

日期: 2026-10-06. 目标: 在不推翻现有 cron/状态页/模拟盘的前提下,
把桌面 4 个项目算法收敛到同一策略/数据接口, 并让 Pi 上更快更稳.
第三方均为 MIT, 仅借鉴思想, 归因见 THIRD_PARTY_NOTICES.md.

## 映射表

| QUANTAXIS (MIT) | zvt (MIT) | ai-quant 现在 | 说明 |
|---|---|---|---|
| QASU/QAFetch/QAData | Schema.record/query + provider | `data/store.py` + `data/stock/cli_bridge.py` | 单例连接/WAL/复合索引/批量读; 缓存仍是 scripts/cli/stock_cache.db, 不改表 |
| QIFI/QAMarket/qaposition/marketpreset | TradableEntity | `trading/stock_account.py` + `trading/instruments.py` + `trading/orders.py` | 已有 MarketModel/RiskManager/订单日志, 本次不动口径, 只加 core.types 统一对外 |
| QAStrategy 回测套件 | Trader.on_time/trade_the_targets | `core/strategy.py` + `strategies/*` + `backtest/engine_v2.py` | 新策略只实现 generate(), 回测/实盘同口径 T+1 |
| QAFactor/QAIndicator | Factor data_df/factor_df/result_df | `factors/technical.py` + `factors/chaodi.py` + `factors/morphology.py` + `factors/ml.py` | numpy 向量化 12 列; 18 策略/形态/ML 均为适配器, 缺失自动降级 |
| QARS/Arrow 零拷贝 | - | `data/store.py#get_batch` | 先拿掉逐只建连的开销 (limit=300 省约 30%), 重依赖以后再加 |
| QASchedule/QAEngine | DataRunner | 现有 cron + `sim/run.py` | 不动; 新策略通过 `--strategy mix_ensemble` (待 sim 接入) 或回测 `engine_v2.run()` 使用 |

## 桌面算法同步

- `vendors/chaodi/et_engine/` <- ~/桌面/chaodi_strategies/et_engine (18 策略, 本次拷贝)
- `vendors/sniper/{indicators,features,signals}.py` <- ~/桌面/market-sniper/market_sniper (向量化写法参考)
- `vendors/sm701/README` <- ~/桌面/sm701 特征表/模型说明 (代码不全拷, 只记 44 维表 + 懒加载, 模型 txt 另拷)
- `scripts/cli/stock_predict.py` 已在库 (morphology 同源, 由 build_cli.py 生成)
- 加载优先级: vendors > 桌面绝对路径 > Pi 降级 (空信号, 不抛错)

## 高效点 (Pi 实测导向)

1. `data/store.ensure_indexes()`: daily_bars(code,date) 复合索引, 回测 scan 最快一档.
2. `get_batch()`: 复用单例连接批量读, 避免 cli_bridge.db_rows 逐只 open/close.
3. `factors/technical.compute()`: O(n) 向量化替代 statistics.stdev 逐窗, 无 numpy 时退化为 MA5/MA20.
4. `StrategyBacktest.scan()`: 先批量读全量 bars 再逐日切 T 尾调 generate(), 因果与原 i+1 一致, 事件统一后移到 T+1.
5. `mix_ensemble`: morph0.5/chaodi0.3/ml0.2, ml 缺失重归一, 保证 Zero2W 无 lightgbm 也能跑.
6. 不动项: 费用/整手/T+1/涨跌停/ATR止损/基准口径全部沿用 stock_engine, 回测数字可比.

## 验证

  python3 -m pytest tests/test_store.py tests/test_strategy_registry.py -q
  python3 -c "import strategies; print(strategies.registry.names())"
  python3 -c "from data import store as s; s.ensure_indexes(); print(s.counts())"
  .venv/bin/python scripts/stock_backtest.py --limit 30  # 原口径冒烟
  python3 -c "from backtest.engine_v2 import run; run(strategy='chaodi_trend', limit=30)"
