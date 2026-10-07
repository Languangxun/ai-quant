# 数据层统一 (v0.9 开发中)

四库合一：A 股 + 港/美/加密日 K，同一 `data/unified.py` 接口。

## 编码

| 市场 | mcode | 物理库 |
|---|---|---|
| A股 | `sh600000`（裸码，兼容 cli_bridge） | `scripts/cli/stock_cache.db`（12.7M 根） |
| 港股 | `hk:00700` | `data/global_daily.db`（sniper 快照，2.4M 根） |
| 美股 | `us:AAPL` | 同上（6.4M 根） |
| 加密 | `crypto:BTC/USDT`（sniper 原生 `/`） | 同上（5 万根） |

## 文件

- `scripts/build_global_daily.py`：从 market-sniper 全量构建快照
 （只搬 `stocks` + `daily_bars`，55M 行分钟线不搬；产物 1.3G，进 U 盘）。
- `data/unified.py`：ATTACH 联邦读取（`get_bars/universe/trade_dates/counts`），
  路径自动 fallback（仓库内 → `/media/usb/ashare/`）。
- `scripts/sync_global_daily.py`：标准库增量（HK 全量成分/美股核心池/加密全 20），
  Pi 上 `GLOBAL_DB` 默认指向 U 盘库。
- `data/us_majors.yaml`：美股增量核心池（全历史随快照走，增量只追核心）。
- Pi cron：`0 18 * * * .../sync_global_daily.py`（每日盘后）。

## 口径

- 日 K 升序 `[Bar]`，尾部为最新；`tail=N` 取末尾 N 根。
- 加密代码归一：输入 `BTC-USDT`/`BTC/USDT` 均可，库内为 `BTC/USDT`。
- A 股仍走原 cli_bridge 口径（市值排序 universe），其余按代码排序。
