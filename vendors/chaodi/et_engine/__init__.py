# -*- coding: utf-8 -*-
"""策略本地化引擎（通达信语义 + stock_cache.db 数据）。

模块划分：
    data        本地 SQLite 缓存读取、代码转换、复权
    indicators  TDX 语义指标（MA/EMA/MACD/BOLL/KDJ/RSI/ATR/OBV/CCI/WR/DMI/SAR）
    signals     signal_* 布尔信号与连板/涨跌停统计
    strategies  18 个内置策略（filter/scoring/退出规则）
"""

__version__ = "1.0.0"
