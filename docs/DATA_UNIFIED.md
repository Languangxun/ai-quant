# 数据层统一 (v0.9 开发中)

五库合一：A 股 + 港/美/加密/期货日 K，同一 `data/unified.py` 接口。

## 编码

| 市场 | mcode | 物理库 |
|---|---|---|
| A股 | `sh600000`（裸码，兼容 cli_bridge） | `scripts/cli/stock_cache.db`（12.7M 根） |
| 港股 | `hk:00700` | `data/global_daily.db`（sniper 快照，2.4M 根） |
| 美股 | `us:AAPL` | 同上（6.4M 根） |
| 加密 | `crypto:BTC/USDT`（sniper 原生 `/`） | 同上（5 万根） |
| 期货 | `fut:RB0`（国内主力连续 82 品种，sm701 fut_daily 同源） | 同上（19.6 万根，2010 起） |

## 文件

- `scripts/build_global_daily.py`：从 market-sniper 全量构建快照
 （只搬 `stocks` + `daily_bars`，55M 行分钟线不搬；产物 1.3G，进 U 盘）。
- `data/unified.py`：ATTACH 联邦读取（`get_bars/universe/trade_dates/counts`），
  路径自动 fallback（仓库内 → `/media/usb/ashare/`）。
- `scripts/sync_global_daily.py`：标准库增量（HK 全量成分/美股核心池/加密全 20/
  期货 82 主力连续），
  HK/US 走腾讯 `proxy.finance.qq.com` HTTP 接口（HTTPS 域名会被 WAF 拦截），
  东财兜底；期货优先读桌面 `~/桌面/sm701/stock_cache.db` 的 `fut_daily`
  （与 sm701 生产模型训练数据同源，`SM701_DB` 可覆盖），缺库回落新浪
  `InnerFuturesNewService.getDailyKLine`（全历史日K）；Pi 上 `GLOBAL_DB`
  默认指向 U 盘库。
- `data/us_majors.yaml`：美股增量核心池（全历史随快照走，增量只追核心）。
- Pi cron：`0 18 * * * .../sync_global_daily.py`（每日盘后）。

## 网络容错

Pi 网口（sunxi-gmac）实测每天有 20~26 次 `Link is Down` 抖动，导致 DNS
瞬时解析失败（`socket.gaierror`）。对应措施：

- DNS：NetworkManager 主/备 DNS 指向 `223.5.5.5`/`119.29.29.29`（不依赖
  路由器 192.168.3.1 的转发）；`scripts/pi_send_report.py` 在解析失败时
  用公共 DNS 直查 A 记录并把 SMTP 主机钉到 IP 兜底。
- LLM：`agent/stock_decision.py` 对网络类错误同样降级纯代码决策
  （`is_network_error`）。
- 邮件：退避重试 5 次 + 当日去重标记（`~/.cache/ai_quant/sent_daily_*`）
  + 17:20/20:20 两个补发时点。
- 监控：`scripts/pi_healthcheck.sh` 在健康快照里输出 24h 链路抖动次数。

## 口径

- 日 K 升序 `[Bar]`，尾部为最新；`tail=N` 取末尾 N 根。
- 加密代码归一：输入 `BTC-USDT`/`BTC/USDT` 均可，库内为 `BTC/USDT`。
- A 股仍走原 cli_bridge 口径（市值排序 universe），其余按代码排序。
