"""日内多时点交易的纯逻辑：当日汇总合并、每日买入限额、复盘渲染。

被 `sim/stock_run.py` 调用；不依赖行情/账户，可单独测试。
"""
import json
import os


def session_time(now):
    return now.strftime("%H:%M")


def load_daily(path):
    """读取当日汇总 JSON；不存在或损坏返回空 dict。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def daily_buys(existing):
    """当日已买入代码集合与笔数（从汇总 trades 统计）。"""
    buys = [t for t in (existing.get("trades") or [])
            if str(t.get("side", "")).upper() == "BUY"]
    return {str(t.get("code")) for t in buys}, len(buys)


def daily_sold(existing):
    """当日已卖出代码集合。"""
    return {str(t.get("code")) for t in (existing.get("trades") or [])
            if str(t.get("side", "")).upper() == "SELL"}


def skip_buy(order, existing, intraday_cfg):
    """日内买入闸门；返回跳过原因，None 表示允许执行。"""
    cfg = intraday_cfg or {}
    code = str(order.get("code") or "").lower()
    if not code:
        return "无代码"
    codes, count = daily_buys(existing)
    if cfg.get("once_per_code_per_day", True) and code in codes:
        return "今日已买入"
    if count >= int(cfg.get("max_buys_per_day", 3)):
        return "今日买入已达上限"
    return None


def merge_summary(existing, session, account_snap, candidates):
    """把本次时点 session 合并进当日汇总（orders/trades 累加）。"""
    sessions = list(existing.get("sessions") or [])
    sessions.append(session)
    orders = list(existing.get("orders") or []) + list(session.get("orders") or [])
    trades = list(existing.get("trades") or []) + list(session.get("trades") or [])
    return {
        "time": session.get("time_iso"),
        "date": session.get("date"),
        "market_view": session.get("market_view"),
        "orders": orders,
        "trades": trades,
        "account": account_snap,
        "candidates": candidates,
        "sessions": sessions,
    }


def render_review(date_str, summary):
    """由当日汇总生成 markdown 复盘（多时点分段）。"""
    acc = summary.get("account") or {}
    lines = [f"# 股票模拟盘复盘 {date_str}", ""]
    lines.append(f"- 总资产：{acc.get('total_asset')} 元")
    lines.append(f"- 现金：{acc.get('cash')} 元")
    lines.append(f"- 已实现收益：{acc.get('realized_pnl')} 元")
    lines.append(f"- 未实现收益：{acc.get('unrealized_pnl')} 元")
    lines.append(f"- 当日时点数：{len(summary.get('sessions') or [])}")
    lines.append("")
    lines.append("## 时点决策与执行")
    for s in summary.get("sessions") or []:
        lines.append("")
        lines.append(f"### {s.get('time', '?')}")
        if s.get("market_view"):
            lines.append(f"- 观点：{s['market_view']}")
        for o in s.get("orders") or []:
            pos = o.get("position")
            pos_txt = f" 仓位{pos}%" if pos is not None else ""
            lines.append(f"- 指令：{o.get('action')} {o.get('code')}"
                         f"{pos_txt} {o.get('reason', '')}")
        skipped = s.get("skipped") or []
        for k in skipped:
            lines.append(f"- 跳过：{k}")
        trades = s.get("trades") or []
        if trades:
            for t in trades:
                lines.append(
                    f"- 成交：{t.get('side')} {t.get('code')} "
                    f"{t.get('shares')}股 @{t.get('price')} "
                    f"费用{t.get('fee')} 已实现{t.get('realized', 0)}")
        else:
            lines.append("- 无成交")
    lines.append("")
    lines.append("## 持仓")
    positions = acc.get("positions") or {}
    if positions:
        for code, p in positions.items():
            lines.append(
                f"- {code}: {p.get('shares')}股 成本{p.get('cost')} "
                f"现价{p.get('price')} ({p.get('pnl_pct', 0):+.2f}%, "
                f"仓位{p.get('pct', 0)}%)")
    else:
        lines.append("- 空仓")
    lines.append("")
    return "\n".join(lines)
