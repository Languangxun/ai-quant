#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 AI Quant 状态页 /var/www/status/index.html（纯标准库、无 JS、无外链）。

内容：
- 股票模拟盘：账户概览 / 资产曲线 / 最新决策 / 候选 / 持仓 / 近期成交
- 回测数据：净值对比、回撤、年化对比、年度收益、月度热力图、全量指标表
- 旧版基金模拟盘（etf-c）概览与净值曲线
- 机器状态（/tmp/health.json）

SVG 全部服务端内联生成，页面适配窄屏。
"""
import argparse
import datetime as dt
import glob
import html
import json
import math
import os
import re

RED = "#ff5a5a"
GREEN = "#4cd964"
BLUE = "#4da3ff"
ORANGE = "#ffa94d"
PURPLE = "#b197fc"
CYAN = "#38d9a9"
GRAY = "#8a8f98"
GRID = "#26292f"
DIM = "#8a8f98"

RUN_ORDER = [
    ("top300", "市值前300", True),
    ("top1000", "市值前1000", True),
    ("main", "沪深主板", True),
    ("full", "全市场", True),
    ("since2022", "全市场·2022起", True),
    ("mode_baoshou", "保守档", True),
    ("mode_jiji", "激进档", True),
    ("pi_smoke", "抽样冒烟", False),
    ("smoke", "小样本", False),
]

RUN_COLORS = {
    "top300": RED,
    "top1000": ORANGE,
    "main": BLUE,
    "full": PURPLE,
    "since2022": CYAN,
    "mode_baoshou": "#f783ac",
    "mode_jiji": "#e599f7",
}


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def pdate(s):
    return dt.date.fromisoformat(str(s)[:10])


def downsample(pts, max_n=520):
    if len(pts) <= max_n:
        return pts
    step = math.ceil(len(pts) / max_n)
    out = pts[::step]
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def cum_pct(equity):
    pts = [(pdate(d), float(v)) for d, v in equity if v is not None]
    if not pts:
        return []
    base = pts[0][1] or 1.0
    return [(d, (v / base - 1) * 100) for d, v in pts]


def drawdown_pct(equity):
    pts = [(pdate(d), float(v)) for d, v in equity if v is not None]
    peak = None
    out = []
    for d, v in pts:
        peak = v if peak is None or v > peak else peak
        out.append((d, (v / peak - 1) * 100))
    return out


def yearly_pct(equity):
    pts = [(pdate(d), float(v)) for d, v in equity if v is not None]
    out = {}
    prev = None
    cur_year = None
    year_start = None
    for d, v in pts:
        if d.year != cur_year:
            if cur_year is not None and year_start:
                out[cur_year] = (prev / year_start - 1) * 100
            cur_year = d.year
            year_start = prev if prev else v
        prev = v
    if cur_year is not None and year_start:
        out[cur_year] = (prev / year_start - 1) * 100
    return out


def monthly_pct(equity):
    pts = [(pdate(d), float(v)) for d, v in equity if v is not None]
    out = {}
    prev = None
    key = None
    month_start = None
    for d, v in pts:
        k = (d.year, d.month)
        if k != key:
            if key is not None and month_start:
                out[key] = (prev / month_start - 1) * 100
            key = k
            month_start = prev if prev else v
        prev = v
    if key is not None and month_start:
        out[key] = (prev / month_start - 1) * 100
    return out


def _nice_step(span, target=4):
    raw = span / max(target, 1)
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _xlabel(d, span_days):
    if span_days > 1400:
        return str(d.year)
    if span_days > 320:
        return f"{d.year % 100}/{d.month}"
    return f"{d.month}/{d.day}"


def line_chart(series, width=680, height=250, y_fmt=None, y_zero=False):
    fmt = y_fmt or (lambda v: f"{v:.0f}")
    series = [
        dict(s, pts=downsample([(pdate(d), float(v)) for d, v in s.get("pts", [])
                                if v is not None]))
        for s in series
    ]
    series = [s for s in series if s["pts"]]
    all_pts = [p for s in series for p in s["pts"]]
    if not all_pts:
        return '<div class="empty">暂无数据</div>'
    xmin, xmax = min(d for d, _ in all_pts), max(d for d, _ in all_pts)
    if xmin == xmax:
        xmax = xmin + dt.timedelta(days=1)
    ymin, ymax = min(v for _, v in all_pts), max(v for _, v in all_pts)
    if y_zero:
        ymin, ymax = min(ymin, 0), max(ymax, 0)
    if ymin == ymax:
        ymin, ymax = ymin - 1, ymax + 1
    pad = (ymax - ymin) * 0.09
    ymin, ymax = ymin - pad, ymax + pad
    left, right, top, bottom = 48, 10, 12, 24
    pw, ph = width - left - right, height - top - bottom
    span = (xmax - xmin).days or 1

    def X(d):
        return left + (d - xmin).days / span * pw

    def Y(v):
        return top + (ymax - v) / (ymax - ymin) * ph

    e = []
    step = _nice_step(ymax - ymin, 4)
    t = math.floor(ymin / step) * step
    while t <= ymax + 1e-9:
        if t >= ymin - 1e-9:
            y = Y(t)
            e.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" '
                     f'y2="{y:.1f}" stroke="{GRID}"/>')
            e.append(f'<text x="{left - 5}" y="{y + 3.5:.1f}" text-anchor="end" '
                     f'font-size="10" fill="{DIM}">{html.escape(fmt(t))}</text>')
        t += step
    if ymin < 0 < ymax:
        y = Y(0)
        e.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" '
                 f'y2="{y:.1f}" stroke="#4a4d55"/>')
    for i in range(5):
        d = xmin + dt.timedelta(days=span * i / 4)
        e.append(f'<text x="{X(d):.1f}" y="{height - 6}" text-anchor="middle" '
                 f'font-size="10" fill="{DIM}">{_xlabel(d, span)}</text>')
    for s in series:
        pts = [(X(d), Y(v)) for d, v in s["pts"]]
        if s.get("area") and len(pts) > 1:
            base = Y(max(ymin, min(ymax, 0))) if s.get("area_zero") else Y(ymin)
            dpath = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            dpath += (f" L{pts[-1][0]:.1f},{base:.1f} "
                      f"L{pts[0][0]:.1f},{base:.1f} Z")
            e.append(f'<path d="{dpath}" fill="{s["color"]}" opacity="0.14"/>')
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        dash = f' stroke-dasharray="{s["dash"]}"' if s.get("dash") else ""
        e.append(f'<polyline points="{line}" fill="none" stroke="{s["color"]}" '
                 f'stroke-width="{s.get("w", 1.6)}"{dash}/>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" '
            f'height="auto" preserveAspectRatio="xMidYMid meet" '
            f'xmlns="http://www.w3.org/2000/svg">' + "".join(e) + "</svg>")


def hbar_chart(items, width=680, row_h=28, value_fmt="{:+.1f}%"):
    if not items:
        return '<div class="empty">暂无数据</div>'
    label_w, right = 104, 58
    height = len(items) * row_h + 8
    vmin = min(0.0, min(v for _, v in items))
    vmax = max(0.0, max(v for _, v in items))
    span = (vmax - vmin) or 1.0
    pw = width - label_w - right

    def X(v):
        return label_w + (v - vmin) / span * pw

    e = []
    x0 = X(0)
    e.append(f'<line x1="{x0:.1f}" y1="4" x2="{x0:.1f}" y2="{height - 4}" '
             f'stroke="#4a4d55"/>')
    for i, (label, v) in enumerate(items):
        y = 4 + i * row_h + row_h * 0.18
        bh = row_h * 0.62
        x1, x2 = sorted((x0, X(v)))
        color = RED if v >= 0 else GREEN
        e.append(f'<rect x="{x1:.1f}" y="{y:.1f}" width="{max(x2 - x1, 1):.1f}" '
                 f'height="{bh:.1f}" fill="{color}" opacity="0.85" rx="2"/>')
        e.append(f'<text x="{label_w - 6}" y="{y + bh * 0.8:.1f}" '
                 f'text-anchor="end" font-size="11" fill="#cfd3da">'
                 f'{html.escape(label)}</text>')
        tx = x2 + 5 if v >= 0 else x1 - 5
        anchor = "start" if v >= 0 else "end"
        e.append(f'<text x="{tx:.1f}" y="{y + bh * 0.8:.1f}" text-anchor="{anchor}" '
                 f'font-size="10" fill="{color}">{html.escape(value_fmt.format(v))}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="auto" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'xmlns="http://www.w3.org/2000/svg">' + "".join(e) + "</svg>")


def grouped_bar_chart(cats, groups, width=680, height=230,
                      value_fmt="{:+.1f}%"):
    if not cats:
        return '<div class="empty">暂无数据</div>'
    vals = [v for g in groups for v in g["values"] if v is not None]
    if not vals:
        return '<div class="empty">暂无数据</div>'
    left, right, top, bottom = 46, 8, 10, 24
    pw, ph = width - left - right, height - top - bottom
    ymin, ymax = min(0, min(vals)), max(0, max(vals))
    if ymin == ymax:
        ymin, ymax = ymin - 1, ymax + 1
    pad = (ymax - ymin) * 0.1
    ymin, ymax = ymin - pad, ymax + pad

    def Y(v):
        return top + (ymax - v) / (ymax - ymin) * ph

    e = []
    step = _nice_step(ymax - ymin, 4)
    t = math.floor(ymin / step) * step
    while t <= ymax + 1e-9:
        if t >= ymin - 1e-9:
            y = Y(t)
            e.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" '
                     f'y2="{y:.1f}" stroke="{GRID}"/>')
            e.append(f'<text x="{left - 5}" y="{y + 3.5:.1f}" text-anchor="end" '
                     f'font-size="10" fill="{DIM}">{fmt_pct_tick(t)}</text>')
        t += step
    slot = pw / len(cats)
    bw = slot * 0.72 / len(groups)
    y0 = Y(0)
    for i, cat in enumerate(cats):
        cx = left + slot * (i + 0.5)
        for j, g in enumerate(groups):
            v = g["values"][i]
            if v is None:
                continue
            x = cx - slot * 0.36 + j * bw
            y = Y(v)
            e.append(f'<rect x="{x:.1f}" y="{min(y, y0):.1f}" width="{bw * 0.9:.1f}" '
                     f'height="{max(abs(y0 - y), 1):.1f}" fill="{g["color"]}" '
                     f'opacity="0.88" rx="1.5"/>')
            ty = y - 3 if v >= 0 else y + 10
            e.append(f'<text x="{x + bw * 0.45:.1f}" y="{ty:.1f}" text-anchor="middle" '
                     f'font-size="9" fill="{g["color"]}">{value_fmt.format(v)}</text>')
        e.append(f'<text x="{cx:.1f}" y="{height - 6}" text-anchor="middle" '
                 f'font-size="10" fill="{DIM}">{html.escape(str(cat))}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="auto" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'xmlns="http://www.w3.org/2000/svg">' + "".join(e) + "</svg>")


def vbar_chart(cats, values, colors, width=680, height=190, unit="笔"):
    if not cats:
        return '<div class="empty">暂无数据</div>'
    left, right, top, bottom = 30, 8, 16, 28
    pw, ph = width - left - right, height - top - bottom
    ymax = max(values) or 1
    e = []
    for i in range(4):
        y = top + ph * i / 3
        v = ymax * (3 - i) / 3
        e.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" '
                 f'y2="{y:.1f}" stroke="{GRID}"/>')
        e.append(f'<text x="{left - 4}" y="{y + 3.5:.1f}" text-anchor="end" '
                 f'font-size="10" fill="{DIM}">{v:.0f}</text>')
    slot = pw / len(cats)
    for i, (cat, v, color) in enumerate(zip(cats, values, colors)):
        bh = ph * (v / ymax) if ymax else 0
        x = left + slot * i + slot * 0.18
        bw = slot * 0.64
        e.append(f'<rect x="{x:.1f}" y="{top + ph - bh:.1f}" width="{bw:.1f}" '
                 f'height="{max(bh, 1):.1f}" fill="{color}" opacity="0.88" rx="2"/>')
        e.append(f'<text x="{x + bw / 2:.1f}" y="{top + ph - bh - 3:.1f}" '
                 f'text-anchor="middle" font-size="9" fill="#cfd3da">{v}</text>')
        e.append(f'<text x="{x + bw / 2:.1f}" y="{height - 8}" text-anchor="middle" '
                 f'font-size="9" fill="{DIM}">{html.escape(cat)}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="auto" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'xmlns="http://www.w3.org/2000/svg">' + "".join(e) + "</svg>")


def trade_hist(trades):
    edges = [(-1e9, -10, "<-10"), (-10, -5, "-10~-5"), (-5, -3, "-5~-3"),
             (-3, -1, "-3~-1"), (-1, 0, "-1~0"), (0, 1, "0~1"),
             (1, 3, "1~3"), (3, 5, "3~5"), (5, 10, "5~10"), (10, 1e9, ">10")]
    counts = [0] * len(edges)
    for t in trades:
        if str(t.get("side", "")).upper() != "SELL":
            continue
        amount = float(t.get("amount") or 0)
        realized = float(t.get("realized") or 0)
        cost = amount - realized
        if cost <= 0:
            continue
        ret = realized / cost * 100
        for i, (lo, hi, _) in enumerate(edges):
            if lo <= ret < hi:
                counts[i] += 1
                break
    labels = [e[2] for e in edges]
    colors = [GREEN if e[1] <= 0 else RED for e in edges]
    return labels, counts, colors


def fmt_pct_tick(v):
    return f"{v:+.0f}%" if v else "0%"


def legend(series):
    items = "".join(
        f'<span class="lg"><i style="background:{s["color"]}"></i>'
        f'{html.escape(s["name"])}</span>' for s in series)
    return f'<div class="legend">{items}</div>'


def heat_color(v):
    if v is None:
        return "#1a1c20"
    a = min(abs(v) / 8.0, 1.0) * 0.72 + 0.06
    return f"rgba(255,90,90,{a:.2f})" if v >= 0 else f"rgba(76,217,100,{a:.2f})"


def heatmap_table(monthly):
    years = sorted({y for y, _ in monthly})
    if not years:
        return '<div class="empty">暂无数据</div>'
    rows = ["<table class='heat'><tr><th></th>"
            + "".join(f"<th>{m}</th>" for m in range(1, 13))
            + "<th>全年</th></tr>"]
    for y in years:
        cells = [f"<th>{y}</th>"]
        prod = 1.0
        for m in range(1, 13):
            v = monthly.get((y, m))
            if v is None:
                cells.append("<td style='color:#555'>·</td>")
                continue
            prod *= 1 + v / 100
            tone = "#e8e8e8" if abs(v) < 4 else "#fff"
            cells.append(f"<td style='background:{heat_color(v)};color:{tone}'>"
                         f"{v:+.1f}</td>")
        total = (prod - 1) * 100
        cells.append(f"<td style='background:{heat_color(total)};color:#fff;"
                     f"font-weight:bold'>{total:+.1f}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    rows.append("</table>")
    return "".join(rows)


def badge(action):
    a = str(action or "").upper()
    color = {"BUY": RED, "SELL": GREEN}.get(a, GRAY)
    return (f'<span class="badge" style="border-color:{color};color:{color}">'
            f'{html.escape(a or "?")}</span>')


def action_label(action):
    a = str(action or "").upper()
    return {"BUY": "买入/加仓", "SELL": "卖出/减仓", "HOLD": "持有/观望"}.get(a, a or "?")


def load_holidays(base):
    data = load_json(os.path.join(base, "config", "holidays.json"), {})
    if isinstance(data, dict):
        closed = data.get("closed") or {}
        if isinstance(closed, dict):
            return {str(k): str(v) for k, v in closed.items()}
        return {str(k): "休市" for k in closed}
    return {}


def market_info(base):
    holidays = load_holidays(base)
    today = dt.date.today()
    week = "一二三四五六日"

    def is_open(d):
        return d.weekday() < 5 and d.isoformat() not in holidays

    opened = is_open(today)
    nd = today + dt.timedelta(days=1)
    while not is_open(nd):
        nd += dt.timedelta(days=1)
    if opened:
        text = (f"今日 {today:%Y-%m-%d} 交易日 · 模拟盘 14:50 运行，"
                f"数据收盘后更新")
        tag = "交易日"
    else:
        name = holidays.get(today.isoformat())
        if not name:
            name = "周末" if today.weekday() >= 5 else "休市"
        text = (f"今日 {today:%Y-%m-%d} {name}休市 · 下一交易日 "
                f"{nd:%Y-%m-%d}（周{week[nd.weekday()]}）")
        tag = "休市"
    return {"open": opened, "text": text, "tag": tag, "next": nd,
            "holidays": holidays}


def load_stock_settings(base):
    """从 config/stock.yaml 提取展示用标量（stdlib-only，不依赖 yaml）。"""
    try:
        with open(os.path.join(base, "config", "stock.yaml"),
                  encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    out = {}
    for key in ("capital", "max_positions", "max_position_pct",
                "min_order_amount", "min_order_pct", "risk_mode"):
        m = re.search(rf"^\s*{key}:\s*([^#\n]+)", text, re.M)
        if m:
            out[key] = m.group(1).strip().strip("\"'")
    m = re.search(r"max_scan:\s*([^#\n]+)", text)
    if m:
        out["max_scan"] = m.group(1).strip()
    m = re.search(r"times:\s*\[([^\]]*)\]", text)
    if m:
        out["times"] = [t.strip().strip("\"'") for t in m.group(1).split(",")
                        if t.strip()]
    return out


def stock_sim_strategy_text(base, state, latest):
    st = load_stock_settings(base)
    times = st.get("times") or []

    def num(key, default=None):
        try:
            return float(st.get(key))
        except (TypeError, ValueError):
            return default

    cap = ((state or {}).get("initial_capital")
           or num("capital") or 20000.0)
    parts = [f"{st.get('risk_mode', '稳健')}档",
             f"扫描市值前{st.get('max_scan', '?')}",
             f"最多{st.get('max_positions', '?')}只",
             f"单票≤{st.get('max_position_pct', '?')}%",
             f"本金 {cap:,.0f} 元",
             f"最小下单 {st.get('min_order_amount', '?')}元"
             f"（小资金按{st.get('min_order_pct', '?')}%自适应）"]
    if times:
        parts.append(f"日内{len(times)}时点 " + "/".join(times))
    sessions = (latest or {}).get("sessions") or []
    line2 = "策略参数：" + " · ".join(parts)
    if sessions:
        line2 += (f"<br>今日已运行 {len(sessions)} 个时点"
                  f"（最近 {sessions[-1].get('time', '?')}）")
    return line2


def load_stock_sim(base):
    dailies = []
    for p in sorted(glob.glob(os.path.join(base, "memory", "daily", "stock-*.json"))):
        d = load_json(p)
        if isinstance(d, dict) and d.get("date"):
            dailies.append(d)
    state = load_json(os.path.join(base, "sim", "state", "stock_account.json"))
    return dailies, state


def stock_section(base, mk=None):
    dailies, state = load_stock_sim(base)
    latest = dailies[-1] if dailies else None
    acc = (latest or {}).get("account") or {}
    capital = (state or {}).get("initial_capital") or 20000.0
    total = acc.get("total_asset")
    cash = acc.get("cash", (state or {}).get("cash"))
    realized = acc.get("realized_pnl", (state or {}).get("realized_pnl", 0.0))
    unrealized = acc.get("unrealized_pnl")
    positions = acc.get("positions") or {}
    fees = acc.get("fees", (state or {}).get("fees", 0.0))
    trade_count = acc.get("trade_count", (state or {}).get("trade_count", 0))
    date = (latest or {}).get("date", "—")
    stale = ""
    if mk and not mk["open"] and str(date) == dt.date.today().isoformat():
        stale = ' <span class="warn">⚠ 该快照生成于休市日，非真实成交行情</span>'

    names = {}
    for d in dailies:
        for c in d.get("candidates") or []:
            if c.get("code"):
                names[c["code"]] = c.get("name", "")

    if total is not None:
        gain = total - capital
        pct = gain / capital * 100 if capital else 0.0
        gain_color = RED if gain >= 0 else GREEN
        head = (f'<div class="big">{total:,.2f} 元</div>'
                f'<div class="lbl">总资产（初始 {capital:,.0f} · '
                f'<span style="color:{gain_color}">{gain:+,.0f} 元 / '
                f'{pct:+.2f}%</span>）</div>')
    else:
        head = ('<div class="big">—</div>'
                '<div class="lbl">暂无账户快照（等待首次模拟盘成交）</div>')

    def _mv(v):
        if v is None:
            return "—"
        color = RED if v > 0 else (GREEN if v < 0 else "#ccc")
        return f'<span style="color:{color}">{v:,.2f}</span>'

    mv = (total - cash) if (total is not None and cash is not None) else None
    rows = [
        ("现金", f"{cash:,.2f}" if cash is not None else "—"),
        ("持仓市值", f"{mv:,.2f}" if mv is not None else "—"),
        ("已实现收益", _mv(realized) + " 元"),
        ("未实现收益", _mv(unrealized) + " 元"),
        ("累计费用", f"{fees:,.2f} 元"),
        ("成交笔数 / 持仓数", f"{trade_count} / {len(positions)}"),
    ]
    acc_table = "".join(f"<tr><td>{k}</td><td class='r'>{v}</td></tr>"
                        for k, v in rows)

    eq_pts = [(d["date"], (d.get("account") or {}).get("total_asset"))
              for d in dailies if (d.get("account") or {}).get("total_asset")]
    if len(eq_pts) >= 2:
        nav = line_chart(
            [{"name": "股票模拟盘", "color": RED, "pts": eq_pts,
              "area": True, "w": 1.8}],
            y_fmt=lambda v: f"{v / 10000:.2f}万")
        chart = (nav + f'<div class="legend"><span class="lg">初始 '
                 f'{capital:,.0f} 元</span></div>')
    else:
        chart = ('<div class="empty">资产曲线将在累计 2 个交易日快照后展示'
                 '（当前由每日 14:50 模拟盘写入）</div>')

    orders = (latest or {}).get("orders") or []
    order_rows = []
    for o in orders:
        pos = o.get("position")
        pos_txt = f"仓位 {pos}%" if pos is not None else ""
        conf = o.get("confidence")
        conf_txt = f"置信 {conf}" if conf is not None else ""
        code = o.get("code", "")
        nm = names.get(code, "")
        order_rows.append(
            f'<div class="order">{badge(o.get("action"))}'
            f'<b>{html.escape(code)}</b> {html.escape(nm)} '
            f'<span class="mut">{pos_txt} {conf_txt}</span>'
            f'<div class="why">{html.escape(str(o.get("reason", "")))}</div></div>')
    if not order_rows:
        order_rows.append('<div class="empty">无指令</div>')
    decision_card = (
        f'<div class="card"><div class="lbl">最新决策（{date}）</div>'
        f'<div class="view">{html.escape(str((latest or {}).get("market_view") or "无"))}'
        f'</div>' + "".join(order_rows) + "</div>")

    cands = sorted((latest or {}).get("candidates") or [],
                   key=lambda c: c.get("score", 0), reverse=True)[:10]
    if cands:
        crows = "".join(
            f"<tr><td>{html.escape(c.get('code', ''))}</td>"
            f"<td>{html.escape(c.get('name', ''))}</td>"
            f"<td class='r'>{c.get('score', '')}</td>"
            f"<td class='r' style='color:"
            f"{RED if (c.get('chg') or 0) >= 0 else GREEN}'>"
            f"{(c.get('chg') or 0):+.2f}%</td>"
            f"<td class='r'>{c.get('price', '')}</td></tr>" for c in cands)
        cand_card = ('<div class="card"><div class="lbl">今日候选 Top10</div>'
                     '<table><tr><th>代码</th><th>名称</th><th class="r">评分</th>'
                     '<th class="r">涨跌</th><th class="r">现价</th></tr>'
                     + crows + "</table></div>")
    else:
        cand_card = (f'<div class="card"><div class="lbl">今日候选</div>'
                     f'<div class="empty">暂无（{date}）</div></div>')

    if positions:
        prows = "".join(
            f"<tr><td>{html.escape(code)}<div class='mut'>"
            f"{html.escape(names.get(code, ''))}</div></td>"
            f"<td class='r'>{p.get('shares', 0):,.0f}</td>"
            f"<td class='r'>{p.get('cost', 0):.3f}</td>"
            f"<td class='r'>{p.get('price', 0):.3f}</td>"
            f"<td class='r' style='color:"
            f"{RED if (p.get('pnl_pct') or 0) >= 0 else GREEN}'>"
            f"{(p.get('pnl_pct') or 0):+.2f}%</td>"
            f"<td class='r'>{p.get('pct', 0):.1f}%</td></tr>"
            for code, p in positions.items())
        pos_card = ('<div class="card"><div class="lbl">当前持仓</div>'
                    '<table><tr><th>代码</th><th class="r">股数</th>'
                    '<th class="r">成本</th><th class="r">现价</th>'
                    '<th class="r">盈亏</th><th class="r">仓位</th></tr>'
                    + prows + "</table></div>")
    else:
        pos_card = ('<div class="card"><div class="lbl">当前持仓</div>'
                    '<div class="empty">空仓</div></div>')

    trades = []
    for d in dailies:
        for t in d.get("trades") or []:
            trades.append((d.get("date", ""), t))
    trade_rows = []
    for day, t in trades[-12:][::-1]:
        side = str(t.get("side", "")).upper()
        color = RED if side == "BUY" else GREEN
        trade_rows.append(
            f"<tr><td>{day}</td><td style='color:{color};font-weight:bold'>"
            f"{html.escape(side)}</td><td>{html.escape(t.get('code', ''))}</td>"
            f"<td class='r'>{t.get('shares', 0):,.0f}股</td>"
            f"<td class='r'>@{t.get('price', 0):.3f}</td>"
            f"<td class='r'>费用 {t.get('fee', 0):.2f}</td>"
            f"<td class='r' style='color:"
            f"{RED if (t.get('realized') or 0) >= 0 else GREEN}'>"
            f"{(t.get('realized') or 0):+.2f}</td></tr>")
    if trade_rows:
        trade_card = ('<div class="card"><div class="lbl">近期成交</div>'
                      '<div class="scroll"><table><tr><th>日期</th><th>方向</th>'
                      '<th>代码</th><th class="r">股数</th><th class="r">价格</th>'
                      '<th class="r">费用</th><th class="r">已实现</th></tr>'
                      + "".join(trade_rows) + "</table></div></div>")
    else:
        trade_card = (f'<div class="card"><div class="lbl">近期成交</div>'
                      f'<div class="empty">暂无成交记录</div></div>')

    return f"""
<h2 id="live">股票模拟盘</h2>
<div class="sub2">CLI 选股 + deepseek-v4.1-flash 组合决策 + A股规则（100股整手/T+1/涨跌停）· 数据 {date}{stale}<br>
{stock_sim_strategy_text(base, state, latest)}</div>
<div class="card">{head}</div>
<div class="card"><table>{acc_table}</table></div>
<div class="card"><div class="lbl">资产曲线（每交易日快照）</div>{chart}</div>
{decision_card}
{cand_card}
{pos_card}
{trade_card}
"""


def load_backtests(base):
    runs = []
    for key, label, visible in RUN_ORDER:
        p = os.path.join(base, "backtest", "results", f"stock_backtest_{key}.json")
        d = load_json(p)
        if not isinstance(d, dict) or "stats" not in d:
            continue
        runs.append({
            "key": key, "label": label, "visible": visible,
            "stats": d["stats"],
            "equity": d.get("equity") or [],
            "index_curve": d.get("index_curve") or [],
            "bench_curve": d.get("bench_curve") or [],
            "trades": d.get("trades") or [],
            "generated": d.get("generated", ""),
        })
    return runs


def backtest_section(base):
    runs = load_backtests(base)
    if not runs:
        return ('<h2 id="bt">回测数据</h2>'
                '<div class="card"><div class="empty">暂无回测结果'
                '（backtest/results/stock_backtest_*.json）</div></div>')
    visible = [r for r in runs if r["visible"]]
    by_key = {r["key"]: r for r in runs}

    series = []
    for k, color in [("top1000", ORANGE), ("main", BLUE), ("full", PURPLE)]:
        r = by_key.get(k)
        if r and r["equity"]:
            series.append({"name": r["label"], "color": color,
                           "pts": cum_pct(r["equity"]), "w": 1.3})
    idx_src = by_key.get("top300") or (visible[0] if visible else None)
    if idx_src and idx_src["index_curve"]:
        series.append({"name": "上证指数", "color": GRAY, "dash": "5 4",
                       "pts": cum_pct(idx_src["index_curve"]), "w": 1.3})
    r300 = by_key.get("top300")
    if r300 and r300["equity"]:
        series.append({"name": "市值前300 ★实盘同口径", "color": RED,
                       "pts": cum_pct(r300["equity"]), "w": 2.4})
    equity_svg = line_chart(series, y_fmt=fmt_pct_tick) if series else \
        '<div class="empty">暂无数据</div>'
    equity_legend = legend(sorted(series, key=lambda s: "★" not in s["name"]))
    if r300:
        s = r300["stats"]
        stat_chips = f"""<div class="stats">
<div class="stat"><div class="v" style="color:{RED}">{s['total_return'] * 100:+.1f}%</div><div class="k">总收益</div></div>
<div class="stat"><div class="v" style="color:{RED}">{s['annual'] * 100:+.1f}%</div><div class="k">年化</div></div>
<div class="stat"><div class="v" style="color:{GREEN}">{s['max_drawdown'] * 100:.1f}%</div><div class="k">最大回撤</div></div>
<div class="stat"><div class="v">{s['sharpe']:.2f}</div><div class="k">Sharpe</div></div>
<div class="stat"><div class="v" style="color:{RED}">{s.get('index_excess', 0) * 100:+.0f}pp</div><div class="k">超额 vs 上证</div></div>
</div>
<div class="mut">★ <b>实盘同口径</b>：稳健档 · 每日扫描市值前120 · 收盘成交；
回测样本为市值前300（现有档位中最接近实盘扫描范围）</div>"""
    else:
        stat_chips = ""

    dd_series = []
    if r300 and r300["equity"]:
        dd_series.append({"name": "市值前300 ★", "color": RED,
                          "pts": drawdown_pct(r300["equity"]),
                          "area": True, "area_zero": True, "w": 1.8})
        if r300["index_curve"]:
            dd_series.append({"name": "上证指数", "color": GRAY, "dash": "5 4",
                              "pts": drawdown_pct(r300["index_curve"]), "w": 1.3})
    rfull = by_key.get("full")
    if rfull and rfull["equity"]:
        dd_series.append({"name": "全市场", "color": PURPLE,
                          "pts": drawdown_pct(rfull["equity"]), "w": 1.4})
    dd_svg = line_chart(dd_series, y_fmt=fmt_pct_tick) if dd_series else \
        '<div class="empty">暂无数据</div>'

    bars = sorted([(r["label"], r["stats"]["annual"] * 100) for r in visible],
                  key=lambda x: x[1], reverse=True)
    bar_svg = hbar_chart(bars)

    yearly_groups = []
    if r300:
        years = sorted(yearly_pct(r300["equity"]))
        strat = [yearly_pct(r300["equity"]).get(y) for y in years]
        idx_y = yearly_pct(r300["index_curve"]) if r300["index_curve"] else {}
        idxv = [idx_y.get(y) for y in years]
        if years:
            yearly_groups.append({"name": "市值前300", "color": RED,
                                  "values": strat})
            yearly_groups.append({"name": "上证指数", "color": GRAY,
                                  "values": idxv})
            yearly_svg = grouped_bar_chart(years, yearly_groups)
            yearly_legend = legend(yearly_groups)
        else:
            yearly_svg, yearly_legend = '<div class="empty">暂无数据</div>', ""
    else:
        yearly_svg, yearly_legend = '<div class="empty">暂无数据</div>', ""

    monthly = monthly_pct(r300["equity"]) if r300 else {}
    heat = heatmap_table(monthly)

    if r300 and r300["trades"]:
        labels, counts, colors = trade_hist(r300["trades"])
        hist_svg = vbar_chart(labels, counts, colors)
        hist_card = ('<div class="card"><div class="lbl">单笔收益分布 · 市值前300'
                     f'（平仓 {r300["stats"].get("closed", 0)} 笔）</div>{hist_svg}'
                     '<div class="mut">按卖出笔收益率分桶（不含费用笔均）'
                     '；红=盈利，绿=亏损</div></div>')
    else:
        hist_card = ""

    trows = []
    for r in visible:
        s = r["stats"]
        idx_exc = s.get("index_excess")
        bench = s.get("bench_total_return")
        exc_txt = f"{idx_exc * 100:+.1f}pp" if idx_exc is not None else "—"
        exc_color = RED if (idx_exc or 0) >= 0 else GREEN
        rng = s.get("range") or ["?", "?"]
        star = r["key"] == "top300"
        label = ("★ " if star else "") + r["label"]
        trows.append(
            f"<tr{' class=mine' if star else ''}>"
            f"<td><b>{html.escape(label)}</b><div class='mut'>{rng[0]}~"
            f"{rng[1]}</div></td>"
            f"<td class='r' style='color:"
            f"{RED if s['total_return'] >= 0 else GREEN}'>{s['total_return'] * 100:+.1f}%</td>"
            f"<td class='r'>{s['annual'] * 100:+.1f}%</td>"
            f"<td class='r'>{s['max_drawdown'] * 100:.1f}%</td>"
            f"<td class='r'>{s['sharpe']:.2f}</td>"
            f"<td class='r'>{s['win_rate'] * 100:.0f}%</td>"
            f"<td class='r'>{s['trades']}</td>"
            f"<td class='r'>{bench * 100:+.0f}%</td>"
            f"<td class='r' style='color:{exc_color}'>{exc_txt}</td></tr>")
    table = ("<div class='scroll'><table><tr><th>回测</th>"
             "<th class='r'>总收益</th><th class='r'>年化</th>"
             "<th class='r'>最大回撤</th><th class='r'>Sharpe</th>"
             "<th class='r'>胜率</th><th class='r'>交易</th>"
             "<th class='r'>等权基准</th><th class='r'>超额(上证)</th></tr>"
             + "".join(trows) + "</table></div>")

    hidden = [r for r in runs if not r["visible"]]
    extra = ""
    if hidden:
        lines = ", ".join(
            f"{html.escape(r['label'])} 年化 {r['stats']['annual'] * 100:+.1f}%"
            for r in hidden)
        extra = f'<details><summary>冒烟/抽样结果</summary><div class="mut">{lines}</div></details>'

    gen = ""
    gs = [r["generated"][:16].replace("T", " ") for r in visible if r.get("generated")]
    if gs:
        gen = f"最近一次回测生成于 {max(gs)}"

    return f"""
<h2 id="bt">回测数据</h2>
<div class="sub2">信号 T 日收盘生成、T+1 成交；100股整手、T+1可卖、涨跌停不成交；
佣金万2.5(最低5元)+印花税0.05%+过户费0.001%；ATR(14) 跟踪止损 · {gen}</div>

<div class="card">{stat_chips}</div>

<div class="card"><div class="lbl">净值走势 · 累计收益（各回测自起点归一）</div>
{equity_svg}{equity_legend}
<div class="mut">其余曲线为对照口径；★ 为与你实盘参数最接近的回测</div></div>

<div class="card"><div class="lbl">回撤曲线（相对历史高点）</div>
{dd_svg}</div>

<div class="card"><div class="lbl">年化收益对比</div>{bar_svg}
<div class="mut">红=正收益，绿=负收益；口径见文末说明</div></div>

<div class="card"><div class="lbl">年度收益（市值前300 vs 上证指数）</div>
{yearly_svg}{yearly_legend}</div>

<div class="card"><div class="lbl">月度收益热力图 · 市值前300（%）</div>
<div class="scroll">{heat}</div></div>

{hist_card}

<div class="card"><div class="lbl">回测指标总表</div>{table}</div>
{extra}
<div class="card"><div class="lbl">口径说明</div>
<div class="mut">
① 选股：CLI 多维评分（T 日收盘），市值/风险档按各回测配置；<br>
② 成交：T+1 收盘价成交，100 股整手，T+1 可卖，涨跌停不成交；<br>
③ 费用：佣金万2.5（最低 5 元）+ 印花税 0.05%（卖出）+ 过户费 0.001%；<br>
④ 退出：ATR(14) 跟踪止损（浮盈超阈值后按最高价回撤比例移动）；<br>
⑤ 基准：等权全市场与上证指数；历史统计研究，不构成投资建议。
</div></div>
"""


def fund_section(base):
    d = load_json(os.path.join(base, "backtest", "results", "latest.json"))
    records = (d or {}).get("records") or []
    live = None
    live_files = sorted(glob.glob(os.path.join(
        base, "memory", "daily", "20[0-9][0-9]-[0-9][0-9]-[0-9][0-9].json")))
    if live_files:
        live = load_json(live_files[-1])
    if not records and not live:
        return ""
    last = records[-1] if records else {}
    acc = ((live or {}).get("account") or (d or {}).get("account") or {})
    decision = ((live or {}).get("decision") or last.get("decision") or {})
    date = (live or {}).get("date") or last.get("date") or "?"
    pts = [(r.get("date"), r.get("asset")) for r in records if r.get("asset")]
    if len(pts) >= 2:
        svg = line_chart(
            [{"name": "基金净值", "color": ORANGE, "pts": cum_pct(pts),
              "area": True, "w": 1.7}], y_fmt=fmt_pct_tick)
    else:
        svg = '<div class="empty">暂无数据</div>'
    pos = {k: v for k, v in (acc.get("positions") or {}).items()
           if v.get("shares")}
    pos_txt = "、".join(f"{k} {v.get('pct', 0):.0f}%" for k, v in pos.items()) or "空仓"
    trade = (live or {}).get("trade", last.get("trade"))
    if isinstance(trade, str):
        trade_txt = trade
    else:
        trade_txt = getattr(trade, "reason", str(trade))
    total = acc.get("total_asset") or last.get("asset")
    total_txt = f"{total:,.2f} 元" if total is not None else "—"
    return f"""
<h2 id="fund">旧版基金模拟盘（etf-c）</h2>
<div class="sub2">场外 ETF 联接 C 类 · 多模型 ensemble · 已切换为股票模式，保留历史展示</div>
<div class="card">
<div class="big">{total_txt}</div>
<div class="lbl">实盘快照截至 {date} · 已实现 {acc.get('realized_pnl', 0):,.2f} ·
未实现 {acc.get('unrealized_pnl', 0):,.2f} · 现金 {acc.get('cash', 0):,.2f}</div>
</div>
<div class="card"><div class="lbl">基金回测净值曲线（累计收益，2025-01 起）</div>{svg}
<div class="legend"><span class="lg"><i style="background:{ORANGE}"></i>{len(pts)} 条净值快照</span></div></div>
<div class="card"><div class="lbl">最近决策（{date}）</div>
<div class="order">{badge(decision.get('action'))}
<b>{html.escape(str(decision.get('target', '')))}</b>
<span class="mut">目标 {decision.get('target_position', '?')}% ·
置信 {decision.get('confidence', '?')}</span>
<div class="why">{html.escape(str(decision.get('reason', '')))}</div></div>
<table><tr><td>持仓</td><td class="r">{html.escape(pos_txt)}</td></tr>
<tr><td>执行</td><td class="r">{html.escape(str(trade_txt))[:120]}</td></tr></table></div>
"""


def sys_section(health_path):
    lines = []
    try:
        with open(health_path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                s = line.strip()
                if s.startswith("- ") and "二进制下载" not in s:
                    lines.append(s[2:])
    except OSError:
        pass
    body = "<br>".join(html.escape(x) for x in lines) if lines else "无"
    return (f'<h2 id="sys">机器状态</h2>'
            f'<div class="card"><div class="mut">{body}</div></div>')


CSS = """
*{box-sizing:border-box}
body{font-family:Helvetica,Arial,sans-serif;margin:0;padding:14px 12px 24px;
background:#101114;color:#e8e8e8;font-size:15px;-webkit-text-size-adjust:100%}
h1{font-size:18px;margin:0 0 2px;color:#fff}
h2{font-size:15px;margin:22px 0 6px;color:#fff;border-left:3px solid #4da3ff;
padding-left:8px}
.sub{font-size:12px;color:#888;margin-bottom:10px}
.sub2{font-size:11.5px;color:#8a8f98;margin:-4px 0 8px;line-height:1.6}
.nav{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 6px}
.nav a{font-size:12px;color:#cfd3da;background:#1a1c20;border:1px solid #2a2d33;
border-radius:999px;padding:3px 10px;text-decoration:none}
.card{background:#1a1c20;border-radius:8px;padding:11px 12px;margin-bottom:10px}
.big{font-size:28px;font-weight:bold;color:#fff;word-break:break-all}
.lbl{font-size:12px;color:#999;margin-bottom:4px}
.mut{font-size:11px;color:#8a8f98;line-height:1.6;margin-top:4px}
.empty{font-size:12px;color:#777;padding:6px 0}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:2px}
th{font-size:11px;color:#8a8f98;font-weight:normal;text-align:left;
padding:3px 2px;border-bottom:1px solid #2a2d33}
td{padding:4px 2px;color:#ccc;border-bottom:1px solid #202329;vertical-align:top}
td.r,th.r{text-align:right}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.scroll table{min-width:560px}
.heat{table-layout:fixed;font-size:10.5px}
.heat th{text-align:center;padding:2px 0}
.heat td{text-align:center;padding:3px 0;border:none;font-size:10.5px;
border-radius:2px}
.legend{margin-top:6px;display:flex;flex-wrap:wrap;gap:10px}
.lg{font-size:11px;color:#cfd3da;display:inline-flex;align-items:center;gap:4px}
.lg i{width:12px;height:3px;border-radius:2px;display:inline-block}
.badge{display:inline-block;font-size:10.5px;border:1px solid #888;border-radius:3px;
padding:0 5px;margin-right:5px;vertical-align:1px}
.order{margin-top:7px;font-size:13px;line-height:1.5}
.why{font-size:11.5px;color:#9aa0a8;margin-top:2px;line-height:1.55}
.view{font-size:12.5px;color:#ffd34d;line-height:1.6;margin-top:4px}
details{margin:6px 0;font-size:12px;color:#9aa0a8}
summary{cursor:pointer;color:#cfd3da;font-size:12px;margin-bottom:4px}
.banner{font-size:12.5px;border-radius:8px;padding:8px 11px;margin:0 0 10px;
border:1px solid;line-height:1.5}
.banner.open{background:#12241a;border-color:#1f4d33;color:#8fe3ac}
.banner.closed{background:#2a2118;border-color:#5a3d1e;color:#ffc078}
.banner b{font-weight:bold}
.stats{display:flex;flex-wrap:wrap;gap:8px}
.stat{flex:1 1 30%;min-width:86px;background:#14161a;border-radius:8px;
padding:8px 10px}
.stat .v{font-size:17px;font-weight:bold;color:#fff;white-space:nowrap}
.stat .k{font-size:10.5px;color:#8a8f98;margin-top:1px}
tr.mine td{background:#1e2430}
.warn{color:#ffc078}
.foot{font-size:11px;color:#666;margin-top:16px;text-align:center;line-height:1.7}
.foot a{color:#4da3ff;text-decoration:none}
"""


def build(base, health, out):
    mk = market_info(base)
    stock = stock_section(base, mk)
    backtest = backtest_section(base)
    fund = fund_section(base)
    sys_part = sys_section(health)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    banner = (f'<div class="banner {"open" if mk["open"] else "closed"}">'
              f'<b>{mk["tag"]}</b> · {html.escape(mk["text"])}</div>')
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Quant · 模拟盘 &amp; 回测</title>
<style>{CSS}</style>
</head>
<body>
<h1>AI Quant · OrangePi</h1>
<div class="sub">常驻模拟盘 + 全缓存回测 · 页面更新 {now}</div>
{banner}
<nav class="nav">
<a href="#live">模拟盘</a><a href="#bt">回测</a><a href="#fund">基金</a>
<a href="#sys">机器</a>
</nav>
{stock}
{backtest}
{fund}
{sys_part}
<div class="foot">自动生成 · 每30分钟刷新 · <code>scripts/gen_status.py</code><br>
本地预测系统 <a href="https://github.com/Languangxun/stock-analyzer"
target="_blank" rel="noopener">stock-analyzer</a> · 不构成投资建议</div>
</body>
</html>
"""
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_doc)
    os.replace(tmp, out)
    return out, len(html_doc)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="生成 AI Quant 状态页")
    parser.add_argument("--base", default=os.path.dirname(here),
                        help="ai-quant 根目录（默认脚本上一级）")
    parser.add_argument("--out", default="/var/www/status/index.html",
                        help="输出 HTML 路径")
    parser.add_argument("--health", default="/tmp/health.json")
    args = parser.parse_args()
    out, size = build(args.base, args.health, args.out)
    print(f"ok -> {out} ({size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
