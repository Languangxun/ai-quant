#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全球库增量同步 data/global_daily.db (标准库 only, Pi 可直接跑).

数据源 (与 stock_web.py 同源, 2026-10 实测 Pi 可达):
  HK: 腾讯 proxy.finance.qq.com fqkline (HTTP) (全量 HK 成分, 约 2900 只)
  US: 腾讯 proxy.finance.qq.com fqkline (HTTP) + 东财兜底 (核心池)
  加密: Gate 现货日K (CRYPTO 全 20 只, 秒级)
  期货: 国内主力连续 82 品种 (FUT)。桌面优先读 sm701 副本库 fut_daily
        (SM701_DB 可覆盖, 与 sm701 生产模型训练数据同源); 缺库时回落
        新浪 InnerFuturesNewService.getDailyKLine (全历史日K)。

用法:
  python3 scripts/sync_global_daily.py [--db PATH] [--only hk,us,crypto,fut]
  cron (Pi, 每日 18:00): 0 18 * * * /usr/bin/python3 ~/ai-quant/scripts/sync_global_daily.py >> ~/ai-quant/logs/sync_global.log 2>&1
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BASE, "data", "global_daily.db")
PI_DB = "/media/usb/ashare/global_daily.db"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _get(url, enc="utf-8", timeout=20, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    last = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(enc, errors="ignore")
        except Exception as e:
            last = e
            time.sleep(1)
    raise RuntimeError(f"GET 失败 {url[:80]}: {last}")


TENCENT_KLINE = ("http://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
                 "fqkline/get")
HK_HOSTS = (
    TENCENT_KLINE,
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
US_SUFFIXES = (".OQ", ".N", ".NQ", ".P", "")


def _tencent_bars(code, host=TENCENT_KLINE):
    txt = _get(f"{host}?param={code},day,,,500,qfq")
    d = ((json.loads(txt).get("data") or {}).get(code)) or {}
    out = []
    for b in d.get("qfqday") or d.get("day") or []:
        try:
            if float(b[2]) <= 0:
                continue
            out.append((b[0], float(b[1]), float(b[3]), float(b[4]),
                        float(b[2]), float(b[5])))
        except (ValueError, IndexError):
            continue
    return out


def _hk_bars(code5):
    last = None
    for host in HK_HOSTS:
        try:
            out = _tencent_bars("hk" + code5, host)
            if out:
                return out
            last = f"{host} 空数据"
        except Exception as e:
            last = e
    raise RuntimeError(f"无数据: {last}")


def _us_bars(symbol, since=None):
    best = None
    last = None
    for suffix in US_SUFFIXES:
        code = f"us{symbol}{suffix}"
        try:
            out = _tencent_bars(code)
            if out and (best is None or
                        (len(out), out[-1][0]) > (len(best), best[-1][0])):
                best = out
        except Exception as e:
            last = e
    if best:
        return best

    beg = ""
    if since and len(since) >= 10:
        try:
            d = datetime.date.fromisoformat(since[:10]) + \
                datetime.timedelta(days=1)
            beg = d.strftime("%Y%m%d")
        except ValueError:
            beg = ""
    for mkt in (105, 106, 107):
        try:
            txt = _get("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                       f"?secid={mkt}.{symbol}&fields1=f1,f2,f3"
                       "&fields2=f51,f52,f53,f54,f55,f56"
                       f"&klt=101&fqt=1&beg={beg or 0}&end=20500101&lmt=120",
                       headers={"Referer": "https://quote.eastmoney.com/"})
            kl = ((json.loads(txt).get("data") or {}).get("klines")) or []
            out = []
            for line in kl:
                p = line.split(",")
                if len(p) < 6:
                    continue
                try:
                    if float(p[2]) <= 0:
                        continue
                    out.append((p[0], float(p[1]), float(p[3]), float(p[4]),
                                float(p[2]), float(p[5])))
                except (ValueError, IndexError):
                    continue
            if len(out) >= 20:
                return out
            last = f"secid={mkt} 仅 {len(out)} 根"
        except Exception as e:
            last = e
    raise RuntimeError(f"无数据: {last}")


def _crypto_bars(code):
    pair = code.replace("/", "_").replace("-", "_").upper()
    txt = _get("https://api.gateio.ws/api/v4/spot/candlesticks"
               f"?currency_pair={pair}&interval=1d&limit=500")
    out = []
    for b in json.loads(txt) or []:
        try:
            if float(b[2]) <= 0:
                continue
            out.append((datetime.datetime.fromtimestamp(
                            int(b[0]), datetime.timezone.utc
                            ).strftime("%Y-%m-%d"),
                        float(b[5]), float(b[3]), float(b[4]), float(b[2]),
                        float(b[6])))
        except (ValueError, IndexError):
            continue
    return sorted(out)


def _upsert(con, market, code, rows):
    cur_max = con.execute("SELECT MAX(date) FROM daily_bars "
                          "WHERE market=? AND code=?",
                          (market, code)).fetchone()[0] or ""
    new = [r for r in rows if r[0] > cur_max]
    if new:
        con.executemany("INSERT OR IGNORE INTO daily_bars(market, code, date,"
                        " open, high, low, close, volume) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        [(market, code) + tuple(r) for r in new])
    return len(new)


# ---- 期货 (FUT): 国内主力连续 82 品种, 桌面 sm701 副本库优先, 新浪兜底 ----
# 名称取自 stock_predict v7.0.1 _FUT_NAMES (郑商所/大商所/上期所/中金所/上能源)
FUT_NAMES = {
    "A0": "豆一", "AD0": "豆一专", "AG0": "沪银", "AL0": "沪铝",
    "AO0": "氧化铝", "AP0": "苹果", "AU0": "沪金", "B0": "豆二",
    "BC0": "国际铜", "BR0": "丁二烯胶", "BU0": "沥青", "BZ0": "苯乙烯专",
    "C0": "玉米", "CF0": "棉花", "CJ0": "红枣", "CS0": "玉米淀粉",
    "CU0": "沪铜", "CY0": "棉纱", "EB0": "苯乙烯", "EC0": "欧线集运",
    "EG0": "乙二醇", "FB0": "纤维板", "FG0": "玻璃", "FU0": "燃油",
    "HC0": "热卷", "I0": "铁矿石", "IC0": "中证500期货",
    "IF0": "沪深300期货", "IH0": "上证50期货", "IM0": "中证1000期货",
    "J0": "焦炭", "JD0": "鸡蛋", "JM0": "焦煤", "JR0": "粳稻",
    "L0": "塑料", "LC0": "碳酸锂", "LG0": "原木", "LH0": "生猪",
    "LR0": "籼稻", "LU0": "低硫燃油", "M0": "豆粕", "MA0": "甲醇",
    "NI0": "沪镍", "NR0": "20号胶", "OI0": "菜油", "OP0": "菜油专",
    "P0": "棕榈油", "PB0": "沪铅", "PD0": "钯金", "PF0": "短纤",
    "PG0": "LPG", "PK0": "花生", "PL0": "铂金", "PP0": "聚丙烯",
    "PR0": "数据专", "PS0": "数据PS", "PT0": "PTA专", "PX0": "对二甲苯",
    "RB0": "螺纹钢", "RI0": "早籼稻", "RM0": "菜粕", "RR0": "粳米",
    "RS0": "菜籽", "RU0": "橡胶", "SA0": "纯碱", "SC0": "原油",
    "SF0": "硅铁", "SH0": "烧碱", "SI0": "工业硅", "SM0": "锰硅",
    "SN0": "沪锡", "SP0": "纸浆", "SR0": "白糖", "SS0": "不锈钢",
    "TA0": "PTA", "TF0": "二债期货", "TS0": "短融期货", "UR0": "尿素",
    "V0": "PVC", "WH0": "小麦", "Y0": "豆油", "ZN0": "沪锌",
}
SM701_DB_CANDIDATES = [
    os.environ.get("SM701_DB") or "",
    os.path.expanduser("~/桌面/sm701/stock_cache.db"),
]
SINA_FUT_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php"
                "/var%20t=/InnerFuturesNewService.getDailyKLine?symbol=")


def _sm701_db():
    for p in SM701_DB_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _fut_bars_sina(code):
    """新浪 InnerFuturesNewService.getDailyKLine (jsonp 包一层 var t=([...]))."""
    txt = _get(SINA_FUT_URL + code)
    i, j = txt.find("("), txt.rfind(")")
    if i < 0 or j <= i:
        raise RuntimeError("新浪期货响应格式异常")
    out = []
    for b in json.loads(txt[i + 1:j]):
        try:
            c = float(b.get("c") or 0)
            if c <= 0:
                continue
            out.append((b["d"], float(b.get("o") or c), float(b.get("h") or c),
                        float(b.get("l") or c), c, float(b.get("v") or 0)))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _fut_ensure_stocks(con):
    names = {r[0] for r in con.execute(
        "SELECT code FROM stocks WHERE market='FUT'")}
    todo = [(c, n) for c, n in FUT_NAMES.items() if c not in names]
    if todo:
        con.executemany("INSERT OR REPLACE INTO stocks(market, code, name, "
                        "exchange, updated) VALUES('FUT', ?, ?, 'CN-FUT', "
                        "datetime('now'))", todo)
    con.commit()
    return len(todo)


def sync_fut(con, workers=4):
    """期货增量: sm701 副本库有则整表读增量 (快), 否则逐品种新浪拉取."""
    n_new = _fut_ensure_stocks(con)
    if n_new:
        print(f"  FUT stocks 登记 {n_new} 只", flush=True)
    codes = [r[0] for r in con.execute(
        "SELECT code FROM stocks WHERE market='FUT' ORDER BY code")]
    src = _sm701_db()
    n = 0
    if src:
        print(f"  FUT 源: sm701 副本库 {src}", flush=True)
        cur = dict(con.execute(
            "SELECT code, MAX(date) FROM daily_bars WHERE market='FUT' "
            "GROUP BY code").fetchall())
        scon = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        try:
            batch = []
            for code in codes:
                d0 = cur.get(code) or ""
                rows = scon.execute(
                    "SELECT date, open, high, low, close, vol FROM fut_daily "
                    "WHERE code=? AND date>? ORDER BY date",
                    (code, d0)).fetchall()
                for d, o, h, l, c, v in rows:
                    if not c or float(c) <= 0:
                        continue
                    batch.append(("FUT", code, d, float(o or 0),
                                  float(h or 0), float(l or 0), float(c),
                                  float(v or 0)))
            if batch:
                con.executemany("INSERT OR IGNORE INTO daily_bars(market, "
                                "code, date, open, high, low, close, volume)"
                                " VALUES(?,?,?,?,?,?,?,?)", batch)
                con.commit()
            n = len(batch)
        finally:
            scon.close()
    else:
        print("  FUT 源: 新浪 (无 sm701 副本库)", flush=True)
        for i, code in enumerate(codes, 1):
            try:
                cur_max = con.execute(
                    "SELECT MAX(date) FROM daily_bars WHERE market='FUT' "
                    "AND code=?", (code,)).fetchone()[0] or ""
                rows = [r for r in _fut_bars_sina(code) if r[0] > cur_max]
                if rows:
                    con.executemany(
                        "INSERT OR IGNORE INTO daily_bars(market, code, date,"
                        " open, high, low, close, volume) "
                        "VALUES('FUT',?,?,?,?,?,?,?)",
                        [(code,) + r for r in rows])
                    con.commit()
                    n += len(rows)
            except Exception as e:
                print(f"  FUT {code} 跳过: {str(e)[:80]}", flush=True)
            if i % 20 == 0:
                print(f"  FUT {i}/{len(codes)} +{n}", flush=True)
    print(f"FUT 新增 {n} 根", flush=True)
    return n


def _majors():
    import yaml
    p = os.path.join(BASE, "data", "us_majors.yaml")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f).get("us_majors") or []


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get(
        "GLOBAL_DB", PI_DB if os.path.exists(PI_DB) else DEFAULT_DB))
    ap.add_argument("--only", default="hk,us,crypto,fut")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(f"库不存在: {a.db} (先跑 build_global_daily.py)", file=sys.stderr)
        return 1
    only = set(a.only.split(","))
    con = sqlite3.connect(a.db, timeout=60)
    total = 0
    try:
        if "hk" in only:
            codes = [r[0] for r in con.execute(
                "SELECT code FROM stocks WHERE market='HK' ORDER BY code")]
            n = 0
            workers = max(1, int(a.workers))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_hk_bars, c): c for c in codes}
                for i, fut in enumerate(as_completed(futs), 1):
                    c = futs[fut]
                    try:
                        n += _upsert(con, "HK", c, fut.result())
                    except Exception as e:
                        print(f"  HK {c} 跳过: {str(e)[:80]}", flush=True)
                    if i % 200 == 0:
                        con.commit()
                        print(f"  HK {i}/{len(codes)} +{n}", flush=True)
            con.commit()
            print(f"HK 新增 {n} 根")
            total += n
        if "us" in only:
            n = 0
            majors = _majors()
            for i, c in enumerate(majors):
                try:
                    cur = con.execute(
                        "SELECT MAX(date) FROM daily_bars "
                        "WHERE market='US' AND code=?", (c,)).fetchone()[0]
                    n += _upsert(con, "US", c, _us_bars(c, cur))
                except Exception as e:
                    print(f"  US {c} 跳过: {str(e)[:100]}")
                time.sleep(1.5)  # 东财反爬严，慢速
                if i % 20 == 0:
                    con.commit()
            con.commit()
            print(f"US 新增 {n} 根")
            total += n
        if "crypto" in only:
            codes = [r[0] for r in con.execute(
                "SELECT code FROM stocks WHERE market='CRYPTO' ORDER BY code")]
            n = 0
            for c in codes:
                try:
                    n += _upsert(con, "CRYPTO", c, _crypto_bars(c))
                except Exception as e:
                    print(f"  CRYPTO {c} 跳过: {str(e)[:80]}")
            con.commit()
            print(f"CRYPTO 新增 {n} 根")
            total += n
        if "fut" in only:
            total += sync_fut(con, workers=max(1, int(a.workers)))
    finally:
        con.close()
    print(f"TOTAL +{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
