# vendors/sm701 (桌面 sm701 算法同步说明)
- 来源: ~/桌面/sm701/lgbm_train_backtest.py (44维 LightGBM P(次日上涨)) + features_extra.py (资金流/两融/龙虎榜/业绩, 均错后1日) + train_tcn_pattern.py + lgbm_tcn_ensemble.py
- 本仓库只收录特征表与懒加载调用 (factors/ml.py), 不收录 7.7GB stock_cache.db 与模型二进制.
- 需要 ML 时从桌面拷模型: `lgbm_output/lgbm_model.txt` -> `vendors/sm701/lgbm_model.txt` (Pi 上 pip install lightgbm).
- 口径: 训练 2018-2023 / 验证 2024+ 样本外; T日收盘信号 T+1开盘买 T+1收盘卖; 成本 0.0015; 特征见 factors/ml.py FEATURES_SM701.

## 分市场 production 模型 (2026-10, 港/美/期货默认 sm701)

- 桌面 `~/桌面/sm701/production/` 有分市场三周期模型:
  `prod_{hk|us|fut}_h{1,5,10}_tech.txt` (40 维 tech 特征, 三周期等权),
  与 `features_tech.json` (特征列序) 配套.
- factors/ml.py 自动识别市场并优先加载 (hk/us/fut); cn/fund/crypto 走
  44 维单模型 lgbm_model.txt (A股口径可比). 缺 production 文件自动回退.
- Pi 部署: 把 production 目录整体拷到 `vendors/sm701/production/`
  (约 100MB, 不入 git; 与桌面模型同源, 更新后重拷即可).
