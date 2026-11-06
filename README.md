# ai-quant · OrangePi 量化系统

Pi（`orangepizero2w`）上的 AI 量化研究与模拟交易系统：**多市场组合模拟盘**
（基金/港/美/加密/期货 + 股票/ETF，CLI 选股 + LLM 决策 + 全缓存回测）、
旧版场外 ETF 联接 C 类基金模式（etf-c，保留可切换）、每日复盘与
U 盘快照备份，并接收本机 stock-analyzer 通过 `--push` 推送的分析报告。

架构借鉴 [AKQuant](https://github.com/akfamily/akquant) 的三层拆分（轻量 Python 版）：
`MarketModel`（可插拔交易规则：股票/ETF、T+1/T+0、涨跌停、费用）、
独立 `RiskManager` 预交易风控拦截层、订单生命周期日志（REQUESTED →
VALIDATED → FILLED / REJECTED / NO_CHANGE）。

2026-10 重构另参考 MIT 项目 [QUANTAXIS](https://github.com/yutiansut/QUANTAXIS)
（QASU/QAData/QIFI/QAStrategy/QAFactor 分层）与 [zvt](https://github.com/zvtvz/zvt)
（TradableEntity/Event + record/query + Factor/Trader），仅借鉴思想、无源码复制，
归因见 `THIRD_PARTY_NOTICES.md`，说明见 `docs/REFACTOR_qa_zvt.md`。

## 目录

| 路径 | 用途 |
|---|---|
| `core/` | 统一类型与策略基类（`types.Bar/Signal` + `strategy.BaseStrategy/registry`，对标 QAStrategy/zvt-Trader） |
| `data/store.py` | 统一行情存储（单例+WAL+批量读+索引，对标 QAData/zvt-query；旧 `data/stock/cli_bridge.py` 仍兼容） |
| `factors/` | 统一因子层：`technical` numpy 12列 / `chaodi` 18策略适配 / `morphology` 形态三档适配 / `ml` LGBM懒加载 |
| `strategies/` | 统一策略注册表：`chaodi_trend` / `morphology_tier` / `lgbm_prob` / `mix_ensemble`（回测实盘同口径） |
| `backtest/engine_v2.py` | 策略可插拔回测（沿用 `stock_engine` 成交/费用/ATR止损口径，只换信号源） |
| `vendors/` | 桌面算法同步拷贝：`chaodi/et_engine` / `sniper/{indicators,features,signals}` / `sm701/lgbm_model.txt+README`（Pi 另拷 `sm701/production/` 分市场模型，不入库） |
| `main.py` | 入口：读取 `config/system.yaml` |
| `agent/` | 决策链路（stock_decision 股票组合 / ensemble 基金多模型） |
| `models/` | 模型客户端（deepseek / embedding / prompt / stock_prompt） |
| `config/` | 运行配置（system / model / risk / **stock** / **holidays** 休市日历） |
| `trading/` | 账户与执行（**stock_account/stock_executor** + 基金 account/executor）；`instruments.py` 市场规则（股票/ETF）、`orders.py` 订单状态日志 |
| `risk/` | 风控：`stock_rules.py` 股票/ETF 预交易拦截层；`manager.py` 场外基金仓位规则 |
| `sim/` | 模拟盘：`run.py` 双模式调度，`stock_run.py` / `fund_run.py` |
| `backtest/` | 回测：`stock_engine.py` 股票组合全缓存回测 + 基金引擎 |
| `data/` | 行情、基金映射、特征、历史缓存；`data/stock/cli_bridge.py` 桥接 CLI |
| `memory/` | 记忆与收件箱（`inbox/` 推送落点，`daily/` 每日总结/复盘） |
| `scripts/` | 运维脚本（状态页 `gen_status.py`、账户重置 `reset_account.py`）；`scripts/cli/` 为独立单文件 CLI（生成物，勿手改） |
| `archive/` | 历史备份归档（`*.bak*`、旧快照，不参与运行） |

## 股票模拟盘

- **实盘（每交易日多个时点，cron `python -m sim.run`）**：
  交易日闸门（周末 + `config/holidays.json` 休市日跳过，`--force` 强制）
  → CLI 缓存选股 → 腾讯实时行情 → `deepseek-v4.1-flash`（`deepseek-flash`）
  组合决策 → `StockExecutor` 按 A股规则成交 → 状态/复盘/RAG 记忆/U 盘快照
  ```bash
  .venv/bin/python -m sim.run --dry-run --no-rag   # 试跑
  .venv/bin/python -m sim.run --mode fund          # 旧版 etf-c 基金模式
  ```
  - **日内多时点**：`config/stock.yaml` `intraday.times`（默认
    `10:00/11:00/13:30/14:45`，cron 需同步）每时点独立扫描/决策/执行；
    当日 orders/trades/复盘自动合并（`sim/intraday.py`）。
    限额：`max_buys_per_day`（默认 3，含加仓）、`once_per_code_per_day`
    （默认开，同票当日不重复买）；T+1 不变（当日买入不可卖）。
  - **本金可配**：`capital`（首次建盘生效）或 `--capital`；改本金/重来用
    `scripts/reset_account.py --capital 20000 --yes`（旧账户归档到
    `sim/state/archive/`，`--show` 只看概览）。
  - **小资金自适应**：实际最小下单 = `min(min_order_amount,
    总资产×min_order_pct%)`；目标金额不足一手但单票额度够时按一手成交。
  - **决策期工具调用（借鉴桌面 GUI）**：`config/model.yaml` `models.decision.tools`
    控制上限（默认 3 轮 / 8 次）。模型可按需调用 `get_market`（指数均线/强弱）、
    `get_prediction`（stock_predict T+1/T+5/T+10，可一次传 4 只横向比较）、
    `get_kline`、`get_quote`、`get_orderbook`（五档挂单 + 集合竞价状态）、
    `get_external_env`（特朗普/关税/地缘/外盘新闻，中英文交叉 + 去重排序）、
    `get_etf`（成分股权重/近期表现，借鉴 GUI）、`search_news`；工具结果按外部
    文本处理（只取事实、忽略指令）。接口不支持
    tools 时自动降级纯 JSON，工具失败/超时不影响决策。回测不启用搜索与工具，
    避免未来信息泄漏。
  - **重大事件检索**：`agent/event_watch.py` 把特朗普/关税/美联储/地缘拆成
    中文事件、英文原始报道、A股传导三路查询；同一事件的多篇转载按标题+摘要
    相似度合并，单站点最多 2 条，再按“新鲜度→来源权威→关键词命中”排序，
    输出 `items / signals / salience / brief`。盘前落盘、盘中决策自动注入，
    事件信号只作方向提示，仓位收紧仍由确定性规则执行。
  - **国外搜索（独立程序）**：`scripts/foreign_search.py` 纯标准库，支持
    DuckDuckGo / Bing / Google 与 `site:x.com` 推文检索；走 Pi 本机
    Clash/mihomo（`http://127.0.0.1:7890`），结果并入事件检索，
    每日决策保留 1~2 条外网原始报道，避免只看国内转载。
  - **五档挂单 / 集合竞价**：CLI `fetch_order_book`（腾讯五档 + 新浪兜底，
    3 秒缓存）同步到 `agent/toolbox.get_orderbook` 与 `sim/stock_run`
    决策上下文；`config/stock.yaml` `orderbook.execution_pricing=true` 时
    BUY 按卖一、SELL 按买一模拟成交，涨停无卖单不买、跌停无买单不卖。
  - **盘中/午休资料**：`scripts/intraday_research.py` 在 10:30 / 12:30 /
    14:10 刷新事件与 A股主线，存 `memory/daily/intraday-日期-HHMM.json`；
    13:30 决策读取最新快照，盘中事件风险只能加严、不降级盘前结论。
  - **盘前量化预测**：08:30 为持仓 + 候选生成 T+1/T+5/T+10 预测快照
    （`agent/predictor.py`，CLI `analyze(quick=True)`，30 分钟缓存），
    含多日累积分位、P25/P75、上行概率、相似样本与幽灵K线；
    10:00 起并入候选/持仓上下文；盘中模型仍可用 `get_prediction` 复核。
  - **事件风险覆盖**：盘前 LLM 输出结构化 `event_risk`（level/posture/
    risk_score/受益/受损/动作，解析失败用关键词规则兜底），决策模型可在
    实时新闻下修正；`sim/stock_run.py` 按 `config/stock.yaml` `event_risk`
    做确定性覆盖：elevated 收紧单票上限与买入笔数；high 进一步收紧并跳过
    明确受损行业。只收紧 BUY，不干预 SELL，也不突破风控硬规则。
- **全缓存回测**：
  ```bash
  .venv/bin/python scripts/stock_backtest.py                  # 全量（约4860只）
  .venv/bin/python scripts/stock_backtest.py --limit 300      # 抽样
  .venv/bin/python scripts/stock_backtest.py --mode 激进 --exec-px open
  .venv/bin/python scripts/stock_backtest.py --no-etf         # 纯股票口径
  ```
  信号 T 日收盘生成、T+1 成交；ATR(14) 跟踪止损；100股整手/T+1/佣金
  万2.5+印花税0.05%；基准=等权全市场 + 上证指数。结果落
  `backtest/results/`（JSON + markdown 报告）。回测为**日线口径**
  （无日内数据，不模拟多时点），`backtest.capital` 默认 100000。 
  - 实测（2020-02~2026-09）：市值前 300 年化 **+16.1%**、Sharpe **1.01**
    （超额上证 +63.9pp）；全市场 +1.0% —— **信号边际集中在大市值**，
    实盘候选默认按市值前 120 扫描与此一致。
- **股票 + ETF 同池**：候选池含 ETF（`config/stock.yaml`
  `universe.include_etf`）。ETF 数据需先用 CLI 回填：
  `cd scripts/cli && python3 stock_predict.py --refresh-etf`（约1500只，
  10~25分钟）；市场规则在 `trading/instruments.py`（可用
  `stock.market.etf` 覆盖）：
  - 股票：T+1，涨跌停 10%（创业板/科创板 20%，主板 ST 5%），印花税+过户费
  - ETF：股票型 T+1；跨境/债券/黄金/货币/商品类 T+0（代码前缀 sh511/
    sh513/sh518 + 名称关键词识别）；免印花税与过户费；创业板/科创板
    相关 ETF 涨跌停 20%
- **A股规则**：整手买入、T+1 可卖（T+0 标的当日可卖）、涨跌停不成交、
  最多 5 只、单票 ≤20%、日内买入笔数上限；下单金额随本金自适应。
- **订单日志**：指令状态落 `sim/state/order_journal.jsonl`（REQUESTED →
  VALIDATED → FILLED / REJECTED / NO_CHANGE），风控在
  `risk/stock_rules.py` 独立拦截层统一判定。
- **LLM 自动降级**：DeepSeek 余额不足/额度耗尽/鉴权失败，或网络不可达
  （网口抖动导致 Connection error）时，自动切纯代码评分轮动
  （卖出跌出评分前列或 -5% 止损的持仓，买入评分最高候选），
  继续遵守持仓数/单票上限/日内买入限额；恢复后自动回到 LLM 决策。

## 多市场模拟盘（基金/港/美/加密/期货）

`sim/multi_run.py` + `backtest/multi_engine.py`，无 LLM、Pi 友好，
**sm701 自动识别市场**：信号层按代码前缀（hk:/us:/fut:/crypto:/fund:）
分市场选模型——

- **hk/us/fut 默认 sm701**：优先 production 分市场三周期模型
  （`prod_{hk|us|fut}_h{1,5,10}_tech.txt`，40 维 tech 特征，三周期等权，
  与桌面 stock_predict v7 实时复算同口径）；模型缺失自动回退 44 维单模型。
  模型目录：`vendors/sm701/production/`（Pi）或 `~/桌面/sm701/production/`（桌面）。
- cn/fund/crypto 走 44 维单模型（A股口径保持可比，fund/crypto 为域外推暂代）。
- 阈值按市场分别配在 `config/markets.yaml`（`sm701.threshold`，默认 fund
  0.52 / 其余 0.55）。

```bash
python3 -m sim.multi_run --market fut --dry-run      # 试跑（fut=国内期货82主力连续）
python3 -m sim.multi_run --market all                # fund+hk+us+crypto+fut
python3 -m backtest.multi_engine --market fut --min-hold 2
```

- 数据：港/美/加密/期货日K 在 `data/global_daily.db`（增量
  `scripts/sync_global_daily.py`，期货自 sm701 副本库同源同步）。
- 期货口径：T+0、仅佣金、涨跌停/保证金/合约乘数未建模（现金全额记账，
  高价品种单笔额度不足一手时跳过）；编码 `fut:RB0`，与 sm701 fut_daily
  主力连续同源。
- 状态：`sim/state/multi-{market}.json` + `memory/daily/multi-{market}-*.json`，
  状态页「多市场」区含期货卡片。

## CLI（预测/选股引擎）

```bash
cd scripts/cli && python3 stock_predict.py 000725
```

- 源码 `scripts/cli/stock_predict.py` 已入库（生成物但随仓库分发，
  2026-11-06 同步桌面 v7.0.3：AI 工具取数、ETF 成分股、多日预测、
  五档盘口/集合竞价（`fetch_order_book`）与 DeepSeek Search 改进）；
  缓存 `scripts/cli/stock_cache.db`（全市场日K，约 1272 万根）不入库，
  新环境用 `--refresh-cache` / `--backfill` / `--refresh-etf` 重建
- AI：优先 `DEEPSEEK_API_KEY` / `stock_gui.ini [deepseek]`；缺省回退主目录
  opencode-go 授权（`~/.local/share/opencode/auth.json`）
- 支持 `--tiers` / `--research` / `--push` 等（见文件头注释）

## 模型与记忆

| 用途 | 配置 | 说明 |
|---|---|---|
| 决策 | `config/model.yaml` → `deepseek-flash` | DeepSeek-V4.1-Flash，`api.deepseek.com`，Key 在 `.env`；支持 GUI 风格工具调用（可配） |
| 联网检索 | `config/model.yaml` → `models.search` | 决策前自动调用 DeepSeek Search API（重大事件 + 大盘 + 候选/持仓）；事件检索中英文交叉、按新鲜度/来源去重排序，结果 30 分钟缓存；失败静默降级，回测禁用（防未来信息泄漏） |
| 盘前资料 | `scripts/premarket_research.py`（08:30 cron） | 每交易日盘前先搜特朗普/关税/地缘/美联储主导事件，再补宏观/外盘/政策/板块 + 持仓/候选 T+1/T+5/T+10 预测 + 结构化事件风险，存 `memory/daily/premarket-*.json`，10:00 决策自动参考；`--email` 可推送盘前邮件 |
| 邮件反馈 | `~/.config/ai-quant/mail.env`（IMAP） | 读取用户回复邮件，近 7 天反馈并入决策上下文；只提取合理建议、不违反风控；失败自动用缓存 |
| 缓存 | 一天一个会话（`sim/state/llm_daily_*.json`） | 当日历史多轮追加，DeepSeek 前缀缓存命中（约 1/10 价），跨天自动新会话 |
| 记忆 | Ollama `qwen3-embedding:0.6b` | 相似历史检索（RAG），失败自动降级 |

邮件反馈：IMAP 主机默认由 `MAIL_HOST` 推导（smtp→imap），只收
`MAIL_FEEDBACK_SENDERS`（默认 `MAIL_RECIPIENTS`）发来的邮件；
`python3 -m memory.mail_feedback --show` 查看缓存，`--force` 立即拉取。
可直接回复日报邮件批评/提建议，决策时会参考但不会越权执行。

盘前资料：交易日 08:30 cron 跑 `scripts/premarket_research.py --email`，
先用 `agent/event_watch.py` 搜特朗普/关税/地缘/美联储（中文事件 + 英文原始
报道 + A股传导），去重排序后作为“外部事件主线”，再补 A股盘前/隔夜外盘/
板块资金；同时用 CLI `analyze(quick=True)` 给持仓+候选生成 T+1/T+5/T+10
预测。LLM 输出四段摘要与结构化 `event_risk`，存
`memory/daily/premarket-*.json` 与 `盘前-*.md`。10:00 股票/基金决策自动读取
当日文件，候选/持仓自动挂预测，事件风险参与订单收紧。非交易日自动跳过；
`--force` 手动试跑，`--no-predict` 跳过预测，`--focus "特朗普 关税"`
增加聚焦检索。

## Pi 部署 / Clash / 国外搜索

Pi 通过 Tailscale 访问：`ssh orangepi@100.117.242.5`
（主机名 `orangepizero2w-1`）。Clash 用 mihomo user service 常驻：

```bash
# Pi 本地文件（均不入库，chmod 600）
~/.config/mihomo/config.yaml                 # Clash 订阅生成的配置
~/.config/ai-quant/clash/subscription.txt    # 订阅 URL
~/.config/ai-quant/foreign_search.env        # 国外搜索代理/引擎

systemctl --user status mihomo
python3 scripts/clash_update.py --check      # 连通性
python3 scripts/clash_update.py              # 拉订阅 -> 校验 -> 重启
# Pi cron: 每周一 06:00 自动刷新节点；失败保留旧配置
python3 scripts/foreign_search.py --check    # Google/X/DDG
python3 scripts/foreign_search.py -q "Trump tariff China" --count 5
```

X 的网页检索可能被登录/风控挡住；要稳定查 X，在
`~/.config/ai-quant/foreign_search.env` 增加 `X_BEARER_TOKEN=...`
（X API v2），不配则自动退回 `site:x.com` 网页检索。

盘中/午休不再按固定点跑：**板块波动监控（事件驱动）**
`agent/volatility_watch.py` + `scripts/volatility_watcher.py --loop`
每分钟拉东财全行业板块指数快照，用量比/振幅/涨跌预筛，
对可疑板块复核 ATRTR/ATR14、布林带宽(20,2) z 分数、量比
（三者至少占二才算"波动大"，阈值在 `config/stock.yaml`
`volatility_watch`）。触发即激活：刷新事件资料 + flock 锁下跑一轮
`sim.run` 决策。静默超过 60 分钟或无信号也会强制激活一次，防失联。

```bash
systemctl --user status aiquant-watcher   # Pi 常驻服务
python3 scripts/volatility_watcher.py --status    # 今日激活情况
python3 scripts/volatility_watcher.py --once      # 手动单次检查
python3 scripts/volatility_watcher.py --force     # 立即激活
```

仅 12:30 午休保留一条固定研究 cron（时段无行情，只补外部事件）；每日
激活上限 `max_activations`（默认 8 次），两次激活至少间隔 15 分钟。

## 状态页（`/quant/`）

`scripts/gen_status.py` 每 30 分钟生成 `/var/www/status/index.html`（纯标准库、无 JS）：
顶栏休市/交易日状态（读 `config/holidays.json`），股票模拟盘账户/资产曲线/决策/候选/
持仓/成交（含**当日时点数与各时点指令/成交**、本金与日内时段），回测 6 图（净值对比、
回撤、年化对比、年度收益、月度热力图、单笔收益分布）+ 指标总表（「市值前300」标
★实盘同口径），旧版基金快照，机器状态。

```bash
.venv/bin/python scripts/gen_status.py --out /tmp/status.html   # 本地预览
```

## 备份

```bash
.venv/bin/python scripts/usb_backup.py --label stock --keep 14
```

时间戳快照 + `manifest.json`（sha256）+ 复制校验 + 保留最近 14 份，
挂载点自动探测（`/media/usb` 等）。

## 约定

- 密钥只放 `.env` 与主目录授权文件，不入库（`.env` 已在 .gitignore）
- Clash 订阅、节点配置、代理地址只放
  `~/.config/ai-quant/` / `~/.config/mihomo/`，仓库只保留
  `config/foreign_search.example.env` 模板
- 提交前自动检查：`git config core.hooksPath .githooks` 后，pre-commit 会
  扫描 `DEEPSEEK_API_KEY`、`sk-*`、`MAIL_AUTH_CODE`、Clash 订阅与
  `proxy-groups`；也可手动 `python3 scripts/check_secrets.py`
- 历史备份不散落：统一移入 `archive/`；大文件（缓存、生成物）不入 git
