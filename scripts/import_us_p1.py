#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入美股 P1 qfq 日线到 data/global_daily.db:us_daily_qfq.

来源: ~/桌面/sm701/stock_cache.db (fetch_us_qfq.py 产物, 503 只标普500
2010-01 起前复权日线); 目标: quant/ai-quant 的 global_daily.db.

用法:
  python3 scripts/import_us_p1.py --source ~/桌面/sm701/stock_cache.db
  python3 scripts/import_us_p1.py --source /tmp/us_p1_bundle.db --db data/global_daily.db --replace

只新增/补 us_daily_qfq 表, 不修改 US daily_bars (未复权源仍保留给
P1 的美元成交额 raw_close join).
"""
import argparse
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BASE, "data", "global_daily.db")
TABLE = "us_daily_qfq"
DDL = (f"CREATE TABLE IF NOT EXISTS {TABLE}("
       "code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, "
       "low REAL, close REAL, vol REAL, PRIMARY KEY(code,date))")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="sm701 stock_cache.db 或含 us_daily_qfq 的 bundle")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--replace", action="store_true",
                    help="清空目标表后全量重导 (默认增量 INSERT OR IGNORE)")
    a = ap.parse_args(argv)
    if not os.path.exists(a.source):
        return f"源库不存在: {a.source}"
    if not os.path.exists(a.db):
        return f"目标库不存在: {a.db}"

    con = sqlite3.connect(a.db, timeout=120)
    try:
        con.execute(DDL)
        con.execute("ATTACH ? AS src", (a.source,))
        n_src = con.execute(
            f"SELECT COUNT(*) FROM src.{TABLE}").fetchone()[0]
        if not n_src:
            return f"源库无数据: {a.source}:{TABLE}"
        if a.replace:
            con.execute(f"DELETE FROM main.{TABLE}")
        before = con.execute(f"SELECT COUNT(*) FROM main.{TABLE}").fetchone()[0]
        con.execute(
            f"INSERT OR IGNORE INTO main.{TABLE}"
            "(code,date,open,high,low,close,vol) "
            f"SELECT code,date,open,high,low,close,vol FROM src.{TABLE}")
        con.commit()
        after = con.execute(f"SELECT COUNT(*) FROM main.{TABLE}").fetchone()[0]
        rng = con.execute(
            f"SELECT COUNT(DISTINCT code),MIN(date),MAX(date) "
            f"FROM main.{TABLE}").fetchone()
    finally:
        con.close()
    return (f"导入完成: 源 {n_src:,} 行; 目标 {before:,} -> {after:,} 行 "
            f"(+{after - before:,}); {rng[0]} 代码 {rng[1]} ~ {rng[2]}")


if __name__ == "__main__":
    print(main())
