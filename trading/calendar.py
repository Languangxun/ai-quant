"""A股交易日历。

规则：
- 周六/周日非交易日
- 法定节假日默认读 `config/holidays.json`（交易所公告，每年更新）
- 15:00 前下单记当日 T；15:00 后顺延下一交易日
"""
import json
import os
from datetime import date, datetime, time, timedelta

CUTOFF = time(15, 0)
DEFAULT_HOLIDAYS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "holidays.json")


def load_holidays(path=None):
    """读取休市日配置；支持 {"closed": {...}} / {"closed_weekdays": []} / []。

    路径优先 AIQUANT_HOLIDAYS 环境变量，其次 config/holidays.json；
    缺失或格式错误时返回空集合（退化为仅跳过周末）。
    """
    path = path or os.environ.get("AIQUANT_HOLIDAYS") or DEFAULT_HOLIDAYS
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    if isinstance(data, dict):
        data = data.get("closed") or data.get("closed_weekdays") or []
    if isinstance(data, dict):
        data = list(data)
    return {str(d) for d in data}


class TradingCalendar:
    """交易日历。默认周末 + 法定节假日，可注入 holidays 覆盖。"""

    def __init__(self, holidays=None):
        self.holidays = load_holidays() if holidays is None else set(holidays)

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:  # Sat=5 Sun=6
            return False
        if d.isoformat() in self.holidays:
            return False
        return True

    def next_trading_day(self, d: date, offset: int = 1) -> date:
        """从 d 之后数第 offset 个交易日（不含 d 本身）。"""
        cur = d
        count = 0
        while count < offset:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                count += 1
        return cur

    def trade_date_of(self, dt: datetime) -> date:
        """下单时刻 -> 交易日期 T。

        - 交易日 15:00 前：当日
        - 交易日 15:00 后：下一交易日
        - 非交易日任意时刻：下一交易日
        """
        d = dt.date()
        if self.is_trading_day(d) and dt.time() <= CUTOFF:
            return d
        return self.next_trading_day(d)
