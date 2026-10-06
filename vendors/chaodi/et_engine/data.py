# -*- coding: utf-8 -*-
"""本地数据层：读取 stock_predict/stock_cache.db（后复权 + adjust 系数）。

约定：
    · 库内 daily_bars 一律存后复权(hfq)；adjust.k 把 hfq 缩放为"乘法前复权"，
      即最后一根 bar 的价格 ≈ 最新真实价（与云端 close 口径一致）。
    · 本地代码 sh600000/sz000001/bj920002 ↔ 服务端 600000.SH/000001.SZ/920002.BJ。
    · volume 单位为手（100 股），amount = volume * 100 * 均价。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(_HERE)),
                           "stock_predict", "stock_cache.db")
DB_PATH = os.environ.get("STOCK_DB", _DEFAULT_DB)

_lock = threading.Lock()
_tls = threading.local()


def db_path():
    return DB_PATH


def _cx():
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _tls.conn = conn
    return conn


@contextmanager
def db_conn():
    yield _cx()


def close():
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        conn.close()
        _tls.conn = None


def reopen():
    """丢弃当前线程的连接引用（多进程 worker 启动时用，避免继承 fork 连接）。"""
    _tls.conn = None


# ---------------- 代码转换 ----------------

def to_local(symbol):
    """600000.SH -> sh600000；sh600000 -> sh600000；裸 6 位代码自动补市场前缀。"""
    s = symbol.strip().upper()
    if "." in s:
        code, mkt = s.split(".")
        return mkt.lower() + code
    s = s.lower()
    if len(s) > 2 and s[:2] in ("sh", "sz", "bj"):
        return s
    if len(s) == 6 and s.isdigit():
        if s[0] in "69" or s[:2] in ("51", "56", "58"):
            return "sh" + s
        if s[0] in "03" or s[:2] in ("15", "16", "18"):
            return "sz" + s
        if s[0] in "48" or s[:2] == "92":
            return "bj" + s
    return s


def to_symbol(local):
    """sh600000 -> 600000.SH；裸代码自动补前缀。"""
    s = to_local(local)
    if len(s) > 2 and s[:2] in ("sh", "sz", "bj"):
        return s[2:] + "." + s[:2].upper()
    return local.strip().upper()


def board_of(local):
    """返回板块名（与云端 basic_filter.boards 口径一致）。"""
    s = local.lower()
    code = s[2:] if s[:2] in ("sh", "sz", "bj") else s
    if s.startswith("bj"):
        return "北交所"
    if code.startswith("688") or code.startswith("689"):
        return "科创板"
    if code.startswith("300") or code.startswith("301"):
        return "创业板"
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("00"):
        return "深主板"
    return "其他"


def limit_pct(symbol, name=""):
    """涨跌幅限制（小数）：创业板/科创板 20%，北交所 30%，ST 5%，主板 10%。"""
    code = symbol.split(".")[0] if "." in symbol else symbol
    if "ST" in (name or "").upper():
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if symbol.upper().endswith(".BJ") or code.startswith(("92", "83", "87", "43")):
        return 0.30
    return 0.10


# ---------------- 行情读取 ----------------

def adjust_factor(local):
    row = _cx().execute("SELECT k FROM adjust WHERE code=?", (local,)).fetchone()
    return float(row[0]) if row and row[0] else 1.0


def load_bars(local, limit=None, adjust=True):
    """读取单只日K，按 adjust 缩放到前复权（乘法前复权）。

    返回 dict:
        dates  list[str]
        open/high/low/close/volume  np.ndarray(float64)
    """
    sql = ("SELECT date,open,high,low,close,vol FROM daily_bars "
           "WHERE code=? ORDER BY date")
    rows = _cx().execute(sql, (local,)).fetchall()
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    if not rows:
        return None
    k = adjust_factor(local) if adjust else 1.0
    dates = [r[0] for r in rows]
    o = np.array([r[1] or np.nan for r in rows], dtype=np.float64)
    h = np.array([r[2] or np.nan for r in rows], dtype=np.float64)
    l = np.array([r[3] or np.nan for r in rows], dtype=np.float64)
    c = np.array([r[4] or np.nan for r in rows], dtype=np.float64)
    v = np.array([r[5] or 0.0 for r in rows], dtype=np.float64)
    if k and k > 0 and adjust:
        o, h, l, c = o * k, h * k, l * k, c * k
    # 腾讯科创板(688/689)日K成交量单位是「股」，其余是「手」；统一为手
    code6 = local[2:] if local[:2] in ("sh", "sz", "bj") else local
    if code6.startswith(("688", "689")):
        v = v / 100.0
    return {"local": local, "symbol": to_symbol(local), "dates": dates,
            "open": o, "high": h, "low": l, "close": c, "volume": v}


def list_universe(min_bars=250, max_bars=None, boards=None, exclude_st=True,
                  exclude_etf=True, exclude_delisted=True):
    """返回 [(local_code, name, n_bars, last_date), ...]，过滤新上市/ST/退市。"""
    meta = {r[0]: (r[1], r[2] or "", r[3] or 0.0)
            for r in _cx().execute("SELECT code,name,industry,mktcap FROM stocks")}
    out = []
    q = ("SELECT code, COUNT(*), MAX(date) FROM daily_bars "
         "GROUP BY code HAVING COUNT(*) >= ?")
    for code, n, last in _cx().execute(q, (min_bars,)):
        name, industry, _cap = meta.get(code, ("", "", 0.0))
        board = board_of(code)
        if boards and board not in boards:
            continue
        if exclude_etf and (industry == "ETF" or name.endswith("ETF")
                            or code[2:5] in ("510", "511", "512", "513", "515",
                                             "516", "518", "159", "588")):
            continue
        if exclude_st and ("ST" in name.upper()):
            continue
        if exclude_delisted and ("退" in name or "PT" in name.upper()):
            continue
        if max_bars and n > max_bars:
            n = max_bars
        out.append((code, name, n, last))
    return out


def load_panel(codes, limit=800, adjust=True, min_bars=250):
    """批量读取（顺序版），返回 {local: bars}。扫描器里用线程池按需读取。"""
    out = {}
    for c in codes:
        b = load_bars(c, limit=limit, adjust=adjust)
        if b and len(b["dates"]) >= min_bars:
            out[c] = b
    return out
