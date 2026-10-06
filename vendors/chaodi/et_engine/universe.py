# -*- coding: utf-8 -*-
"""股票池元数据（名称/市值/行业）与 basic_filter 应用。

市值优先取 stock_cache.db 的 stocks 表；缺失时可用 refresh_from_eastmoney()
从东财代码表补齐（与 stock_gui.refresh_all_codes 同一接口，但只做 upsert，
不删旧行，避免影响现有工具）。
"""
import json
import time
import urllib.parse
import urllib.request

import numpy as np

from . import data
from .data import board_of

INDEX_PREFIX = ("sh000", "sh880", "sz399", "bj899")


def is_index(local):
    return local.startswith(INDEX_PREFIX)


def is_etf(local):
    code = local[2:] if local[:2] in ("sh", "sz", "bj") else local
    return code[:3] in ("510", "511", "512", "513", "515", "516", "518",
                        "159", "588", "560", "561", "562", "563", "159")


def _ensure_shares_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS et_shares("
        "code TEXT PRIMARY KEY, shares REAL, ts REAL)")
    conn.commit()


def load_meta():
    """返回 {local: {"name","industry","mktcap","shares"}}。"""
    out = {}
    for code, name, ind, cap in data._cx().execute(
            "SELECT code,name,industry,mktcap FROM stocks"):
        out[code] = {"name": name or "", "industry": ind or "",
                     "mktcap": float(cap or 0.0), "shares": 0.0}
    try:
        for code, shares in data._cx().execute(
                "SELECT code, shares FROM et_shares"):
            if code in out and shares:
                out[code]["shares"] = float(shares)
    except Exception:
        pass
    return out


def refresh_from_eastmoney(progress=None, timeout=20):
    """全A（含北交所）代码/名称/总市值/行业 upsert 到 stocks 表。"""
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    hosts = ("https://push2delay.eastmoney.com",
             "https://push2.eastmoney.com",
             "http://push2.eastmoney.com")
    ut = "fa5fd1943c7b386f172d6893dbfba10b"
    items, pn, fail_streak = [], 1, 0
    while pn <= 90:
        data_json = None
        for attempt in range(3):
            for host in hosts:
                url = (f"{host}/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
                       f"&fltt=2&invariant=0&fields=f12,f14,f20,f100"
                       f"&fs={urllib.parse.quote(fs, safe=':+,')}&ut={ut}")
                try:
                    req = urllib.request.Request(
                        url, headers={
                            "Referer": "https://quote.eastmoney.com/",
                            "User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        data_json = json.loads(
                            r.read().decode("utf-8", "replace"))
                    break
                except Exception:
                    time.sleep(0.8 + attempt * 1.5)
            if data_json:
                break
        if not data_json:
            fail_streak += 1
            if pn == 1 or fail_streak >= 3:
                if pn == 1:
                    raise RuntimeError("东财代码表拉取失败")
                break
            pn += 1
            continue
        fail_streak = 0
        diff = (data_json.get("data") or {}).get("diff") or {}
        batch = list(diff.values()) if isinstance(diff, dict) else diff
        if not batch:
            break
        for it in batch:
            code, name = it.get("f12"), it.get("f14") or ""
            cap = it.get("f20")
            ind = it.get("f100")
            if not code or len(code) != 6 or not isinstance(cap, (int, float)):
                continue
            if code.startswith(("4", "8", "92")):
                full = "bj" + code
            elif code[0] in "69" or code[:2] in ("51", "56", "58"):
                full = "sh" + code
            else:
                full = "sz" + code
            items.append((full, name, ind if isinstance(ind, str) else "",
                          float(cap)))
        if progress:
            progress(f"代码表 {len(items)} 只 (第{pn}页)")
        pn += 1
        time.sleep(0.8)
    if not items:
        raise RuntimeError("东财代码表为空")
    today = time.strftime("%Y-%m-%d")
    with data.db_conn() as conn:
        old_tier = {r[0]: r[1] for r in
                    conn.execute("SELECT code, tier FROM stocks")}
        conn.executemany(
            "INSERT OR REPLACE INTO stocks(code,name,industry,mktcap,tier,updated) "
            "VALUES(?,?,?,?,?,?)",
            [(c, n, i, cap, old_tier.get(c), today)
             for c, n, i, cap in items])
        conn.commit()
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                     ("stocks_updated", repr(time.time())))
        conn.commit()
    return len(items)


def refresh_from_tencent(codes=None, progress=None, batch=60, timeout=15,
                         return_prices=False):
    """腾讯行情批量刷新名称/总市值（亿元→元），保留原有行业/tier。

    codes 为空时取库内全部代码（日K≥60 根）。
    return_prices=True 时额外返回 {code: 最新价}（供增量更新算复权系数）。"""
    if codes is None:
        codes = [c for (c,) in data._cx().execute(
            "SELECT code FROM daily_bars GROUP BY code HAVING COUNT(*)>=60")]
    codes = [c for c in codes if c[:2] in ("sh", "sz", "bj")]
    old = {r[0]: (r[1], r[2], r[3]) for r in data._cx().execute(
        "SELECT code,name,industry,tier FROM stocks")}
    today = time.strftime("%Y-%m-%d")
    updated = 0
    prices = {}
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("gbk", "replace")
        except Exception:
            time.sleep(1.0)
            continue
        rows, share_rows = [], []
        for line in txt.strip().split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            body = line.split("=", 1)[1].strip().strip('"')
            f = body.split("~")
            if len(f) < 46 or len(f[2]) != 6:
                continue
            code6, name = f[2], f[1]
            prefix = "bj" if line.startswith("v_bj") else (
                "sh" if line.startswith("v_sh") else "sz")
            full = prefix + code6
            try:
                cap = float(f[45]) * 1e8
                price = float(f[3])
            except ValueError:
                cap, price = 0.0, 0.0
            _, ind, tier = old.get(full, ("", "", None))
            rows.append((full, name, ind, cap, tier, today))
            if price > 0:
                prices[full] = price
            if cap > 0 and price > 0:
                share_rows.append((full, cap / price, time.time()))
        if rows:
            with data.db_conn() as conn:
                _ensure_shares_table(conn)
                conn.executemany(
                    "INSERT OR REPLACE INTO stocks"
                    "(code,name,industry,mktcap,tier,updated) "
                    "VALUES(?,?,?,?,?,?)", rows)
                conn.executemany(
                    "INSERT OR REPLACE INTO et_shares(code,shares,ts) "
                    "VALUES(?,?,?)", share_rows)
                conn.commit()
            updated += len(rows)
        if progress and (i // batch) % 10 == 0:
            progress(f"腾讯代码表 {updated}/{len(codes)}")
        time.sleep(0.15)
    with data.db_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                     ("stocks_updated", repr(time.time())))
        conn.commit()
    if return_prices:
        return updated, prices
    return updated


def shares_of(local, meta, close):
    """总股本近似：市值 / 现价（市值来自代码表刷新日）。"""
    cap = (meta.get(local) or {}).get("mktcap") or 0.0
    if cap > 0 and close and close > 0:
        return cap / close
    return 0.0


def apply_basic(meta, local, ind, i, basic, listing_date, scan_date=None):
    """在索引 i 处判断 basic_filter（返回 (ok, reason)）。"""
    if is_index(local):
        return False, "指数"
    name = (meta.get(local) or {}).get("name", "")
    if basic.get("exclude_st", True) and "ST" in name.upper():
        return False, "ST"
    if "退" in name or "PT" in name.upper():
        return False, "退市"
    board = board_of(local)
    boards = basic.get("boards")
    if boards and board not in boards:
        return False, "板块"
    close = ind["close"][i]
    if not np.isfinite(close) or close <= 0:
        return False, "无价格"
    if basic.get("price_min") is not None and close < basic["price_min"]:
        return False, "低价"
    if basic.get("price_max") is not None and close > basic["price_max"]:
        return False, "高价"
    cap = (meta.get(local) or {}).get("mktcap") or 0.0
    if basic.get("market_cap_min") is not None:
        if cap <= 0 or cap < basic["market_cap_min"]:
            return False, "市值"
    amount = ind["amount"][i]
    if basic.get("amount_min") is not None and amount < basic["amount_min"]:
        return False, "成交额"
    if basic.get("amount_max") is not None and amount > basic["amount_max"]:
        return False, "成交额"
    if basic.get("turnover_min") is not None:
        tr = ind["turnover_rate"][i]
        if not np.isfinite(tr) or tr < basic["turnover_min"]:
            return False, "换手"
    if basic.get("turnover_max") is not None:
        tr = ind["turnover_rate"][i]
        if np.isfinite(tr) and tr > basic["turnover_max"]:
            return False, "换手"
    nd = basic.get("exclude_new_days")
    if nd and listing_date and scan_date:
        try:
            from datetime import date
            y, m, d = (int(x) for x in listing_date.split("-"))
            y2, m2, d2 = (int(x) for x in scan_date.split("-"))
            days = (date(y2, m2, d2) - date(y, m, d)).days
            if days < nd:
                return False, "次新"
        except Exception:
            pass
    return True, ""
