#!/usr/bin/env python3
"""集合竞价快照：交易日 09:25 抓持仓/候选的五档挂单与竞价参考价。

09:25 是开盘集合竞价撮合完成、连续竞价开始前的窗口：
- 开盘缺口、竞价量、买卖盘不平衡对未来 1~2 小时有参考价值；
- 本脚本只取数据、不下单；10:00 决策通过 premarket.load_for_decision
  读取 `auction` 字段，结合实时行情再决定。

用法：
  .venv/bin/python scripts/auction_snapshot.py
  .venv/bin/python scripts/auction_snapshot.py --force --max-codes 12
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(BASE_DIR, ".env"))

from data.stock import cli_bridge as bridge
from memory import premarket
from trading.calendar import TradingCalendar

STATE_PATH = os.path.join(BASE_DIR, "sim", "state", "stock_account.json")


def _holdings():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        return list((state.get("lots") or {}).keys())
    except (OSError, ValueError):
        return []


def _candidate_codes(max_codes):
    codes = []
    pre = premarket.load() or {}
    for p in pre.get("predictions") or []:
        code = str(p.get("code") or "").lower()
        if code and code not in codes:
            codes.append(code)
        if len(codes) >= max_codes:
            return codes
    for code in _holdings():
        code = str(code or "").lower()
        if code and code not in codes:
            codes.append(code)
    if len(codes) >= 3:
        return codes[:max_codes]
    try:
        cfg = {}
        import yaml
        with open(os.path.join(BASE_DIR, "config", "stock.yaml"),
                  encoding="utf-8") as f:
            cfg = (yaml.safe_load(f) or {}).get("stock") or {}
        u = cfg.get("universe") or {}
        cands = bridge.candidates(
            top_n=max(5, max_codes), max_scan=min(60, u.get("max_scan", 120)),
            min_bars=u.get("min_bars", 400),
            min_price=u.get("min_price", 2.0),
            recent_days=u.get("recent_days", 15),
            risk_mode=cfg.get("risk_mode", "稳健"),
            include_etf=u.get("include_etf", False),
            etf_min_price=u.get("etf_min_price", 0.5),
            etf_max_scan=u.get("etf_max_scan", 30),
        )
        for c in cands:
            code = str(c.get("code") or "").lower()
            if code and code not in codes:
                codes.append(code)
            if len(codes) >= max_codes:
                break
    except Exception as e:
        print(f"[auction] 候选读取失败（继续用持仓/盘前）：{e}", flush=True)
    return codes[:max_codes]


def _save(date_str, books):
    path = os.path.join(BASE_DIR, "memory", "daily",
                        f"auction-{date_str}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {
        "date": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "label": time.strftime("%H%M"),
        "books": books,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def run(force=False, max_codes=10, levels=5):
    today = date.today()
    if not force and not TradingCalendar().is_trading_day(today):
        print(f"[auction] {today} 非交易日，跳过", flush=True)
        return 0
    date_str = today.isoformat()
    codes = _candidate_codes(max_codes)
    if not codes:
        print("[auction] 无持仓/候选，跳过", flush=True)
        return 0
    books = {}
    for code in codes:
        try:
            books[code] = bridge.order_book_view(code, levels=levels)
            b = books[code]
            print(f"[auction] {code} {b.get('name', '')} "
                  f"{b.get('phase')} 竞价={b.get('price')} "
                  f"买1={b.get('bid1')} 卖1={b.get('ask1')} "
                  f"委比={b.get('wei_bi_pct')}", flush=True)
        except Exception as e:
            print(f"[auction] {code} 盘口失败: {e}", flush=True)
    if not books:
        return 1
    path = _save(date_str, books)
    print(f"[auction] 已保存 {path}（{len(books)} 只）", flush=True)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="集合竞价/五档挂单快照")
    p.add_argument("--force", action="store_true", help="非交易日也执行")
    p.add_argument("--max-codes", type=int, default=10)
    p.add_argument("--levels", type=int, default=5)
    args = p.parse_args(argv)
    return run(force=args.force, max_codes=args.max_codes,
               levels=args.levels)


if __name__ == "__main__":
    raise SystemExit(main())
