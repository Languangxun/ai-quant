"""data.unified: 五库合一读取层 (A股 + 港/美/加密/期货日K).

统一编码 (沿用 market-sniper 市场前缀习惯):
  A股: sh600000 / sz000725 (裸码, 与现有 cli_bridge 兼容)
  港股: hk:00700   美股: us:AAPL   加密: crypto:BTC/USDT (sniper 原生)
  期货: fut:RB0 (国内主力连续 82 品种, sm701 fut_daily 同源编码)

物理库 (ATTACH 联邦, 不搬数据, 单连接跨库查):
  ashare: scripts/cli/stock_cache.db (A股 12.7M 根, 2.2G)
          Pi 上另有 /media/usb/ashare/stock_cache.db 同内容归档
  global: data/global_daily.db (港/美/加密/期货日线, 由
          scripts/build_global_daily.py 构建快照 + sync_global_daily.py
          增量维护; 期货自 2026-10 起为 sm701 fut_daily 同源)
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
    if mcode.startswith(("hk:", "us:", "crypto:", "fut:")):
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


US_P1_TABLE = "us_daily_qfq"
US_P1_TICKERS = os.path.join(BASE_DIR, "data", "us_sp500.txt")


def _g_table(name):
    connect()
    try:
        return bool(_conn.execute(
            "SELECT 1 FROM g.sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone())
    except Exception:
        return False


def has_us_p1():
    """global 库是否已导入美股 P1 qfq 表."""
    return _has_g() and _g_table(US_P1_TABLE)


def us_p1_universe():
    """标普500 当前成分 [(us:CODE, CODE)]. 缺清单时回退库内 US 成分."""
    out = []
    try:
        with open(US_P1_TICKERS, encoding="utf-8") as f:
            for line in f:
                c = line.strip()
                if c and not c.startswith("#"):
                    out.append(("us:" + c, c))
    except OSError:
        pass
    if out:
        return out
    return universe("us")


def get_us_p1_bars(mcode, tail=None):
    """美股 P1 qfq 日K -> [Bar] (含未复权 raw_close 供美元成交额).

    数据来自 g.us_daily_qfq; raw_close 取 g.daily_bars 同代码同日未复权
    close, 缺失时回退 qfq close (最新日两者一致).
    """
    if not has_us_p1():
        return []
    m, code = _split(mcode)
    if m != "us":
        return []
    code = code.upper()
    con = connect()
    base = (f"SELECT q.date,q.open,q.high,q.low,q.close,q.vol,"
            f"COALESCE(r.close,q.close) AS raw_close "
            f"FROM g.{US_P1_TABLE} q LEFT JOIN g.daily_bars r "
            f"ON r.market='US' AND r.code=q.code AND r.date=q.date "
            f"WHERE q.code=?")
    if tail:
        sql = (f"SELECT * FROM ({base} ORDER BY q.date DESC LIMIT ?) "
               f"ORDER BY date")
        args = (code, int(tail))
    else:
        sql = base + " ORDER BY q.date"
        args = (code,)
    rows = con.execute(sql, args).fetchall()
    label = f"us:{code}"
    return [Bar(code=label, date=r["date"], open=r["open"] or 0.0,
                high=r["high"] or 0.0, low=r["low"] or 0.0,
                close=r["close"] or 0.0, vol=r["vol"] or 0.0,
                raw_close=r["raw_close"] or 0.0)
            for r in rows]


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
    mm = {"hk": "HK", "us": "US", "crypto": "CRYPTO",
          "fut": "FUT"}[m]
    cc = code.upper() if m in ("us", "fut") else code
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
    mm = {"hk": "HK", "us": "US", "crypto": "CRYPTO",
          "fut": "FUT"}[market]
    rows = connect().execute(
        "SELECT code, name FROM g.stocks WHERE market=? ORDER BY code",
        (mm,)).fetchall()
    pre = {"hk": "hk:", "us": "us:", "crypto": "crypto:",
           "fut": "fut:"}[market]
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
    mm = {"hk": "HK", "us": "US", "crypto": "CRYPTO",
          "fut": "FUT"}[market]
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
        for m, mm in (("hk", "HK"), ("us", "US"), ("crypto", "CRYPTO"),
                  ("fut", "FUT")):
            r = con.execute("SELECT COUNT(DISTINCT code), COUNT(*), "
                            "MIN(date), MAX(date) FROM g.daily_bars "
                            "WHERE market=?", (mm,)).fetchone()
            out[m] = {"codes": r[0], "rows": r[1],
                      "min": r[2], "max": r[3]}
    return out
