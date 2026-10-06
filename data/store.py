"""data.store: 统一行情存储 (SQLite 缓存 + 批量读取 + 索引优化).

设计参考:
- zvt Schema.record_data/query_data: 统一 record/query, provider 可插拔,
  增量更新 (MIT, zvtvz/zvt).
- QUANTAXIS QASU/QAData: 本地市场数据库 + 内存数据结构 (MIT, yutiansut/QUANTAXIS).

本实现保持与现有 scripts/cli/stock_cache.db 兼容 (不改表结构),
只做三件事让回测/实盘更快:
1. 单例连接 + WAL + 批量查询 (一次取多只, 避免逐只 db_rows 循环建连).
2. ensure_indexes(): 给 daily_bars(code,date) 建索引 (首次自动建, 秒级).
3. to_bars(): 把 dict 行转为 core.types.Bar, 供策略层直接消费.

旧接口 data/stock/cli_bridge.py 保持可用; 新代码优先用本模块.
"""
import os
import sqlite3

from core.types import Bar

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "scripts", "cli", "stock_cache.db")

_conn = None


def connect(db_path=None):
    """进程内单例读连接 (WAL, 超时 30s)."""
    global _conn
    if _conn is None:
        path = db_path or DB_PATH
        con = sqlite3.connect(path, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        _conn = con
    return _conn


def close():
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    finally:
        _conn = None


def ensure_indexes(db_path=None):
    """给大表建复合索引, 回测 scan 提速最明显 (幂等)."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return False
    con = sqlite3.connect(path, timeout=60)
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_bars_code_date "
                    "ON daily_bars(code, date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bars_date "
                    "ON daily_bars(date)")
        con.commit()
    finally:
        con.close()
    return True


def _row_to_bar(code, r):
    return Bar(code=code, date=r["date"], open=r["open"] or 0.0,
               high=r["high"] or 0.0, low=r["low"] or 0.0,
               close=r["close"] or 0.0, vol=r["vol"] or 0.0)


def get_bars(code, tail=None, db_path=None):
    """单只日K升序. tail=N 只取末尾 N 根 (等价 cli_bridge.db_rows)."""
    con = connect(db_path)
    if tail:
        rows = con.execute(
            "SELECT date,open,high,low,close,vol FROM ("
            " SELECT date,open,high,low,close,vol FROM daily_bars"
            " WHERE code=? ORDER BY date DESC LIMIT ?"
            ") ORDER BY date", (code, int(tail))).fetchall()
    else:
        rows = con.execute(
            "SELECT date,open,high,low,close,vol FROM daily_bars"
            " WHERE code=? ORDER BY date", (code,)).fetchall()
    return [_row_to_bar(code, r) for r in rows]


def get_batch(codes, tail=None):
    """批量取多只 (一次复用同一连接, 比循环 db_rows 少建连)."""
    return {c: get_bars(c, tail=tail) for c in codes}


def trade_dates(start=None, db_path=None):
    con = connect(db_path)
    if start:
        rows = con.execute(
            "SELECT DISTINCT date FROM daily_bars WHERE date>=? ORDER BY date",
            (str(start),)).fetchall()
    else:
        rows = con.execute(
            "SELECT DISTINCT date FROM daily_bars ORDER BY date").fetchall()
    return [r[0] for r in rows]


def latest_date(db_path=None):
    con = connect(db_path)
    r = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()
    return r[0] if r and r[0] else None


def counts(db_path=None):
    """库概况: 股票数/总K线数/起止日期 (供状态页与 U 盘校验)."""
    con = connect(db_path)
    n_code = con.execute("SELECT COUNT(DISTINCT code) FROM daily_bars"
                         ).fetchone()[0]
    n_row = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
    mx = con.execute("SELECT MIN(date), MAX(date) FROM daily_bars").fetchone()
    return {"codes": n_code, "rows": n_row,
            "min_date": mx[0], "max_date": mx[1]}
