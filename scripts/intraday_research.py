#!/usr/bin/env python3
"""盘中/午休资料刷新：交易日 10:30、12:30、14:10 各跑一次。

与 08:30 盘前资料分开：
- 不重算 T+1/T+5 预测（耗时），只更新“外部事件 + A股盘中主线”；
- 存 memory/daily/intraday-YYYY-MM-DD-HHMM.json，按时间分文件；
- 13:30 决策自动读取最新一份，事件风险只能加严、不能降级盘前结论；
- 外网查询走 scripts/foreign_search.py（Clash/Google/X/DDG），失败静默。

用法：
  .venv/bin/python scripts/intraday_research.py
  .venv/bin/python scripts/intraday_research.py --force --focus "特朗普 关税"
  .venv/bin/python scripts/intraday_research.py --no-foreign
"""
import argparse
import os
import sys
import time
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(BASE_DIR, ".env"))

from agent import event_risk, event_watch
from memory import premarket
from trading.calendar import TradingCalendar

EVENT_TOPICS = ("trump", "macro", "geopolitics")
MAX_ITEMS = 15


def _baseline(client, date_str, days, count):
    """A股盘中/板块补充；失败返回 []。"""
    if client is None:
        return []
    queries = [
        "A股 盘中 异动 板块 资金 最新",
        "A股 午后 行情 主线 最新",
    ]
    out, seen = [], set()
    for q in queries:
        full = event_watch._compose(q, date_str, days, "zh")
        try:
            rows = client.search(full, count=count)
        except Exception:
            rows = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            ident = r.get("url") or r.get("title")
            if not ident or ident in seen:
                continue
            seen.add(ident)
            out.append(r)
    return out


def run(force=False, days=2, count=6, focus="", use_foreign=True,
        email=False):
    today = date.today()
    if not force and not TradingCalendar().is_trading_day(today):
        print(f"[intraday] {today} 非交易日，跳过", flush=True)
        return 0
    from models.web_search import build_from_config

    client = build_from_config()
    if client is None:
        print("[intraday] DeepSeek Search 未启用（已改必应），仅跑外网检索",
              flush=True)
    date_str = today.isoformat()
    label = time.strftime("%H%M")
    if client is not None:
        fcfg = dict(getattr(client, "foreign", {}) or {})
    else:
        try:
            from models.config import load_model_config
            _s = (load_model_config().get("models") or {}).get("search") or {}
            fcfg = dict(_s.get("foreign") or {})
        except Exception:
            fcfg = {"enabled": True,
                    "engines": ["bing", "wsj", "investing"],
                    "count": 8, "days": days, "include_x": True}
    if not use_foreign:
        fcfg["enabled"] = False
    else:
        fcfg.update({"max_queries": 2, "count": 8, "include_x": True})

    print(f"[intraday] {date_str} {label} 刷新（近{days}天）", flush=True)
    ew = event_watch.collect(
        client, date_str, days=days, topics=EVENT_TOPICS,
        max_queries=3, count=max(count, 8), limit=10, focus=focus,
        foreign=fcfg)
    items, seen = [], set()
    for r in (ew.get("items") or []) + _baseline(
            client, date_str, days, count):
        ident = r.get("url") or r.get("title")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        items.append(r)
        if len(items) >= MAX_ITEMS:
            break

    # 盘前市场简报/预测沿用，盘中只更新事件与资料
    pre = premarket.load(date_str) or {}
    fallback = event_risk.heuristic(
        items, str(pre.get("market_brief") or ""),
        pre.get("predictions") or [])
    doc = {
        "date": date_str,
        "label": label,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": int(days),
        "queries": ew.get("queries") or [],
        "event_watch": {
            "date": ew.get("date"),
            "days": ew.get("days"),
            "topics": ew.get("topics"),
            "salience": ew.get("salience"),
            "signals": ew.get("signals"),
            "brief": ew.get("brief"),
            "queries": ew.get("queries"),
            "items": ew.get("items"),
            "foreign_count": ew.get("foreign_count", 0),
        },
        "event_risk": fallback,
        "items": items,
    }
    path = premarket.save_intraday(doc)
    print(f"[intraday] 事件 {len(ew.get('items') or [])} 条"
          f"（外网 {ew.get('foreign_count', 0)} 条），"
          f"资料 {len(items)} 条，风险 {fallback['level']}"
          f"/{fallback['posture']}（{fallback['risk_score']}）", flush=True)
    print(f"[intraday] 已保存 {path}", flush=True)
    if email and ew.get("salience", 0) >= 0.6:
        try:
            from scripts.pi_send_report import send
            body = (f"# 盘中资料 {date_str} {label}\n\n"
                    f"{ew.get('brief') or ''}\n\n"
                    f"事件风险：{fallback['level']}/"
                    f"{fallback['posture']}（{fallback['risk_score']}）")
            send(f"ai-quant 盘中资料 {date_str} {label}", body,
                 dedupe_key=f"intraday_{date_str}_{label}")
        except Exception as e:
            print(f"[intraday] 邮件发送异常：{e}", flush=True)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="盘中/午休资料刷新")
    p.add_argument("--force", action="store_true", help="非交易日也执行")
    p.add_argument("--days", type=int, default=2, help="搜索时间窗口")
    p.add_argument("--count", type=int, default=6, help="每条查询条数")
    p.add_argument("--focus", default="", help="额外聚焦词，如 特朗普 关税")
    p.add_argument("--no-foreign", action="store_true",
                   help="不跑 Google/X/DDG 外网补充")
    p.add_argument("--email", action="store_true",
                   help="显著度高时发邮件")
    args = p.parse_args(argv)
    return run(force=args.force, days=args.days, count=args.count,
               focus=args.focus, use_foreign=not args.no_foreign,
               email=args.email)


if __name__ == "__main__":
    raise SystemExit(main())
