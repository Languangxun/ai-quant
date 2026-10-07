#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全球库增量同步 data/global_daily.db (标准库 only, Pi 可直接跑).

数据源 (与 stock_web.py 同源, 2026-10 实测 Pi 可达):
  HK: 腾讯 ifzq fqkline (全量 HK 成分, 约 2900 只, Pi 约 15-25 分钟)
  US: 东财 push2his 105/106/107 轮询 (仅 data/us_majors.yaml 核心池)
  加密: Gate 现货日K (CRYPTO 全 20 只, 秒级)

用法:
  python3 scripts/sync_global_daily.py [--db PATH] [--only hk,us,crypto]
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


def _hk_bars(code5):
    txt = _get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param=hk{code5},day,,,500,qfq")
    d = ((json.loads(txt).get("data") or {}).get("hk" + code5)) or {}
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


def _us_bars(symbol, since=None):
    """since: YYYY-MM-DD, 只拉增量 (beg=次日, 轻量防反爬)."""
    beg = ""
    if since and len(since) >= 10:
        try:
            d = datetime.date.fromisoformat(since[:10]) + \
                datetime.timedelta(days=1)
            beg = d.strftime("%Y%m%d")
        except ValueError:
            beg = ""
    last = None
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


def _majors():
    import yaml
    p = os.path.join(BASE, "data", "us_majors.yaml")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f).get("us_majors") or []


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get(
        "GLOBAL_DB", PI_DB if os.path.exists(PI_DB) else DEFAULT_DB))
    ap.add_argument("--only", default="hk,us,crypto")
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
            for i, c in enumerate(codes):
                try:
                    n += _upsert(con, "HK", c, _hk_bars(c))
                except Exception as e:
                    print(f"  HK {c} 跳过: {str(e)[:80]}")
                time.sleep(0.3)  # 礼貌延迟，防腾讯反爬
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
    finally:
        con.close()
    print(f"TOTAL +{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
