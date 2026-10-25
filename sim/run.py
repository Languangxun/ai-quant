"""模拟盘统一入口（多市场）。

  python -m sim.run                 # 默认：股票模式（stock_run）
  python -m sim.run --mode fund     # 旧版：场外 ETF 联接 C 类基金（fund_run）
  python -m sim.run --mode hk --force      # 港股 sm701 (multi_run)
  python -m sim.run --mode us --force      # 美股 sm701
  python -m sim.run --mode crypto --force  # 加密 sm701 (7x24, 闸门跳过)
  python -m sim.run --mode all --force     # fund+hk+us+crypto 全量
  python -m sim.run --force         # 非交易日强制执行（测试用）

股票模式：CLI 选股 + deepseek-v4.1-flash + qwen3-embedding 记忆 + A股规则。
基金旧模式：etf-c 旧版，保留可切换（cron 无需改动）。
新多市场：sm701 44维暂代因子 (A股训练域外推), 无LLM, 详见 config/markets.yaml。

交易日闸门：周末与 `config/holidays.json` 法定休市日直接跳过，避免节假日
用陈旧行情误成交 (crypto 7x24 不设闸门)。
"""
import datetime
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = "stock"
    force = False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
            continue
        if a.startswith("--mode="):
            mode = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--force":
            force = True
            i += 1
            continue
        rest.append(a)
        i += 1
    from trading.calendar import TradingCalendar
    today = datetime.date.today()
    if mode == "crypto":
        pass  # 7x24 无闸门
    elif not force and not TradingCalendar().is_trading_day(today):
        print(f"[skip] {today} 非交易日（周末/节假日休市），模拟盘不运行；"
              f"如确需执行请加 --force")
        return 0
    if mode == "fund":
        from sim import fund_run
        return fund_run.main(rest)
    if mode in ("hk", "us", "crypto", "all", "multi",
                "fund-sm701"):
        from sim import multi_run
        m = {"fund-sm701": "fund"}.get(mode, mode)
        if m == "multi":
            m = "all"
        return multi_run.main(["--market", m] + rest)
    from sim import stock_run
    return stock_run.main(rest)


if __name__ == "__main__":
    main()
