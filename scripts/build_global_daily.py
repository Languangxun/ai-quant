#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建全球日K快照 data/global_daily.db (HK/US/加密, 日线 only).

来源: market-sniper/data/market_cache.db 的 stocks + daily_bars
(作者本人项目；分钟级 min_bars 55M 行不搬，只搬日线，供 Pi/回测用).
产物约 1.5G，可进 U 盘 (vfat 4G 上限内)，不入库 (.gitignore).

用法:
  python3 scripts/build_global_daily.py [--src PATH] [--dst PATH]
  SNIPER_DB=... python3 scripts/build_global_daily.py
"""
import argparse
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = "/home/lan/桌面/market-sniper/data/market_cache.db"
DEFAULT_DST = os.path.join(BASE, "data", "global_daily.db")
MIN_DATE = "1980-01-01"


def build(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    s = sqlite3.connect(src, timeout=60)
    d = sqlite3.connect(dst, timeout=60)
    try:
        d.execute("CREATE TABLE stocks(market TEXT, code TEXT, name TEXT, "
                  "exchange TEXT, updated TEXT, "
                  "PRIMARY KEY(market, code))")
        d.execute("CREATE TABLE daily_bars(market TEXT, code TEXT, "
                  "date TEXT, open REAL, high REAL, low REAL, close REAL, "
                  "volume REAL, amount REAL, source TEXT, "
                  "PRIMARY KEY(market, code, date))")
        n_stock = 0
        for r in s.execute("SELECT market, code, name, exchange "
                           "FROM stocks"):
            d.execute("INSERT INTO stocks VALUES(?,?,?,?,datetime('now'))", r)
            n_stock += 1
        n_bar = 0
        cur = s.execute("SELECT market, code, date, open, high, low, close,"
                        " volume, amount, source FROM daily_bars "
                        "WHERE date >= ?", (MIN_DATE,))
        batch = []
        for r in cur:
            batch.append(r)
            if len(batch) >= 5000:
                d.executemany("INSERT OR IGNORE INTO daily_bars VALUES"
                              "(?,?,?,?,?,?,?,?,?,?)", batch)
                n_bar += len(batch)
                batch = []
        if batch:
            d.executemany("INSERT OR IGNORE INTO daily_bars VALUES"
                          "(?,?,?,?,?,?,?,?,?,?)", batch)
            n_bar += len(batch)
        d.execute("CREATE INDEX idx_gb_code_date ON "
                  "daily_bars(market, code, date)")
        d.execute("CREATE INDEX idx_gb_date ON daily_bars(date)")
        d.commit()
        print(f"stocks={n_stock} bars={n_bar} -> {dst}")
        for r in d.execute("SELECT market, COUNT(*), COUNT(DISTINCT code), "
                           "MIN(date), MAX(date) FROM daily_bars "
                           "GROUP BY market"):
            print(" ", r)
    finally:
        s.close()
        d.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.environ.get("SNIPER_DB",
                                                    DEFAULT_SRC))
    ap.add_argument("--dst", default=DEFAULT_DST)
    a = ap.parse_args(argv)
    if not os.path.exists(a.src):
        print(f"源库不存在: {a.src}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(a.dst), exist_ok=True)
    build(a.src, a.dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
