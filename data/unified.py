"""data.unified: 四库合一读取层 (A股 + 港/美/加密日K).

统一编码 (沿用 market-sniper 市场前缀习惯):
  A股: sh600000 / sz000725 (裸码, 与现有 cli_bridge 兼容)
  港股: hk:00700   美股: us:AAPL   加密: crypto:BTC/USDT (sniper 原生)

物理库 (ATTACH 联邦, 不搬数据, 单连接跨库查):
  ashare: scripts/cli/stock_cache.db (A股 12.7M 根, 2.2G)
          Pi 上另有 /media/usb/ashare/stock_cache.db 同内容归档
  global: data/global_daily.db (港/美/加密日线, 8.86M 根, 1.3G,
          由 scripts/build_global_daily.py 从 market-sniper 快照构建)
          Pi 上路径 /media/usb/ashare/global_daily.db

设计参考 zvt Schema.query_data (统一 query, 市场即参数) 与
QUANTAXIS QAData (内存联邦)，详见 THIRD_PARTY_NOTICES.md (MIT 合规).
"""
import os
import sqlite3

from core.types import Bar

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASHARE_CANDIDATES = [
    os.path.join(BASE_DIR, "scripts", "cli", "stock_cache.db"),
    "/media/usb/ashare/stock_cache.db",
]
GLOBAL_CANDIDATES = [
    os.path.join(BASE_DIR, "data", "global_daily.db"),
    "/media/usb/ashare/global_daily.db",
]

_conn = None
_paths = {}


def _pick(cands):
    for p in cands:
        if p and os.path.exists(p):
            return p
    return ""


def connect():
    """单例联邦连接 (ashare=主库, global=ATTACH). 缺库时对应市场返回空."""
    global _conn, _paths
    if _conn is None:
        a = _pick(ASHARE_CANDIDATES)
        g = _pick(GLOBAL_CANDIDATES)
        if not a and not g:
            raise FileNotFoundError("A股库与全球库均不存在")
        con = sqlite3.connect(a or g, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        if g and (not a or os.path.abspath(g) != os.path.abspath(a)):
            con.execute("ATTACH ? AS g", (g,))
        _paths = {"ashare": a, "global": g}
        _conn = con
    return _conn


def close():
    global _conn, _paths
    try:
        if _conn is not None:
            _conn.close()
    finally:
        _conn, _paths = None, {}


def paths():
    connect()
    return dict(_paths)


def _split(mcode):
    mcode = str(mcode or "").strip()
    if mcode.startswith(("hk:", "us:", "crypto:")):
        m, c = mcode.split(":", 1)
        return m, c
    return "cn", mcode


def _has_g():
    connect()
    try:
        _conn.execute("SELECT 1 FROM g.stocks LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def get_bars(mcode, tail=None):
    """统一日K升序 -> [Bar]. mcode 如 sh600000 / hk:00700."""
    m, code = _split(mcode)
    con = connect()
    if m == "cn":
        if not _paths.get("ashare"):
            return []
        sql = ("SELECT date,open,high,low,close,vol FROM daily_bars "
               "WHERE code=? ORDER BY date")
        args = (code,)
        if tail:
            sql = ("SELECT date,open,high,low,close,vol FROM (SELECT "
                   "date,open,high,low,close,vol FROM daily_bars WHERE "
                   "code=? ORDER BY date DESC LIMIT ?) ORDER BY date")
            args = (code, int(tail))
        rows = con.execute(sql, args).fetchall()
        return [Bar(code=code, date=r["date"], open=r["open"] or 0.0,
                    high=r["high"] or 0.0, low=r["low"] or 0.0,
                    close=r["close"] or 0.0, vol=r["vol"] or 0.0)
                for r in rows]
    if not _has_g():
        return []
    mm = {"hk": "HK", "us": "US", "crypto": "CRYPTO"}[m]
    cc = code.upper() if m in ("us",) else code
    if m == "crypto":
        cc = code.upper().replace("-", "/")  # 库内为 sniper 原生 BTC/USDT
    sql = ("SELECT date,open,high,low,close,volume FROM g.daily_bars "
           "WHERE market=? AND code=? ORDER BY date")
    args = (mm, cc)
    if tail:
        sql = ("SELECT date,open,high,low,close,volume FROM (SELECT "
               "date,open,high,low,close,volume FROM g.daily_bars WHERE "
               "market=? AND code=? ORDER BY date DESC LIMIT ?) "
               "ORDER BY date")
        args = (mm, cc, int(tail))
    rows = con.execute(sql, args).fetchall()
    label = f"{m}:{cc}"
    return [Bar(code=label, date=r["date"], open=r["open"] or 0.0,
                high=r["high"] or 0.0, low=r["low"] or 0.0,
                close=r["close"] or 0.0, vol=r["volume"] or 0.0)
            for r in rows]


def universe(market="cn"):
    """市场成分 [(mcode, name)]. cn 走 cli_bridge (市值排序), 其余走 g.stocks."""
    if market == "cn":
        from data.stock import cli_bridge as b
        return [(c, n) for c, n, _i, _m in
                b.universe(min_bars=100, max_scan=5000)]
    if not _has_g():
        return []
    mm = {"hk": "HK", "us": "US", "crypto": "CRYPTO"}[market]
    rows = connect().execute(
        "SELECT code, name FROM g.stocks WHERE market=? ORDER BY code",
        (mm,)).fetchall()
    pre = {"hk": "hk:", "us": "us:", "crypto": "crypto:"}[market]
    return [(pre + r["code"], r["name"] or "") for r in rows]


def trade_dates(market="cn", start=None):
    con = connect()
    if market == "cn":
        if not _paths.get("ashare"):
            return []
        q = "SELECT DISTINCT date FROM daily_bars"
        a = ()
        if start:
            q += " WHERE date>=?"
            a = (str(start),)
        return [r[0] for r in
                con.execute(q + " ORDER BY date", a).fetchall()]
    if not _has_g():
        return []
    mm = {"hk": "HK", "us": "US", "crypto": "CRYPTO"}[market]
    q = "SELECT DISTINCT date FROM g.daily_bars WHERE market=?"
    a = [mm]
    if start:
        q += " AND date>=?"
        a.append(str(start))
    return [r[0] for r in
            con.execute(q + " ORDER BY date", a).fetchall()]


def counts():
    """四市场概况 {market: {codes, rows, min, max}}."""
    out = {}
    con = connect()
    if _paths.get("ashare"):
        r = con.execute("SELECT COUNT(DISTINCT code), COUNT(*), MIN(date), "
                        "MAX(date) FROM daily_bars").fetchone()
        out["cn"] = {"codes": r[0], "rows": r[1],
                     "min": r[2], "max": r[3]}
    if _has_g():
        for m, mm in (("hk", "HK"), ("us", "US"), ("crypto", "CRYPTO")):
            r = con.execute("SELECT COUNT(DISTINCT code), COUNT(*), "
                            "MIN(date), MAX(date) FROM g.daily_bars "
                            "WHERE market=?", (mm,)).fetchone()
            out[m] = {"codes": r[0], "rows": r[1],
                      "min": r[2], "max": r[3]}
    return out
