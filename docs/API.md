# 行情源接入文档（Quant Market Data）

> 面向开发者：程序/脚本/其它系统对接 quant 机器上的行情数据。
> 站内同款文档：<https://quant.tail6a6cc8.ts.net/docs>（含下载入口）。
> 维护者：獨白（TG @Monologue_101 / QQ 2180287399）· 更新日期 2026-11-07

---

## 1. 总览

行情源覆盖 **A股 / 港股 / 美股 / 加密货币 / 国内期货** 五个市场的日频行情，
底层为本地 SQLite 联邦库（`data/global_daily.db` 全球库 + A股 `scripts/cli/stock_cache.db`），
对外提供两种接入方式：

| 方式 | 适用场景 | 入口 |
|---|---|---|
| REST API（推荐） | 任意语言 / 远程 / 脚本轮询 | 本站 `/api/*` 接口 |
| 本地数据库直连 | 与 quant 机同机 / 挂载同一磁盘的 Python 程序 | `data/unified.py` 统一读取层 |

**Base URL**：

```
https://quant.tail6a6cc8.ts.net     # Tailscale 域名（账号内免鉴权直达）
http://100.126.30.51:8080           # 纯内网 IP 直连（同 tailnet 内更快）
```

所有响应为 UTF-8 JSON。站点仅对 Tailscale 网络/账号可见（内网 + funnel），请只读使用。

## 2. 快速开始（30 秒）

### curl

```bash
curl "https://quant.tail6a6cc8.ts.net/api/quote?code=sh600519"
curl "https://quant.tail6a6cc8.ts.net/api/kline?code=us:AAPL&period=week&tail=52"
curl "https://quant.tail6a6cc8.ts.net/api/indicators?code=crypto:BTC/USDT&period=day&tail=300"
```

### Python（requests）

```python
import requests

BASE = "https://quant.tail6a6cc8.ts.net"

# 1) K 线 + 指标
d = requests.get(BASE + "/api/indicators",
                 params={"code": "sh600519", "period": "day", "tail": 300},
                 timeout=10).json()
bars = d["bars"]                    # [{date, open, high, low, close, vol, MA_MA5, ..., BOLL_UP, ...}]
ma5 = d["indicators"]["MA"]["MA5"]  # 与 bars 一一对齐的序列，None=窗口不足
macd = d["indicators"]["MACD"]      # {DIF, DEA, MACD}

# 2) 全市场概览（服务端缓存 60s）
m = requests.get(BASE + "/api/markets", timeout=10).json()
btc = next(x for x in m["markets"]["crypto"] if x["code"] == "crypto:BTC/USDT")
print(btc["close"], btc["change_pct"])
```

### JavaScript / Node（>=18 自带 fetch）

```js
const r = await fetch("https://quant.tail6a6cc8.ts.net/api/kline?code=sh600519&period=day");
const d = await r.json();
d.bars.forEach(b => console.log(b.date, b.close));
```

## 3. REST 接口明细

| 接口 | 参数 | 说明 |
|---|---|---|
| `GET /api/quote?code=<code>` | `code`（必） | 单标概览：最新价/涨跌/量 + 最近 30 个收盘点（`history`，迷你走势图用）。返回 `{code, data:{...}}` |
| `GET /api/kline?code=<code>&period=day&tail=250` | `code`；`period=day\|week\|month`；`tail=30~800` 根数 | 纯 K 线 `bars[]:{date, open, high, low, close, vol}`。周/月K 为日K重采样，`date` 取周期内最近成交日 |
| `GET /api/indicators?code=<code>&period=day&tail=300` | 同上 | K线+指标：每根 bar 附 `MA_MA5..` / `MACD_*` / `RSI_*` / `KDJ_*` / `BOLL_*` 字段；另附 `indicators` 分组序列（与 bars 对齐，窗口不足为 `null`）。本站 K 线图同款数据 |
| `GET /api/markets` | 无 | 看板汇总：5 市场观察名单（各 8~10 只）+ headline 热门标的。服务端缓存 60 秒 |
| `GET /api/search?q=<关键字>` | `q`（代码或中文名，模糊） | 全市场代码搜索 → `[{code, name, market}]` |
| `GET /health` | 无 | 健康检查 `{"status":"ok"}` |

### quote / markets 返回字段

```
code        标的编码（编码规则见第 4 节）
date        最近交易日 YYYY-MM-DD
open/high/low/close    OHLC（float）
prev        前一交易日收盘
change      涨跌额 = close - prev
change_pct  涨跌幅（小数，0.0123 = 1.23%）
vol         成交量（各市场原始口径）
history     [(date, close), ...] 最近 30 个交易日收盘
```

### 指标口径（/api/indicators）

| 组 | 键 | 算法 |
|---|---|---|
| MA | MA5 / MA10 / MA20 / MA30 / MA60 | 收盘价简单移动平均 |
| MACD | DIF / DEA / MACD | EMA12−EMA26；DEA=EMA9(DIF)；MACD=2×(DIF−DEA) |
| RSI | RSI6 / RSI12 / RSI24 | 分窗口涨跌均值 RS 法，0~100 |
| KDJ | K / D / J | 9 日 RSV；D 平滑 K；J=3K−2D |
| BOLL | UP / MID / DOWN | MA20 ± 2×标准差（样本估计） |

历 * 指标序列数组与 `bars` 一一对齐；某标的上市不足窗口时对应位置为 `null`。

## 4. 标的编码规则（code 参数）

| 市场 | 格式 | 示例 |
|---|---|---|
| A股个股 | `sh|sz + 6位代码`（交易所前缀） | `sh600519` 贵州茅台 |
| A股指数 | 同上，`000/399` 开头自动识别 | `sh000001` 上证指数、`sz399006` 创业板指 |
| 港股 | `hk:5位补零` | `hk:00700` 腾讯控股 |
| 美股 | `us:大写Ticker` | `us:AAPL`、`us:TSLA` |
| 加密货币 | `crypto:BASE/QUOTE` | `crypto:BTC/USDT` |
| 国内期货 | `fut:品种+0`（主力连续 82 品种，sm701 fut_daily 同源） | `fut:IH0` 上证50、`fut:UR0` 尿素 |

不确定编码？先查 `GET /api/search?q=茅台` → 返回 `sh600519`。

## 5. 本地数据库直连（Python，同机/挂载盘，推荐大数据量离线回测）

在 quant 机上（或挂载了 `/media/usb/ashare` 的环境）开发时，无需走 HTTP，
直接用统一读取层 `data/unified.py`：五库联邦、市场即参数，与 REST 口径完全一致。

```python
import sys; sys.path.insert(0, "/home/lan/ai-quant")
from data import unified as u

u.paths()                                  # 查看命中的库文件
bars = u.get_bars("sh600519", tail=300)    # [Bar(code,date,open,high,low,close,vol)]
bars = u.get_bars("fut:RB0", tail=250)
for m in ["cn", "hk", "us", "crypto", "fut"]:
    print(len(u.universe(m)))              # [(code, name), ...]
print(u.counts())                          # 各市场标的数/记录数/时间范围

# 美股 P1 专用 (前复权 + 未复权 raw_close):
u.has_us_p1()                              # 是否已导入 us_daily_qfq
bars = u.get_us_p1_bars("us:AAPL", tail=340)
print(u.us_p1_universe()[:3])              # 标普500 当前成分
```

### 库结构

```
A股库   scripts/cli/stock_cache.db（约 2.2G，1272 万根日K）
  daily_bars    日K（code, date, open, high, low, close, ...）
  stocks        代码表 / 行业 / 市值分层
  f10_cache     基本面
  minute_cache  分钟线

全球库 data/global_daily.db（约 1.3G）
  g.stocks      code, name, market(HK/US/CRYPTO/FUT)
  g.daily_bars  market, code, date, open, high, low, close, vol
```

完整库文件可在 [下载中心](https://quant.tail6a6cc8.ts.net/downloads) 打包带走。

## 6. 注意事项

- **频率**：本站读本地库，无强制限频；`/api/markets` 有 60 秒缓存，请勿高频刷新全市场。
- **数据口径**：日频（收盘后落库，由 cron 每交易日更新）。加密 7×24 交易含周末，A股/港美股各交易日历对齐。
- **新标的历史不足**（如缺少 MA60）：指标返回 `null` 属正常，按位置对齐 bars 消费即可。
- **错误处理**：无数据返回 `{"code":"...","error":"no data"}`；HTTP 500 说明服务端异常，先看 [状态页](https://quant.tail6a6cc8.ts.net/quant)。
- **周期扩展**：如需 MA90/MA120 等额外窗口，请用 `/api/indicators` 拿原始 K 线后自算；
  指标实现同源 `factors/technical.py`，可下载复用。

## 7. 前端组件复用（可选）

站内 K 线图为零依赖 Canvas 实现（主图 K 线 + MA/BOLL 通道叠加，副图 VOL/MACD/KDJ/RSI，
十字光标联动、面板可开合）。如需在自己的系统里复刻，直接消费
`/api/indicators` 的 `bars`（每根已含指标值）配合 `indicators` 序列数组即可，无需再自行计算指标。
