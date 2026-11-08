#!/usr/bin/env python3
"""Quant Market Dashboard — 行情看板 + Quant 状态 + 项目入口。
主页：实时行情看板
/quant：原 quant 机器状态页
/projects：项目入口
/api/markets：行情 JSON
/api/quote?code=xxx：单标 JSON
/api/kline?code=xxx&period=day|week|month&tail=250：K线 JSON
/api/indicators?code=xxx：K线+指标序列 JSON（开发者数据源）
/api/search?q=xxx：代码/名称搜索
/kline?code=xxx&period=day|week|month：全屏K线（多指标副图）
/quote?code=xxx&period=day|week|month：个股详情（K线主图 + MA/BOLL 叠层 + 副图指标）
/docs：开发者接入文档
"""
import os
import sys
import json
import glob
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(BASE_DIR, "sim", "state")
LOG_DIR = os.path.join(BASE_DIR, "logs")
BACKTEST_DIR = os.path.join(BASE_DIR, "backtest", "results")
HOST_LABEL = "quant(100.126.30.51 / 192.168.3.94)"
MARKETS = ["fund", "hk", "us", "crypto", "fut"]

# 行情缓存（秒）
QUOTE_CACHE_TTL = 60
_quote_cache = {"ts": 0, "data": []}

# 首页预定义观察列表（避免全市场扫描过慢）
WATCHLIST = {
    "cn": [
        ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
        ("sh600519", "贵州茅台"), ("sz000858", "五粮液"), ("sh601318", "中国平安"),
        ("sh600036", "招商银行"), ("sz002594", "比亚迪"), ("sh601012", "隆基绿能"),
        ("sz300750", "宁德时代"),
    ],
    "hk": [
        ("hk:00700", "腾讯控股"), ("hk:09988", "阿里巴巴"), ("hk:03690", "美团"),
        ("hk:01810", "小米集团"), ("hk:02015", "理想汽车"), ("hk:09618", "京东集团"),
        ("hk:02318", "中国平安H"), ("hk:00005", "汇丰控股"),
    ],
    "us": [
        ("us:AAPL", "Apple"), ("us:TSLA", "Tesla"), ("us:NVDA", "NVIDIA"),
        ("us:MSFT", "Microsoft"), ("us:GOOGL", "Alphabet"), ("us:AMZN", "Amazon"),
        ("us:META", "Meta"), ("us:BABA", "Alibaba"), ("us:JD", "京东"),
    ],
    "crypto": [
        ("crypto:BTC/USDT", "BTC/USDT"), ("crypto:ETH/USDT", "ETH/USDT"),
        ("crypto:SOL/USDT", "SOL/USDT"), ("crypto:ADA/USDT", "ADA/USDT"),
        ("crypto:APT/USDT", "Aptos"), ("crypto:ARB/USDT", "Arbitrum"),
        ("crypto:ATOM/USDT", "Cosmos"), ("crypto:AVAX/USDT", "Avalanche"),
    ],
    "fut": [
        ("fut:IH0", "上证50期货"), ("fut:UR0", "尿素期货"), ("fut:CF0", "棉花期货"),
        ("fut:RB0", "螺纹钢期货"), ("fut:CU0", "沪铜期货"), ("fut:AU0", "黄金期货"),
        ("fut:SC0", "原油期货"), ("fut:AG0", "白银期货"),
    ],
}


def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout.strip() or "(无输出)"
    except Exception as e:
        return f"(失败: {e})"


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def fmt_size(num_bytes):
    try:
        b = int(num_bytes)
        for u in ["B", "KB", "MB", "GB", "TB"]:
            if b < 1024:
                return f"{b:.2f} {u}"
            b /= 1024
        return f"{b:.2f} PB"
    except Exception:
        return str(num_bytes)


def fmt_money(v):
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return str(v)


def fmt_pct(v):
    try:
        return f"{float(v)*100:.2f}%"
    except Exception:
        return str(v)


def fmt_vol(v):
    """成交量/成交额中文单位：亿 / 万 / 手。"""
    try:
        x = float(v)
        if x >= 1e8:
            return f"{x/1e8:.2f} 亿"
        if x >= 1e4:
            return f"{x/1e4:.2f} 万"
        return f"{x:,.0f}"
    except Exception:
        return str(v)


# ============== 文件下载 ==============

DOWNLOAD_WHITELIST = {
    "data": os.path.join(BASE_DIR, "data"),
    "scripts": os.path.join(BASE_DIR, "scripts"),
    "scripts_cli": os.path.join(BASE_DIR, "scripts", "cli"),
    "database": os.path.join(BASE_DIR, "database"),
    "factors": os.path.join(BASE_DIR, "factors"),
    "config": os.path.join(BASE_DIR, "config"),
    "backtest": os.path.join(BASE_DIR, "backtest"),
    "docs": os.path.join(BASE_DIR, "docs"),
}


def resolve_download_path(rel):
    """只允许下载白名单目录下的文件，防止目录遍历；黑名单命中返回 None。"""
    rel = rel.lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    if rel in DOWNLOAD_BLOCKLIST:
        return None
    for prefix, base in DOWNLOAD_WHITELIST.items():
        if rel.startswith(prefix + "/"):
            full = os.path.join(base, rel[len(prefix) + 1:])
            real = os.path.realpath(full)
            if real.startswith(os.path.realpath(base) + os.sep) or real == os.path.realpath(base):
                if os.path.isfile(real):
                    return real
    return None


def send_file_with_range(handler, filepath):
    size = os.path.getsize(filepath)
    range_hdr = handler.headers.get("Range", "")
    start, end = 0, size - 1
    status = 200
    if range_hdr.startswith("bytes="):
        try:
            spec = range_hdr[6:].split(",")[0]
            if "-" in spec:
                a, b = spec.split("-", 1)
                if a:
                    start = int(a)
                if b:
                    end = int(b)
                else:
                    end = size - 1
                status = 206
        except Exception:
            pass
    length = end - start + 1
    handler.send_response(status)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Content-Disposition",
                        f'attachment; filename="{os.path.basename(filepath)}"')
    handler.end_headers()
    with open(filepath, "rb") as f:
        f.seek(start)
        while length > 0:
            chunk = f.read(min(65536, length))
            if not chunk:
                break
            handler.wfile.write(chunk)
            length -= len(chunk)


# ============== 可下载资源列表 ==============

DOWNLOAD_RESOURCES = {
    "数据库": [
        ("data/global_daily.db", "全球市场日频库 (HK/US/CRYPTO/FUT)，SQLite，约 1.3GB"),
        ("scripts/cli/stock_cache.db", "A股日频缓存库 + 代码表/市值分层/F10基本面/分钟线，SQLite，约 2.2GB"),
    ],
    "开发文档": [
        ("docs/API.md", "开发者接入文档：行情源 REST API + 本地数据库直连（Markdown 源文件）"),
    ],
    "数据工具": [
        ("scripts/sync_global_daily.py", "全球市场增量同步工具 (HK/US/CRYPTO/FUT)"),
        ("scripts/build_global_daily.py", "全球市场全量建库工具"),
        ("scripts/cli/stock_predict.py", "stock-analyzer / 桌面 predict 核心：形态匹配 + 回测 + 数据清洗/回填"),
        ("scripts/daily_data_sync.py", "每日自动补全数据脚本（全球 + A股 watchlist）"),
        ("scripts/daily_status.sh", "每日状态邮件 + 模拟盘任务脚本"),
        ("scripts/daily_stock.sh", "A股交易日自动跑盘脚本"),
    ],
    "指标/因子代码": [
        ("factors/technical.py", "技术指标因子 (MA/RSI/MACD/布林带等)"),
        ("factors/morphology.py", "形态因子"),
        ("factors/sm701.py", "sm701 LightGBM/TCN 选股因子"),
        ("factors/ml.py", "机器学习特征工程"),
        ("factors/chaodi.py", "抄底因子"),
        ("data/features/technical.py", "data/features 技术指标入口"),
    ],
    "配置": [
        ("config/markets.yaml", "多市场配置 (fund/hk/us/crypto/fut)"),
        ("config/stock.yaml", "A股策略配置"),
    ],
}

# 下载黑名单：含明文授权码/密钥的文件禁止下载（防止密钥外泄）。
# 若变量来自环境文件/密钥库，恢复下载前先完成脱敏。
DOWNLOAD_BLOCKLIST = {
    "scripts/quant_status_mail.py",   # 内含 SMTP 授权码明文
    "scripts/quant_status_web.py",    # 主服务脚本（源码不在下载范围）
    "scripts/check_secrets.py",       # 密钥检查工具（描述敏感文件路径）
    ".env", ".env.local", "config/model.yaml",
    "scripts/daily_status.sh", "scripts/daily_stock.sh",  # 含环境加载
    "scripts/sync_global_daily.py",   # 数据口径工具（内部）
}


def quote_page(code, period="day"):
    u = _load_unified()
    raw_bars = _get_bars(u, code, tail=250)
    if not raw_bars:
        return base_page("无数据", f'<h1>⚠️ 无数据</h1><p>未找到 {code} 的行情数据。</p>', active="")

    bars = _resample_bars(raw_bars, period)
    info = _get_stock_info(code)
    name = info.get("name", code)
    is_index = info.get("_is_index", False)
    ind = _calc_indicators(bars)
    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else bars[-1]
    change_pct = (last.close - prev.close) / prev.close if prev.close else 0
    cls = "up" if change_pct >= 0 else "down"
    sign = "+" if change_pct >= 0 else ""

    # K线 JSON（含指标序列，用于前端主图/副图层叠绘制）
    kdata = []
    for i, b in enumerate(bars):
        kd = {"date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "vol": b.vol}
        for g, vals in ind.items():
            if isinstance(vals, dict):
                for k, v in vals.items():
                    kd[f"{g}_{k}"] = v[i]
        kdata.append(kd)
    kdata_json = json.dumps(kdata)

    # 周期按钮
    periods = {"day": "日K", "week": "周K", "month": "月K"}
    period_btns = " ".join(
        f'<a class="tab {"active" if p==period else ""}" href="/quote?code={code}&period={p}">{label}</a>'
        for p, label in periods.items()
    )

    # 头部关键数据
    header_extra = []
    if not is_index:
        if "总市值" in info:
            header_extra.append(f"总市值 {info['总市值']}")
        if "市盈率(PE)" in info and info["市盈率(PE)"]:
            header_extra.append(f"PE {info['市盈率(PE)']}")
        if "市净率(PB)" in info and info["市净率(PB)"]:
            header_extra.append(f"PB {info['市净率(PB)']}")
    if "换手率(%)" in info and info["换手率(%)"]:
        header_extra.append(f"换手 {info['换手率(%)']}%")
    header_extra_html = " &nbsp;|&nbsp; ".join(header_extra)
    if header_extra_html:
        header_extra_html = " &nbsp;|&nbsp; " + header_extra_html

    # 基本资料（中文化，过滤空值与内部字段）
    info_rows = []
    skip_keys = {"code", "name", "_is_index", "_industry"}
    for k, v in info.items():
        if k in skip_keys:
            continue
        if v is None or v == "" or str(v).lower() == "none":
            continue
        if isinstance(v, (int, float)) and v == 0 and k in ("市盈率(PE)", "市净率(PB)"):
            continue
        info_rows.append(f"<tr><td><strong>{k}</strong></td><td>{v}</td></tr>")
    info_html = f"<table>{''.join(info_rows)}</table>" if info_rows else "<p>暂无基本面数据</p>"

    # 最近 K 线表格
    recent_rows = []
    for b in bars[-15:][::-1]:
        c_cls = "up" if b.close >= b.open else "down"
        recent_rows.append(
            f"<tr><td>{b.date}</td><td>{fmt_money(b.open)}</td><td>{fmt_money(b.high)}</td>"
            f"<td>{fmt_money(b.low)}</td><td class='{c_cls}'>{fmt_money(b.close)}</td>"
            f"<td>{fmt_vol(b.vol)}</td></tr>"
        )

    # 相关标的
    related = _get_related(code)
    related_html = " ".join(
        f'<a class="badge" href="/quote?code={r["code"]}">{r["name"]} ({r["code"]})</a>'
        for r in related
    )

    content = f"""
    <h1>{name} <span style="color:var(--muted);font-size:20px">{code}</span></h1>
    <div class="quote-header">
        <div class="quote-price {cls}">{fmt_money(last.close)}</div>
        <div class="quote-change {cls}">{sign}{fmt_pct(change_pct)} &nbsp; {sign}{fmt_money(last.close - prev.close)}</div>
        <div class="quote-meta">
            今开 {fmt_money(last.open)} &nbsp;|&nbsp; 最高 {fmt_money(last.high)} &nbsp;|&nbsp; 最低 {fmt_money(last.low)} &nbsp;|&nbsp; 昨收 {fmt_money(prev.close)} &nbsp;|&nbsp; 成交量 {fmt_vol(last.vol)}{header_extra_html}
        </div>
    </div>

    <div class="tabs">{period_btns} <a class="tab" href="/kline?code={code}&period={period}">📈 全屏K线</a></div>

    {kline_component(kdata_json)}

    <div class="grid">
        <div class="card"><h2>基本资料</h2>{info_html}</div>
    </div>

    <div class="card">
        <h2>最近 15 根 K 线</h2>
        <table>
            <tr><th>日期</th><th>开盘</th><th>最高</th><th>最低</th><th>收盘</th><th>成交量</th></tr>
            {''.join(recent_rows)}
        </table>
    </div>

    <div class="card">
        <h2>相关标的</h2>
        <p>{related_html or '无相关标的'}</p>
    </div>
    """ + KLINE_SUBSTYLES

    return base_page(f"{name} ({code})", content, active="")


# ============== K线前端组件 ==============

KLINE_JS_TEMPLATE = '\nconst kdata = __KDATA__;\nconst canvas = document.getElementById(\'klineCanvas\');\nconst ctx = canvas.getContext(\'2d\');\nconst tooltip = document.getElementById(\'klineTooltip\');\n\nconst UP = \'#3fb950\', DOWN = \'#f85149\';\nconst C = {\n  ma5:\'#f0883e\', ma10:\'#58a6ff\', ma20:\'#a371f7\',\n  ma30:\'rgba(240,136,62,0.55)\', ma60:\'rgba(163,113,247,0.45)\',\n  bollUp:\'rgba(248,81,73,0.85)\', bollMid:\'rgba(200,214,229,0.75)\', bollDown:\'rgba(63,185,80,0.85)\',\n  bollFill:\'rgba(88,166,255,0.10)\',\n  dif:\'#f0883e\', dea:\'#58a6ff\', k:\'#f0883e\', d:\'#58a6ff\', j:\'#a371f7\',\n  rsi6:\'#f0883e\', rsi12:\'#58a6ff\', rsi24:\'#a371f7\'\n};\nconst state = { ma:true, boll:true, vol:true, macd:true, kdj:true, rsi:true, hover:-1 };\nconst MAIN_H = 380, X_AXIS_H = 24;\nconst PAD = { left: 66, right: 18 };\n\ndocument.querySelectorAll(\'#indToggles [data-t]\').forEach(b => b.addEventListener(\'click\', () => {\n  const t = b.dataset.t;\n  state[t] = !state[t];\n  b.classList.toggle(\'active\', state[t]);\n  draw();\n}));\n\nfunction panels(){\n  const list = [{ id:\'main\', h: MAIN_H }];\n  if (state.vol)  list.push({ id:\'vol\',  h: 100 });\n  if (state.macd) list.push({ id:\'macd\', h: 122 });\n  if (state.kdj)  list.push({ id:\'kdj\',  h: 122 });\n  if (state.rsi)  list.push({ id:\'rsi\',  h: 104 });\n  return list;\n}\n\nfunction fmtNum(v, d){\n  if (v === null || v === undefined || isNaN(v)) return \'-\';\n  return Number(v).toFixed(d === undefined ? 2 : d);\n}\nfunction volFmt(x){\n  if (x >= 1e8) return (x/1e8).toFixed(2) + \'亿\';\n  if (x >= 1e4) return (x/1e4).toFixed(2) + \'万\';\n  return Number(x).toLocaleString();\n}\nfunction rawVal(key){\n  const d = kdata[state.hover >= 0 ? state.hover : kdata.length - 1];\n  return d ? d[key] : null;\n}\nfunction val(key){ return fmtNum(rawVal(key)); }\n\nfunction drawSeries(getV, step, top, h, min, max, color, width){\n  ctx.strokeStyle = color; ctx.lineWidth = width || 1.2; ctx.beginPath();\n  let started = false;\n  for (let i = 0; i < kdata.length; i++){\n    const v = getV(i);\n    if (v === null || v === undefined || isNaN(v)){ started = false; continue; }\n    const x = PAD.left + i * step + step / 2;\n    const y = top + (max - v) / (max - min) * h;\n    if (!started){ ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);\n  }\n  ctx.stroke();\n}\n\nfunction legend(top, parts){\n  ctx.textAlign = \'left\'; ctx.font = \'11px sans-serif\';\n  let x = PAD.left + 8;\n  for (const p of parts){\n    ctx.fillStyle = p[1] || \'#8b949e\';\n    ctx.fillText(p[0], x, top + 14);\n    x += ctx.measureText(p[0]).width + 12;\n  }\n}\n\nfunction gridPanel(top, h, min, max, fmt, skipBottom){\n  ctx.strokeStyle = \'#21262d\'; ctx.lineWidth = 1;\n  ctx.beginPath();\n  for (let i = 0; i <= 4; i++){\n    const y = top + h * i / 4;\n    ctx.moveTo(PAD.left, y); ctx.lineTo(canvas.width - PAD.right, y);\n  }\n  ctx.stroke();\n  const f = fmt || (v => fmtNum(v));\n  ctx.fillStyle = \'#8b949e\'; ctx.font = \'10px monospace\'; ctx.textAlign = \'left\';\n  for (let i = 0; i <= 4; i++){\n    if (i === 4 && skipBottom) continue;\n    const y = top + h * i / 4;\n    const v = max - (max - min) * i / 4;\n    // 顶部刻度下挪 12px，避免与上一个面板的底部刻度重叠\n    const dy = (i === 0 && top > 0) ? 12 : 3;\n    ctx.fillText(f(v), 4, y + dy);\n  }\n}\n\nfunction draw(){\n  const n = kdata.length;\n  if (!n) return;\n  const list = panels();\n  let topAcc = 0;\n  const layers = list.map(p => { const l = { id: p.id, h: p.h, top: topAcc }; topAcc += p.h; return l; });\n  const totalH = topAcc + X_AXIS_H;\n  canvas.height = totalH;\n  const W = canvas.width;\n  const chartW = W - PAD.left - PAD.right;\n  const step = chartW / n;\n  const candleW = Math.max(1.5, Math.min(10, step * 0.62));\n  const bg = getComputedStyle(document.body).getPropertyValue(\'--bg\').trim() || \'#0b0e11\';\n  ctx.fillStyle = bg; ctx.fillRect(0, 0, W, totalH);\n  const L = id => layers.find(l => l.id === id);\n  const lastId = layers[layers.length - 1].id;\n\n  // ---------- 主图：K线 + MA + BOLL 通道 ----------\n  const M = L(\'main\');\n  let maxH = -Infinity, minL = Infinity;\n  for (const d of kdata){ if (d.high > maxH) maxH = d.high; if (d.low < minL) minL = d.low; }\n  if (state.boll) for (const d of kdata){\n    if (d.BOLL_UP !== null && d.BOLL_UP !== undefined && d.BOLL_UP > maxH) maxH = d.BOLL_UP;\n    if (d.BOLL_DOWN !== null && d.BOLL_DOWN !== undefined && d.BOLL_DOWN < minL) minL = d.BOLL_DOWN;\n  }\n  if (maxH === minL) maxH = minL + 1;\n  const padV = (maxH - minL) * 0.06 || 1;\n  maxH += padV; minL -= padV;\n  const yP = v => M.top + (maxH - v) / (maxH - minL) * M.h;\n\n  gridPanel(M.top, M.h, minL, maxH, null, M.id !== lastId);\n\n  if (state.boll){\n    ctx.beginPath(); let started = false;\n    for (let i = 0; i < n; i++){\n      const u = kdata[i].BOLL_UP;\n      if (u === null || u === undefined) continue;\n      const x = PAD.left + i * step + step / 2, y = yP(u);\n      if (!started){ ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);\n    }\n    for (let i = n - 1; i >= 0; i--){\n      const l = kdata[i].BOLL_DOWN;\n      if (l === null || l === undefined) continue;\n      ctx.lineTo(PAD.left + i * step + step / 2, yP(l));\n    }\n    ctx.closePath(); ctx.fillStyle = C.bollFill; ctx.fill();\n    drawSeries(i => kdata[i].BOLL_UP,   step, M.top, M.h, minL, maxH, C.bollUp, 1);\n    drawSeries(i => kdata[i].BOLL_MID,  step, M.top, M.h, minL, maxH, C.bollMid, 1);\n    drawSeries(i => kdata[i].BOLL_DOWN, step, M.top, M.h, minL, maxH, C.bollDown, 1);\n  }\n\n  if (state.ma){\n    drawSeries(i => kdata[i].MA_MA5,  step, M.top, M.h, minL, maxH, C.ma5, 1.2);\n    drawSeries(i => kdata[i].MA_MA10, step, M.top, M.h, minL, maxH, C.ma10, 1.2);\n    drawSeries(i => kdata[i].MA_MA20, step, M.top, M.h, minL, maxH, C.ma20, 1.2);\n    drawSeries(i => kdata[i].MA_MA30, step, M.top, M.h, minL, maxH, C.ma30, 1);\n    drawSeries(i => kdata[i].MA_MA60, step, M.top, M.h, minL, maxH, C.ma60, 1);\n  }\n\n  for (let i = 0; i < n; i++){\n    const d = kdata[i];\n    const x = PAD.left + i * step + step / 2;\n    const up = d.close >= d.open;\n    ctx.strokeStyle = up ? UP : DOWN; ctx.fillStyle = up ? UP : DOWN; ctx.lineWidth = 1;\n    ctx.beginPath(); ctx.moveTo(x, yP(d.high)); ctx.lineTo(x, yP(d.low)); ctx.stroke();\n    const yO = yP(d.open), yC = yP(d.close);\n    const cTop = Math.min(yO, yC), bh = Math.max(1, Math.abs(yC - yO));\n    if (up) ctx.strokeRect(x - candleW / 2, cTop, candleW, bh);\n    else ctx.fillRect(x - candleW / 2, cTop, candleW, bh);\n  }\n\n  const mainLegend = [[\'BOLL 20,2\', \'#8b949e\'], [\'mid \' + val(\'BOLL_MID\'), C.bollMid]];\n  if (state.ma) mainLegend.unshift(\n    [\'MA5 \' + val(\'MA_MA5\'), C.ma5], [\'MA10 \' + val(\'MA_MA10\'), C.ma10],\n    [\'MA20 \' + val(\'MA_MA20\'), C.ma20], [\'MA30 \' + val(\'MA_MA30\'), C.ma30], [\'MA60 \' + val(\'MA_MA60\'), C.ma60]\n  );\n  mainLegend.push([\'tick \' + fmtNum(vmin0(minL + padV)) + \' - \' + fmtNum(maxH - padV), \'#8b949e\']);\n  legend(M.top, mainLegend);\n\n  // ---------- VOL ----------\n  const V = L(\'vol\');\n  if (V){\n    let maxVol = 0;\n    for (const d of kdata) if ((d.vol || 0) > maxVol) maxVol = d.vol;\n    maxVol = maxVol || 1;\n    gridPanel(V.top, V.h, 0, maxVol, volFmt, V.id !== lastId);\n    for (let i = 0; i < n; i++){\n      const d = kdata[i];\n      if (!d.vol && d.vol !== 0) continue;\n      const x = PAD.left + i * step + step / 2;\n      const h = (d.vol || 0) / maxVol * V.h;\n      ctx.fillStyle = d.close >= d.open ? \'rgba(63,185,80,0.7)\' : \'rgba(248,81,73,0.7)\';\n      ctx.fillRect(x - candleW / 2, V.top + V.h - h, Math.max(1, candleW), h);\n    }\n    legend(V.top, [[\'VOL \' + volFmt(rawVal(\'vol\') || 0), \'#8b949e\']]);\n  }\n\n  // ---------- MACD ----------\n  const P = L(\'macd\');\n  if (P){\n    let m = 0;\n    for (const d of kdata) for (const k of [\'MACD_DIF\', \'MACD_DEA\', \'MACD_MACD\']){\n      const v = d[k];\n      if (v !== null && v !== undefined && Math.abs(v) > m) m = Math.abs(v);\n    }\n    m = m || 1;\n    const mmin = -m * 1.15, mmax = m * 1.15;\n    gridPanel(P.top, P.h, mmin, mmax, null, P.id !== lastId);\n    const zy = P.top + (mmax - 0) / (mmax - mmin) * P.h;\n    ctx.strokeStyle = \'#30363d\'; ctx.setLineDash([3, 3]); ctx.beginPath();\n    ctx.moveTo(PAD.left, zy); ctx.lineTo(W - PAD.right, zy); ctx.stroke(); ctx.setLineDash([]);\n    for (let i = 0; i < n; i++){\n      const v = kdata[i].MACD_MACD;\n      if (v === null || v === undefined) continue;\n      const x = PAD.left + i * step + step / 2;\n      const vy = P.top + (mmax - v) / (mmax - mmin) * P.h;\n      ctx.fillStyle = v >= 0 ? \'rgba(63,185,80,0.75)\' : \'rgba(248,81,73,0.75)\';\n      ctx.fillRect(x - candleW / 2, Math.min(zy, vy), Math.max(1, candleW), Math.max(1, Math.abs(zy - vy)));\n    }\n    drawSeries(i => kdata[i].MACD_DIF, step, P.top, P.h, mmin, mmax, C.dif, 1.2);\n    drawSeries(i => kdata[i].MACD_DEA, step, P.top, P.h, mmin, mmax, C.dea, 1.2);\n    const hv = rawVal(\'MACD_MACD\');\n    legend(P.top, [[\'MACD 12,26,9\', \'#8b949e\'],\n      [\'DIF \' + val(\'MACD_DIF\'), C.dif], [\'DEA \' + val(\'MACD_DEA\'), C.dea],\n      [\'BAR \' + val(\'MACD_MACD\'), (hv !== null && hv >= 0) ? UP : DOWN]]);\n  }\n\n  // ---------- KDJ ----------\n  const Kp = L(\'kdj\');\n  if (Kp){\n    let kmax = -Infinity, kmin = Infinity;\n    for (const d of kdata) for (const k of [\'KDJ_K\', \'KDJ_D\', \'KDJ_J\']){\n      const v = d[k];\n      if (v !== null && v !== undefined){ if (v > kmax) kmax = v; if (v < kmin) kmin = v; }\n    }\n    if (kmax === -Infinity){ kmax = 100; kmin = 0; }\n    const th = (kmax - kmin) * 0.08 || 1;\n    kmax += th; kmin -= th;\n    gridPanel(Kp.top, Kp.h, kmin, kmax, null, Kp.id !== lastId);\n    [80, 20].forEach(g => {\n      const y = Kp.top + (kmax - g) / (kmax - kmin) * Kp.h;\n      ctx.strokeStyle = \'rgba(139,148,158,0.5)\'; ctx.setLineDash([2, 4]); ctx.beginPath();\n      ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke(); ctx.setLineDash([]);\n    });\n    drawSeries(i => kdata[i].KDJ_K, step, Kp.top, Kp.h, kmin, kmax, C.k, 1.2);\n    drawSeries(i => kdata[i].KDJ_D, step, Kp.top, Kp.h, kmin, kmax, C.d, 1.2);\n    drawSeries(i => kdata[i].KDJ_J, step, Kp.top, Kp.h, kmin, kmax, C.j, 1.2);\n    legend(Kp.top, [[\'KDJ 9,3,3\', \'#8b949e\'],\n      [\'K \' + val(\'KDJ_K\'), C.k], [\'D \' + val(\'KDJ_D\'), C.d], [\'J \' + val(\'KDJ_J\'), C.j]]);\n  }\n\n  // ---------- RSI ----------\n  const R = L(\'rsi\');\n  if (R){\n    gridPanel(R.top, R.h, 0, 100, null, R.id !== lastId);\n    [70, 30].forEach(g => {\n      const y = R.top + (100 - g) / 100 * R.h;\n      ctx.strokeStyle = \'rgba(139,148,158,0.5)\'; ctx.setLineDash([2, 4]); ctx.beginPath();\n      ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke(); ctx.setLineDash([]);\n    });\n    drawSeries(i => kdata[i].RSI_RSI6,  step, R.top, R.h, 0, 100, C.rsi6, 1.2);\n    drawSeries(i => kdata[i].RSI_RSI12, step, R.top, R.h, 0, 100, C.rsi12, 1.2);\n    drawSeries(i => kdata[i].RSI_RSI24, step, R.top, R.h, 0, 100, C.rsi24, 1.2);\n    legend(R.top, [[\'RSI\', \'#8b949e\'],\n      [\'RSI6 \' + val(\'RSI_RSI6\'), C.rsi6], [\'RSI12 \' + val(\'RSI_RSI12\'), C.rsi12], [\'RSI24 \' + val(\'RSI_RSI24\'), C.rsi24]]);\n  }\n\n  // ---------- x 轴日期 ----------\n  ctx.fillStyle = \'#8b949e\'; ctx.font = \'10px sans-serif\'; ctx.textAlign = \'left\';\n  const every = Math.max(1, Math.ceil(n / 7));\n  for (let i = 0; i < n; i += every){\n    const x = PAD.left + i * step + step / 2;\n    ctx.fillText(kdata[i].date.slice(5), x - 18, totalH - 8);\n  }\n\n  // ---------- 十字光标 ----------\n  if (state.hover >= 0){\n    const x = PAD.left + state.hover * step + step / 2;\n    ctx.strokeStyle = \'rgba(88,166,255,0.8)\'; ctx.setLineDash([4, 4]);\n    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, totalH - X_AXIS_H); ctx.stroke();\n    ctx.setLineDash([]);\n  }\n}\n\nfunction vmin0(x){ return Math.max(0, x); }\n\nfunction updateTooltip(){\n  if (state.hover < 0){ tooltip.innerHTML = \'\'; return; }\n  const d = kdata[state.hover];\n  const cc = d.close >= d.open ? \'up\' : \'down\';\n  const parts = [\n    \'<b>\' + d.date + \'</b>\',\n    \'开 <span class="\' + cc + \'">\' + fmtNum(d.open) + \'</span>\',\n    \'高 <span class="up">\' + fmtNum(d.high) + \'</span>\',\n    \'低 <span class="down">\' + fmtNum(d.low) + \'</span>\',\n    \'收 <span class="\' + cc + \'">\' + fmtNum(d.close) + \'</span>\',\n    \'量 \' + volFmt(d.vol)\n  ];\n  let rows = parts.join(\' &nbsp; \');\n  if (state.ma)  rows += \'<br/>MA5 \' + val(\'MA_MA5\') + \' / MA10 \' + val(\'MA_MA10\') + \' / MA20 \' + val(\'MA_MA20\') + \' / MA30 \' + val(\'MA_MA30\') + \' / MA60 \' + val(\'MA_MA60\');\n  if (state.boll) rows += \'<br/>BOLL 上 \' + val(\'BOLL_UP\') + \' / 中 \' + val(\'BOLL_MID\') + \' / 下 \' + val(\'BOLL_DOWN\');\n  if (state.macd) rows += \'<br/>MACD DIF \' + val(\'MACD_DIF\') + \' / DEA \' + val(\'MACD_DEA\') + \' / BAR \' + val(\'MACD_MACD\');\n  if (state.kdj)  rows += \'<br/>KDJ K \' + val(\'KDJ_K\') + \' / D \' + val(\'KDJ_D\') + \' / J \' + val(\'KDJ_J\');\n  if (state.rsi)  rows += \'<br/>RSI6 \' + val(\'RSI_RSI6\') + \' / RSI12 \' + val(\'RSI_RSI12\') + \' / RSI24 \' + val(\'RSI_RSI24\');\n  tooltip.innerHTML = rows;\n}\n\ncanvas.addEventListener(\'mousemove\', e => {\n  const rect = canvas.getBoundingClientRect();\n  const x = (e.clientX - rect.left) * (canvas.width / rect.width);\n  const step = (canvas.width - PAD.left - PAD.right) / kdata.length;\n  const i = Math.floor((x - PAD.left) / step);\n  const ni = (i >= 0 && i < kdata.length) ? i : -1;\n  if (ni !== state.hover){ state.hover = ni; draw(); }\n  updateTooltip();\n});\ncanvas.addEventListener(\'mouseleave\', () => { state.hover = -1; draw(); updateTooltip(); });\ndraw();\nupdateTooltip();\n'

KLINE_HTML_HEAD = '\n    <div class="kline-box">\n        <div class="tabs" id="indToggles">\n            <span class="chip-label">图层/副图：</span>\n            <button class="tab active" data-t="boll">BOLL通道</button>\n            <button class="tab active" data-t="ma">MA均线</button>\n            <button class="tab active" data-t="vol">VOL</button>\n            <button class="tab active" data-t="macd">MACD</button>\n            <button class="tab active" data-t="kdj">KDJ</button>\n            <button class="tab active" data-t="rsi">RSI</button>\n        </div>\n        <canvas id="klineCanvas" width="1100" height="760" style="width:100%;max-width:1100px;display:block;cursor:crosshair"></canvas>\n        <div id="klineTooltip" style="margin-top:10px;font-size:13px;color:var(--muted);min-height:34px;line-height:1.7"></div>\n    </div>\n    <style>\n    #indToggles { margin-bottom: 10px; }\n    #indToggles .chip-label { color: var(--muted); font-size: 12px; }\n    #indToggles .tab { padding: 4px 12px; font-size: 12px; }\n    #indToggles .tab.active { background: var(--accent-2); border-color: var(--accent-2); color: #fff; }\n    </style>\n    <script>__JS__</script>\n'

KLINE_SUBSTYLES = '\n    <style>\n    .quote-header { margin-bottom: 20px; }\n    .quote-price { font-size: 42px; font-weight: 700; font-family: "SF Mono", Monaco, monospace; }\n    .quote-change { font-size: 18px; font-weight: 600; margin-top: 4px; font-family: "SF Mono", Monaco, monospace; }\n    .quote-meta { color: var(--muted); font-size: 14px; margin-top: 8px; }\n    </style>\n'



def kline_component(kdata_json):
    """K线主图(MA/BOLL叠加) + VOL/MACD/KDJ/RSI 副图组件。kdata_json 需含 K线与指标字段。"""
    html = KLINE_HTML_HEAD.replace("__JS__", KLINE_JS_TEMPLATE.replace("__KDATA__", kdata_json))
    return html


# ============== K线/指标 API（开发者行情源） ==============


def _kline_payload(code, period="day", tail=250, with_indicators=False):
    """统一组装 K 线 JSON；with_indicators=True 时附指标序列（开发者行情数据源接口）。"""
    try:
        tail = max(30, min(int(tail), 800))
    except (TypeError, ValueError):
        tail = 250
    if period not in ("day", "week", "month"):
        period = "day"
    u = _load_unified()
    # 日K直取 tail 根；周/月K 取相应倍数的原始日K再重采样
    raw_limit = tail * 25 if period == "month" else (tail * 6 if period == "week" else tail)
    raw_bars = _get_bars(u, code, tail=max(30, min(raw_limit, 4000)))
    if not raw_bars:
        return None
    bars = _resample_bars(raw_bars, period)
    if not bars:
        return None
    ind = _calc_indicators(bars) if with_indicators else None
    out = []
    for i, b in enumerate(bars):
        row = {"date": b.date, "open": b.open, "high": b.high, "low": b.low,
               "close": b.close, "vol": b.vol}
        if ind:
            for g, vals in ind.items():
                if isinstance(vals, dict):
                    for k, v in vals.items():
                        row[f"{g}_{k}"] = v[i]
        out.append(row)
    resp = {"code": code, "period": period, "count": len(out), "bars": out}
    if ind:
        resp["indicators"] = ind
    return resp




def download_page():
    sections = []
    for category, items in DOWNLOAD_RESOURCES.items():
        rows = []
        for rel, desc in items:
            full = resolve_download_path(rel)
            if not full:
                continue  # 黑名单命中（含密钥）不列出
            size = fmt_size(os.path.getsize(full)) if full else "不可用"
            rows.append(
                f"<tr><td><strong>{os.path.basename(rel)}</strong></td>"
                f"<td>{desc}</td><td>{size}</td>"
                f'<td><a class="btn" href="/download?file={rel}">⬇ 下载</a></td></tr>'
            )
        sections.append(f"""
        <div class="card">
            <h2>{category}</h2>
            <table>
                <tr><th>文件名</th><th>说明</th><th>大小</th><th>操作</th></tr>
                {''.join(rows)}
            </table>
        </div>
        """)

    contact = """
    <div class="card">
        <h2>📬 站长联系</h2>
        <p>本项目由 <strong>獨白</strong> 维护，欢迎开发者交流。</p>
        <ul>
            <li>个人主页：<a href="https://languangxun.github.io/" target="_blank">https://languangxun.github.io/</a></li>
            <li>Telegram：<a href="https://t.me/Monologue_101" target="_blank">@Monologue_101</a></li>
            <li>QQ：2180287399</li>
        </ul>
        <p style="color:var(--muted);font-size:13px">提示：数据库文件较大（GB 级），建议通过 wget/curl 或支持断点续传的下载工具获取。</p>
    </div>
    """

    usage = """
    <div class="card">
        <h2>🔌 外部程序接入行情</h2>
        <p>行情已开放为 REST API 与本地数据库两种接入方式，相关字段/编码/示例见
           <a href="/docs">开发者接入文档</a> 或直接下载 <a href="/download?file=docs/API.md">docs/API.md</a>。</p>
    </div>
    """ + """
    <div class="card">
        <h2>🛠️ 工具用法速查</h2>
        <h3>全球市场数据补全</h3>
        <pre>python3 scripts/sync_global_daily.py --only hk,us,crypto,fut --workers 4</pre>
        <h3>桌面 predict / stock-analyzer 数据清洗</h3>
        <pre>cd scripts/cli
python3 stock_predict.py --clean          # 清洗异常/除权/退市数据
python3 stock_predict.py --backfill       # 全市场深历史回填
python3 stock_predict.py --refresh-cache  # 刷新代码表与市值分层</pre>
        <h3>数据库结构（global_daily.db）</h3>
        <pre>g.stocks    : code, name, market (HK/US/CRYPTO/FUT)
g.daily_bars: market, code, date, open, high, low, close, vol</pre>
        <h3>数据库结构（stock_cache.db）</h3>
        <pre>daily_bars : A股日K (code, date, open, high, low, close, vol, ...)
stocks     : A股代码表、市值分层、行业
f10_cache  : 基本面数据缓存
quote_cache: 实时行情缓存
minute_cache: 分钟线缓存</pre>
    </div>
    """

    content = f"""
    <h1>⬇ 下载中心</h1>
    <div class="meta">为后续开发者提供的本地数据库、工具脚本与配置资源</div>
    {''.join(sections)}
    {usage}
    {contact}
    """
    return base_page("下载中心", content, active="/downloads")


# ============== 行情数据 ==============

def _load_unified():
    sys.path.insert(0, BASE_DIR)
    from data import unified as u
    return u


def _get_bars(u, code, tail=30):
    try:
        return u.get_bars(code, tail=tail)
    except Exception:
        return []


def _quote_for_code(u, code, name=""):
    bars = _get_bars(u, code, tail=30)
    if len(bars) < 1:
        return None
    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else bars[-1]
    change = last.close - prev.close
    change_pct = (change / prev.close) if prev.close else 0
    return {
        "code": code,
        "name": name or code,
        "date": last.date,
        "open": last.open,
        "high": last.high,
        "low": last.low,
        "close": last.close,
        "prev": prev.close,
        "change": change,
        "change_pct": change_pct,
        "vol": last.vol,
        "history": [(b.date, b.close) for b in bars],
    }


def _resample_bars(bars, period):
    """日K重采样为周K/月K。period: week|month"""
    if period == "day" or not bars:
        return bars
    from collections import OrderedDict
    groups = OrderedDict()
    for b in bars:
        d = b.date
        if period == "week":
            key = d[:4] + "-W" + str(datetime.strptime(d, "%Y-%m-%d").isocalendar()[1]).zfill(2)
        else:
            key = d[:7]
        if key not in groups:
            groups[key] = []
        groups[key].append(b)
    out = []
    for key, g in groups.items():
        out.append(type(bars[0])(
            code=g[0].code,
            date=g[-1].date,
            open=g[0].open,
            high=max(b.high for b in g),
            low=min(b.low for b in g),
            close=g[-1].close,
            vol=sum(b.vol for b in g),
        ))
    return out


def _sma(values, n):
    if len(values) < n:
        return [None] * len(values)
    out = [None] * (n - 1)
    s = sum(values[:n])
    out.append(s / n)
    for i in range(n, len(values)):
        s += values[i] - values[i - n]
        out.append(s / n)
    return out


def _ema(values, n):
    k = 2 / (n + 1)
    out = []
    prev = None
    for i, v in enumerate(values):
        if prev is None:
            prev = v
        else:
            prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def _calc_indicators(bars):
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    n = len(bars)
    # MA
    ma = {f"MA{ p}": _sma(closes, p) for p in [5, 10, 20, 30, 60] if n >= p}
    # MACD
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    macd = [(a - b) * 2 for a, b in zip(dif, dea)]
    # RSI
    def rsi(values, period):
        out = [None] * period
        for i in range(period, len(values)):
            gains, losses = 0, 0
            for j in range(i - period + 1, i + 1):
                diff = values[j] - values[j - 1]
                if diff > 0:
                    gains += diff
                else:
                    losses -= diff
            avg_gain = gains / period
            avg_loss = losses / period
            if avg_loss == 0:
                out.append(100)
            else:
                rs = avg_gain / avg_loss
                out.append(100 - 100 / (1 + rs))
        return out
    rsi6 = rsi(closes, 6)
    rsi12 = rsi(closes, 12)
    rsi24 = rsi(closes, 24)
    # KDJ
    k_values, d_values, j_values = [None]*n, [None]*n, [None]*n
    prev_k, prev_d = 50.0, 50.0
    for i in range(n):
        if i < 8:
            k_values[i] = d_values[i] = j_values[i] = None
            continue
        period_low = min(lows[i-8:i+1])
        period_high = max(highs[i-8:i+1])
        rsv = 0 if period_high == period_low else (closes[i] - period_low) / (period_high - period_low) * 100
        k = prev_k * 2 / 3 + rsv / 3
        d = prev_d * 2 / 3 + k / 3
        j = 3 * k - 2 * d
        k_values[i], d_values[i], j_values[i] = k, d, j
        prev_k, prev_d = k, d
    # BOLL
    ma20 = _sma(closes, 20)
    std20 = []
    for i in range(n):
        if i < 19:
            std20.append(None)
        else:
            m = sum(closes[i-19:i+1]) / 20
            std20.append((sum((x - m) ** 2 for x in closes[i-19:i+1]) / 20) ** 0.5)
    boll_up = [m + 2 * s if m else None for m, s in zip(ma20, std20)]
    boll_down = [m - 2 * s if m else None for m, s in zip(ma20, std20)]

    return {
        "MA": ma,
        "MACD": {"DIF": dif, "DEA": dea, "MACD": macd},
        "RSI": {"RSI6": rsi6, "RSI12": rsi12, "RSI24": rsi24},
        "KDJ": {"K": k_values, "D": d_values, "J": j_values},
        "BOLL": {"UP": boll_up, "MID": ma20, "DOWN": boll_down},
    }


def _get_stock_info(code):
    """从 stock_cache.db 读取 A股基本面；全球市场从 global_daily.db 读取 name。"""
    info = {"code": code, "name": code}
    # 判断是否是指数
    is_index = code.startswith(("sh000", "sz399"))
    # A股
    if not code.startswith(("hk:", "us:", "crypto:", "fut:")):
        db = os.path.join(BASE_DIR, "scripts", "cli", "stock_cache.db")
        if os.path.exists(db):
            try:
                import sqlite3
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM stocks WHERE code=?", (code,))
                r = c.fetchone()
                if r:
                    raw = {k: r[k] for k in r.keys()}
                    # 中文化 + 单位转换
                    mapping = {
                        "name": "name",
                        "industry": "所属行业",
                        "mktcap": "总市值",
                        "tier": "市值分层",
                        "turnover": "换手率(%)",
                        "pe": "市盈率(PE)",
                        "pb": "市净率(PB)",
                    }
                    for k, label in mapping.items():
                        v = raw.get(k)
                        if v is None or v == "":
                            continue
                        if k == "mktcap" and v:
                            v = f"{v/1e8:.2f} 亿"
                        info[label] = v
                    info["_is_index"] = is_index
                    info["_industry"] = raw.get("industry") or ""
                conn.close()
            except Exception:
                pass
    # 全球市场 name
    if info.get("name") == code:
        try:
            u = _load_unified()
            name = dict(u.universe(code.split(":")[0])).get(code)
            if name:
                info["name"] = name
        except Exception:
            pass
    return info


def _get_related(code, limit=12):
    """获取相关标的：A股优先同板块，其它市场取同市场热门标的。"""
    try:
        market = code.split(":")[0] if ":" in code else "cn"
        u = _load_unified()
        # A股：找同板块
        if market == "cn" and not code.startswith(("sh000", "sz399")):
            info = _get_stock_info(code)
            industry = info.get("_industry", "")
            if industry:
                try:
                    import sqlite3
                    db = os.path.join(BASE_DIR, "scripts", "cli", "stock_cache.db")
                    conn = sqlite3.connect(db)
                    c = conn.cursor()
                    c.execute(
                        "SELECT code, name FROM stocks WHERE industry=? AND code != ? ORDER BY mktcap DESC LIMIT ?",
                        (industry, code, limit),
                    )
                    rows = c.fetchall()
                    conn.close()
                    if rows:
                        return [{"code": r[0], "name": r[1] or r[0]} for r in rows]
                except Exception:
                    pass
        # 默认：同市场 universe
        univ = u.universe(market)
        out = []
        for c, name in univ:
            if c == code:
                continue
            out.append({"code": c, "name": name or c})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _market_quotes(u, market, max_items=25):
    """读取某市场全部 universe，返回有数据的标的列表。"""
    try:
        universe = u.universe(market)
    except Exception:
        return []
    out = []
    for code, name in universe[:max_items * 3]:
        q = _quote_for_code(u, code, name)
        if q:
            out.append(q)
        if len(out) >= max_items:
            break
    return out


def get_market_data():
    global _quote_cache
    now = time.time()
    if now - _quote_cache["ts"] < QUOTE_CACHE_TTL and _quote_cache["data"]:
        return _quote_cache["data"]

    u = _load_unified()
    data = {}
    for market, items in WATCHLIST.items():
        out = []
        for code, name in items:
            q = _quote_for_code(u, code, name)
            if q:
                out.append(q)
        data[market] = out

    # 精选指数/龙头作为 headline
    headline_codes = [
        ("sh000001", "上证指数"),
        ("sz399001", "深证成指"),
        ("sz399006", "创业板指"),
        ("hk:00700", "腾讯控股"),
        ("us:AAPL", "Apple"),
        ("crypto:BTC/USDT", "BTC/USDT"),
        ("crypto:ETH/USDT", "ETH/USDT"),
        ("fut:IH0", "上证50期货"),
        ("fut:UR0", "尿素期货"),
    ]
    headlines = []
    for code, name in headline_codes:
        q = _quote_for_code(u, code, name)
        if q:
            headlines.append(q)

    result = {"headlines": headlines, "markets": data, "updated": datetime.now().isoformat()}
    _quote_cache = {"ts": now, "data": result}
    return result


# ============== Quant 状态数据 ==============


def _df():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return f"{fmt_size(used)} / {fmt_size(total)} ({fmt_pct(used/total)})"
    except Exception as e:
        return str(e)


def _mem():
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        vals = {}
        for line in lines:
            if ":" in line:
                k, v = line.split(":", 1)
                vals[k.strip()] = int(v.split()[0]) * 1024
        total = vals.get("MemTotal", 0)
        avail = vals.get("MemAvailable", 0)
        used = total - avail
        return f"{fmt_size(used)} / {fmt_size(total)} ({fmt_size(avail)} available)"
    except Exception as e:
        return str(e)


def machine_status():
    return {
        "主机": sh("hostname"),
        "内核": sh("uname -r"),
        "Tailscale IP": sh("tailscale ip -4 2>/dev/null || echo '未就绪'"),
        "Tailscale 域名": "quant.tail6a6cc8.ts.net",
        "公网地址": "https://quant.tail6a6cc8.ts.net/",
        "运行时间": sh("uptime -p 2>/dev/null || uptime"),
        "负载": sh("cat /proc/loadavg | cut -d' ' -f1-3"),
        "磁盘 /": _df(),
        "内存": _mem(),
        "CPU": f"{sh('nproc')} cores",
        "时区": sh("date +%Z (%z)"),
        "当前时间": sh("date '+%Y-%m-%d %H:%M:%S'"),
    }


def service_status():
    svcs = ["mihomo", "quant-status-web", "cron", "ssh", "tailscaled"]
    out = {}
    for s in svcs:
        out[s] = sh(f"systemctl is-active {s} 2>/dev/null || echo unknown")
    return out


def db_status():
    files = {
        "global_daily.db": os.path.join(BASE_DIR, "data", "global_daily.db"),
        "stock_cache.db": os.path.join(BASE_DIR, "scripts", "cli", "stock_cache.db"),
        "trading.db": os.path.join(BASE_DIR, "database", "trading.db"),
    }
    out = {}
    for name, path in files.items():
        if os.path.exists(path):
            out[name] = fmt_size(os.path.getsize(path))
        else:
            out[name] = "不存在"
    return out


def market_counts():
    try:
        sys.path.insert(0, BASE_DIR)
        from data import unified as u
        return u.counts()
    except Exception as e:
        return {"error": str(e)}


def market_account(market):
    path = os.path.join(STATE_DIR, f"multi-{market}.json")
    data = read_json(path, {})
    if not data:
        return None
    if isinstance(data, list):
        acct = data[-1].get("account", {}) if data else {}
    elif isinstance(data, dict):
        acct = data
    else:
        return None

    initial = acct.get("initial_capital", 10000.0)
    total = acct.get("total_asset") or acct.get("cash", initial)
    try:
        ret = (float(total) - float(initial)) / float(initial)
    except Exception:
        ret = 0.0

    lots = []
    positions = acct.get("positions") or acct.get("lots") or {}
    if isinstance(positions, dict):
        for code, arr in positions.items():
            if isinstance(arr, list):
                for lot in arr:
                    if isinstance(lot, dict):
                        lots.append(lot)
            elif isinstance(arr, dict):
                lots.append(arr)
    mtime = ""
    try:
        mtime = time.strftime("%m-%d %H:%M", time.localtime(
            os.path.getmtime(os.path.join(
                STATE_DIR, f"multi-{market}.json"))))
    except OSError:
        pass
    return {
        "initial": fmt_money(initial),
        "cash": fmt_money(acct.get("cash", 0)),
        "total": fmt_money(total),
        "return": fmt_pct(ret),
        "fees": fmt_money(acct.get("fees", 0)),
        "trade_count": acct.get("trade_count", 0),
        "lots": lots,
        "state_mtime": mtime,
    }


def stock_account():
    path = os.path.join(STATE_DIR, "stock_account.json")
    data = read_json(path, {})
    if not data:
        return None
    return {
        "cash": fmt_money(data.get("cash", 0)),
        "total_asset": fmt_money(data.get("total_asset", 0)),
        "positions": len(data.get("positions", {})),
        "trade_count": data.get("trade_count", 0),
        "date": data.get("date", "-"),
    }


def backtest_summary():
    mds = sorted(glob.glob(os.path.join(BACKTEST_DIR, "*.md")), key=os.path.getmtime, reverse=True)
    out = []
    for p in mds[:5]:
        try:
            with open(p, "r", encoding="utf-8") as f:
                content = f.read(2000)
            out.append({"file": os.path.basename(p), "content": content})
        except Exception:
            pass
    return out


def recent_logs(lines=30):
    out = {}
    for pattern in ["daily-status-*.log", "daily-stock-*.log"]:
        files = sorted(glob.glob(os.path.join(LOG_DIR, pattern)), key=os.path.getmtime, reverse=True)
        if files:
            try:
                with open(files[0], "r", encoding="utf-8", errors="ignore") as f:
                    out[os.path.basename(files[0])] = "\n".join(f.read().splitlines()[-lines:])
            except Exception as e:
                out[pattern] = str(e)
    return out


def cron_tasks():
    return sh("crontab -l 2>/dev/null || echo '(无 crontab)'")


def tailscale_info():
    return {
        "funnel": sh("tailscale funnel status 2>/dev/null | head -5"),
        "serve": sh("tailscale serve status 2>/dev/null | head -5"),
    }


# ============== 搜索 ==============

_search_cache = {"ts": 0, "data": []}
SEARCH_CACHE_TTL = 300


def get_all_universe():
    global _search_cache
    now = time.time()
    if now - _search_cache["ts"] < SEARCH_CACHE_TTL and _search_cache["data"]:
        return _search_cache["data"]
    u = _load_unified()
    items = []
    for market in ["cn", "hk", "us", "crypto", "fut"]:
        try:
            for code, name in u.universe(market):
                items.append({"code": code, "name": name or code, "market": market})
        except Exception:
            pass
    _search_cache = {"ts": now, "data": items}
    return items


def search_universe(q, limit=20):
    q = q.lower().strip()
    if not q:
        return []
    out = []
    for item in get_all_universe():
        if q in item["code"].lower() or q in item["name"].lower():
            out.append(item)
        if len(out) >= limit:
            break
    return out


# ============== K 线页面 ==============


def kline_page(code, period="day"):
    u = _load_unified()
    raw_bars = _get_bars(u, code, tail=4000 if period == "month" else (1200 if period == "week" else 250))
    name = _get_stock_info(code).get("name", code)
    if not raw_bars:
        return base_page("K线", f'<h1>⚠️ 无数据</h1><p>未找到 {code} 的行情数据。</p>', active="")

    bars = _resample_bars(raw_bars, period)
    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else bars[-1]
    change_pct = (last.close - prev.close) / prev.close if prev.close else 0
    cls = "up" if change_pct >= 0 else "down"
    sign = "+" if change_pct >= 0 else ""

    # K线 + 指标序列
    ind = _calc_indicators(bars)
    kdata = []
    for i, b in enumerate(bars):
        kd = {"date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "vol": b.vol}
        for g, vals in ind.items():
            if isinstance(vals, dict):
                for k, v in vals.items():
                    kd[f"{g}_{k}"] = v[i]
        kdata.append(kd)
    kdata_json = json.dumps(kdata)

    # 周期按钮
    periods = {"day": "日K", "week": "周K", "month": "月K"}
    period_btns = " ".join(
        f'<a class="tab {"active" if p == period else ""}" href="/kline?code={code}&period={p}">{label}</a>'
        for p, label in periods.items()
    )

    rows = []
    for b in bars[-10:][::-1]:
        c_cls = "up" if b.close >= b.open else "down"
        rows.append(
            f"<tr><td>{b.date}</td><td>{fmt_money(b.open)}</td><td>{fmt_money(b.high)}</td>"
            f"<td>{fmt_money(b.low)}</td><td class='{c_cls}'>{fmt_money(b.close)}</td>"
            f"<td>{fmt_vol(b.vol)}</td></tr>"
        )

    content = f"""
    <h1>📊 {name} <span style="color:var(--muted);font-size:16px">{code}</span></h1>
    <div class="meta">最新 {last.date} | 收盘 {fmt_money(last.close)} | 涨跌 <span class="{cls}">{sign}{fmt_pct(change_pct)}</span> | 成交量 {fmt_vol(last.vol)}</div>

    <div class="tabs">{period_btns} <a class="tab" href="/quote?code={code}&period={period}">📄 详情页</a></div>

    {kline_component(kdata_json)}

    <div class="card">
        <h2>最近 10 根 K 线</h2>
        <table>
            <tr><th>日期</th><th>开盘</th><th>最高</th><th>最低</th><th>收盘</th><th>成交量</th></tr>
            {''.join(rows)}
        </table>
    </div>
    """
    return base_page(f"{name} K线", content, active="")



# ============== HTML 模板 ==============


def nav(active=""):
    items = [
        ("/", "行情看板", "chart"),
        ("/quant", "Quant 状态", "server"),
        ("/projects", "项目入口", "folder"),
        ("/downloads", "下载中心", "download"),
        ("/docs", "开发文档", "docs"),
    ]
    links = []
    for href, label, icon in items:
        cls = "active" if href == active else ""
        links.append(f'<a href="{href}" class="{cls}">{label}</a>')
    return f"""
    <nav class="navbar">
        <div class="nav-brand">
            <img src="/web_assets/logo.png" alt="观潮量化"
                 style="width:30px;height:30px;border-radius:6px;object-fit:cover"/>
            <span>观潮量化 <span style="color:var(--muted);font-size:12px;letter-spacing:.5px">GUANCHAO QUANT</span></span>
        </div>
        <div class="nav-links">
            {"".join(links)}
            <form class="search-form" onsubmit="return doSearch(event)">
                <input type="text" id="searchInput" placeholder="搜索代码或名称..." autocomplete="off">
                <button type="submit">🔍</button>
                <div id="searchResults" class="search-results"></div>
            </form>
        </div>
    </nav>
    <script>
    let searchTimeout;
    const input = document.getElementById('searchInput');
    const results = document.getElementById('searchResults');
    input.addEventListener('input', function(){{
        clearTimeout(searchTimeout);
        const q = this.value.trim();
        if(q.length < 1){{ results.innerHTML=''; results.style.display='none'; return; }}
        searchTimeout = setTimeout(()=>fetchSearch(q), 200);
    }});
    document.addEventListener('click', function(e){{
        if(!e.target.closest('.search-form')){{ results.style.display='none'; }}
    }});
    function fetchSearch(q){{
        fetch('/api/search?q='+encodeURIComponent(q))
            .then(r=>r.json())
            .then(data=>{{
                if(!data.length){{ results.innerHTML=''; results.style.display='none'; return; }}
                results.innerHTML = data.map(x=>'<a href="/kline?code='+encodeURIComponent(x.code)+'" class="search-item"><b>'+x.name+'</b> <span>'+x.code+'</span></a>').join('');
                results.style.display='block';
            }});
    }}
    function doSearch(e){{
        e.preventDefault();
        const q = input.value.trim();
        if(q) window.location.href='/kline?code='+encodeURIComponent(q);
        return false;
    }}
    </script>
    """


def base_page(title, content, active=""):
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" href="/web_assets/favicon.png" type="image/png">
<title>观潮量化 · {title}</title>
<style>
:root {{ --bg: #0b0e11; --surface: #151a21; --surface-2: #1e2530; --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --accent-2: #238636; --up: #3fb950; --down: #f85149; --border: #30363d; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
.navbar {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 0 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }}
.nav-brand {{ display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 600; padding: 14px 0; }}
.logo {{ width: 30px; height: 30px; background: linear-gradient(135deg, var(--accent), #a371f7); border-radius: 6px; display: grid; place-items: center; color: #fff; font-weight: 700; }}
.nav-links {{ display: flex; gap: 8px; }}
.nav-links a {{ color: var(--muted); text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 14px; font-weight: 500; transition: .15s; }}
.nav-links a:hover, .nav-links a.active {{ background: var(--surface-2); color: var(--text); }}
.search-form {{ position: relative; display: flex; align-items: center; margin-left: 12px; }}
.search-form input {{ background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; width: 180px; font-size: 13px; outline: none; }}
.search-form input:focus {{ border-color: var(--accent); }}
.search-form button {{ background: var(--surface-2); border: 1px solid var(--border); color: var(--text); border-radius: 6px; padding: 6px 10px; margin-left: 6px; cursor: pointer; }}
.search-results {{ position: absolute; top: 36px; left: 0; right: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; max-height: 300px; overflow-y: auto; z-index: 200; display: none; box-shadow: 0 8px 24px rgba(0,0,0,0.4); }}
.search-item {{ display: block; padding: 8px 12px; color: var(--text); text-decoration: none; border-bottom: 1px solid var(--border); font-size: 13px; }}
.search-item:hover {{ background: var(--surface-2); }}
.search-item span {{ color: var(--muted); margin-left: 8px; font-size: 12px; }}
@media (max-width: 900px) {{ .search-form input {{ width: 120px; }} }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size: 24px; margin: 0 0 20px; }}
h2 {{ font-size: 18px; color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 10px; margin: 28px 0 16px; }}
h3 {{ font-size: 15px; margin: 18px 0 10px; color: var(--muted); }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 18px; overflow-x: auto; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
.headline {{ background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 14px; min-width: 180px; }}
.headline .name {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .5px; }}
.headline .code {{ font-size: 13px; color: var(--muted); margin-bottom: 6px; }}
.headline .price {{ font-size: 24px; font-weight: 700; font-family: "SF Mono", Monaco, monospace; }}
.headline .change {{ font-size: 14px; font-weight: 600; margin-top: 4px; font-family: "SF Mono", Monaco, monospace; }}
.up {{ color: var(--up); }} .down {{ color: var(--down); }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ border: 1px solid var(--border); padding: 8px 12px; text-align: left; }}
th {{ background: var(--surface-2); color: var(--accent); font-weight: 600; }}
tr:hover {{ background: rgba(88,166,255,0.05); }}
.sparkline {{ width: 90px; height: 28px; }}
.badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 12px; background: var(--surface-2); }}
.badge.ok {{ background: rgba(35,134,54,0.2); color: var(--up); }}
.btn {{ display: inline-block; background: var(--accent); color: #fff; padding: 6px 14px; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: 600; }}
.btn:hover {{ opacity: 0.9; }}
ul {{ line-height: 2; }}
.badge.warn {{ background: rgba(248,81,73,0.15); color: var(--down); }}
.tabs {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }}
.tab {{ background: var(--surface-2); border: 1px solid var(--border); color: var(--muted); padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; }}
.tab.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}
.project-card {{ background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 18px; transition: transform .15s; }}
.project-card:hover {{ transform: translateY(-2px); border-color: var(--accent); }}
.project-card h3 {{ margin: 0 0 8px; color: var(--text); }}
.project-card p {{ margin: 0; color: var(--muted); font-size: 13px; }}
.project-card a {{ display: inline-block; margin-top: 12px; color: var(--accent); text-decoration: none; font-size: 13px; font-weight: 600; }}
.meta {{ color: var(--muted); font-size: 12px; margin-bottom: 16px; }}
pre {{ background: var(--bg); padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; color: var(--muted); white-space: pre-wrap; word-break: break-all; }}
@media (max-width: 768px) {{ .navbar {{ flex-direction: column; align-items: flex-start; }} .nav-links {{ padding-bottom: 10px; }} }}
</style>
</head>
<body>
{nav(active)}
<div class="container">
{content}
</div>
<footer style="border-top:1px solid var(--border);margin-top:28px;padding:18px 24px;text-align:center;color:var(--muted);font-size:12px">
  <div style="display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:wrap">
    <img src="/web_assets/logo.png" alt="观潮量化"
         style="height:44px;border-radius:8px;display:inline-block"
         onerror="this.style.display='none'"/>
    <span style="font-size:14px;color:var(--text);font-weight:600">观潮量化</span>
    <span style="font-size:11px;color:var(--muted);letter-spacing:1px">GUANCHAO QUANT</span>
    <span style="opacity:.55">·</span>
    <span>Powered by 獨白量化</span>
    <span style="opacity:.55">·</span>
    <span><a href="https://github.com/Languangxun/ai-quant" style="color:var(--accent);text-decoration:none" target="_blank"># ai-quant</a></span>
  </div>
</footer>
</body>
</html>"""


def sparkline_js():
    return """
    <script>
    function drawSparklines(){
        document.querySelectorAll('canvas.sparkline').forEach(c=>{
            const data = JSON.parse(c.dataset.points || '[]');
            if(data.length<2) return;
            const ctx=c.getContext('2d');
            const w=c.width, h=c.height;
            const min=Math.min(...data), max=Math.max(...data);
            const range=max-min||1;
            const color = data[data.length-1]>=data[0] ? '#3fb950' : '#f85149';
            ctx.clearRect(0,0,w,h);
            ctx.strokeStyle=color; ctx.lineWidth=2; ctx.beginPath();
            data.forEach((v,i)=>{
                const x=i/(data.length-1)*w;
                const y=h-((v-min)/range*0.8+0.1)*h;
                if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
            });
            ctx.stroke();
        });
    }
    function switchTab(market){
        document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.market===market));
        document.querySelectorAll('.tab-content').forEach(c=>c.classList.toggle('active', c.id==='tab-'+market));
    }
    document.addEventListener('DOMContentLoaded', drawSparklines);
    </script>
    """


def index_page():
    data = get_market_data()
    headlines = data.get("headlines", [])
    markets = data.get("markets", {})

    # headline 卡片
    hl_cards = []
    for q in headlines:
        cls = "up" if q["change_pct"] >= 0 else "down"
        sign = "+" if q["change_pct"] >= 0 else ""
        points_json = json.dumps([p[1] for p in q["history"]])
        hl_cards.append(f"""
        <div class="headline" onclick="location.href='/quote?code={q['code']}'" style="cursor:pointer">
            <div class="name"><a href="/quote?code={q['code']}" style="color:inherit;text-decoration:none">{q['name']}</a></div>
            <div class="code">{q['code']}</div>
            <div class="price">{fmt_money(q['close'])}</div>
            <div class="change {cls}">{sign}{fmt_pct(q['change_pct'])} &nbsp; {sign}{fmt_money(q['change'])}</div>
            <canvas class="sparkline" width="160" height="40" data-points='{points_json}'></canvas>
        </div>
        """)

    # tabs
    tabs_html = []
    for m in ["cn", "hk", "us", "crypto", "fut"]:
        label = {"cn": "A股", "hk": "港股", "us": "美股", "crypto": "加密", "fut": "期货"}[m]
        active = "active" if m == "cn" else ""
        tabs_html.append(f'<button class="tab {active}" data-market="{m}" onclick="switchTab(\'{m}\')">{label}</button>')

    # market tables
    tables_html = []
    for m in ["cn", "hk", "us", "crypto", "fut"]:
        label = {"cn": "A股", "hk": "港股", "us": "美股", "crypto": "加密", "fut": "期货"}[m]
        active = "active" if m == "cn" else ""
        rows = []
        for q in markets.get(m, []):
            cls = "up" if q["change_pct"] >= 0 else "down"
            sign = "+" if q["change_pct"] >= 0 else ""
            points_json = json.dumps([p[1] for p in q["history"]])
            rows.append(f"""
            <tr onclick="location.href='/quote?code={q['code']}'" style="cursor:pointer">
                <td><a href="/quote?code={q['code']}" style="color:var(--text);text-decoration:none"><strong>{q['name']}</strong></a><br/><span style="color:var(--muted);font-size:12px">{q['code']}</span></td>
                <td>{q['date']}</td>
                <td>{fmt_money(q['close'])}</td>
                <td class="{cls}">{sign}{fmt_pct(q['change_pct'])}</td>
                <td>{q['vol']:,.2f}</td>
                <td><canvas class="sparkline" width="90" height="28" data-points='{points_json}'></canvas></td>
            </tr>
            """)
        tables_html.append(f"""
        <div id="tab-{m}" class="tab-content {active}">
            <h3>{label}市场 ({len(rows)} 只)</h3>
            <table>
                <tr><th>标的</th><th>日期</th><th>收盘价</th><th>涨跌</th><th>成交量</th><th>走势</th></tr>
                {''.join(rows) if rows else '<tr><td colspan="6" style="text-align:center;color:var(--muted)">暂无数据</td></tr>'}
            </table>
        </div>
        """)

    content = f"""
    <h1>📈 行情看板</h1>
    <div class="meta">更新时间：{data.get('updated','-')} | 数据来自本地 SQLite 日频库</div>

    <div class="grid">
        {''.join(hl_cards)}
    </div>

    <h2>市场行情</h2>
    <div class="tabs">{''.join(tabs_html)}</div>
    {''.join(tables_html)}

    {sparkline_js()}
    """
    return base_page("行情看板", content, active="/")


# ============== Pi 节点精华（OrangePi 状态聚合，网站 /quant 嵌入） ==============

PI_STATUS_URL = (os.environ.get("PI_STATUS_URL")
                 or "http://100.117.242.5:8188/pi_status.json")
_pi_cache = {"ts": 0.0, "data": None}


def pi_status_snapshot(refresh=False):
    """拉取 Pi 节点精华 JSON（60 秒 TTL）；失败返回 error dict。"""
    now = time.time()
    if not refresh and _pi_cache["data"] and now - _pi_cache["ts"] < 60:
        return _pi_cache["data"]
    try:
        import urllib.request as _u
        req = _u.Request(PI_STATUS_URL, headers={"User-Agent": "quant-web"})
        with _u.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        _pi_cache["data"], _pi_cache["ts"] = data, now
        return data
    except Exception as e:
        data = {"unreachable": True, "error": str(e)[:120],
                "url": PI_STATUS_URL}
        _pi_cache["data"], _pi_cache["ts"] = data, now
        return data


PI_HOST_TITLE = "Pi OrangepiZero2W 节点"


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pi_status_html(snap=None):
    """渲染 Pi 精华为 HTML 卡片（嵌入网站 /quant 页）。"""
    s = snap if snap is not None else pi_status_snapshot()
    if s.get("unreachable"):
        return ("<div class='card'><h2>" + PI_HOST_TITLE
                + " <span class='meta'>状态退役</span></h2>"
                + "<p>nu Pi 已关机退役（2026-11-08），状态卡停更。</p>"
                + "<div class='meta'>url: " + _esc(s.get("url", "")) + "</div></div>")
    m = s.get("machine") or {}
    mrows = "".join("<tr><td><strong>" + _esc(k) + "</strong></td><td>"
                    + _esc(v) + "</td></tr>" for k, v in m.items()
                    if k not in ("Tailscale IP", "公网地址"))
    srv = s.get("services") or {}
    badges = " ".join(
        "<span class='badge " + ("ok" if v == "active" else "warn")
        + "'>" + _esc(k) + ": " + _esc(v) + "</span>" for k, v in srv.items())
    ac_html = ""
    for name in ("fund", "hk", "us", "crypto", "fut"):
        a = s.get("accounts", {}).get(name) or {}
        if not isinstance(a, dict) or not (a.get("总资产") or a.get("交易数(累计)")):
            ac_html += ("<div class='card'><h3>" + name.upper()
                        + " 模拟盘</h3><p>无状态</p></div>")
            continue
        ret = a.get("收益")
        ret_txt = ("%+.2f%%" % (ret * 100)) if isinstance(ret, (int, float)) else "-"
        body = ("<p><strong>总资产:</strong> " + _esc(a.get("总资产", "-"))
                + " | <strong>现金:</strong> " + _esc(a.get("现金", "-"))
                + " | <strong>收益:</strong> " + ret_txt
                + " | <strong>交易(累计):</strong> " + _esc(a.get("交易数(累计)", 0))
                + "</p>")
        rows = "".join(
            "<tr><td>" + _esc(l.get("code")) + "</td><td>"
            + _esc(l.get("shares")) + "</td><td>" + _esc(l.get("cost"))
            + "</td><td>" + _esc(l.get("pnl_pct")) + "</td></tr>"
            for l in (a.get("持仓") or [])[:4])
        if rows:
            body += ("<table style='font-size:12px'><tr><th>代码</th>"
                     "<th>份额</th><th>成本</th><th>盈亏%</th></tr>"
                     + rows + "</table>")
        ac_html += ("<div class='card'><h3>" + name.upper()
                    + " 模拟盘</h3>" + body + "</div>")
    counts = ((s.get("market_counts") or {}).get("detail") or {})
    crows = "".join(
        "<tr><td>" + _esc(k) + "</td><td>" + _esc(v.get("rows"))
        + "</td><td>" + _esc(v.get("codes")) + "</td><td>"
        + _esc(v.get("max_date")) + "</td></tr>" for k, v in counts.items())
    c_html = (("<table><tr><th>市场</th><th>行数</th><th>代码</th><th>最新日"
               "</th></tr>" + crows + "</table>") if crows else "<p>空</p>")
    drows = "".join("<tr><td><strong>" + _esc(k) + "</strong></td><td>"
                    + _esc(v) + "</td></tr>"
                    for k, v in (s.get("dbs") or {}).items())
    bhtml = "".join(
        "<h4>" + _esc(x.get("file")) + "</h4><pre>"
        + _esc(x.get("head", "")) + "</pre>"
        for x in (s.get("backtest") or []))
    lhtml = "".join(
        "<h4>" + _esc(k) + "</h4><pre>" + _esc(v) + "</pre>"
        for k, v in (s.get("logs") or {}).items())
    return ("<div class='card'><h2>🍓 " + PI_HOST_TITLE
            + " <span class='meta'>数据生成 " + _esc(s.get("generated"))
            + "</span></h2><p>" + badges + "</p><table>" + mrows
            + "</table></div>"
            + "<h2>Pi 多市场模拟盘</h2><div class='grid'>" + ac_html + "</div>"
            + "<div class='card'><h2>Pi 数据范围</h2>" + c_html + "</div>"
            + "<div class='card'><h2>Pi 库文件</h2><table>" + drows
            + "</table></div>"
            + "<div class='card'><h2>Pi 回测摘要</h2>" + bhtml + "</div>"
            + "<div class='card'><h2>Pi 最近日志</h2>" + lhtml + "</div>")


def quant_page():
    machine = machine_status()
    services = service_status()
    dbs = db_status()
    counts = market_counts()
    stock = stock_account()
    backtests = backtest_summary()
    logs = recent_logs(40)
    crontab = cron_tasks()
    ts = tailscale_info()

    service_badge = " ".join(
        f"<span class='badge {'ok' if v == 'active' else 'warn'}'>{k}: {v}</span>"
        for k, v in services.items()
    )

    counts_rows = []
    if isinstance(counts, dict) and "error" not in counts:
        for m, v in counts.items():
            counts_rows.append(
                f"<tr><td>{m.upper()}</td><td>{v.get('codes','-')}</td>"
                f"<td>{v.get('rows','-')}</td><td>{v.get('min','-')}</td><td>{v.get('max','-')}</td></tr>"
            )
        counts_html = "<table><tr><th>市场</th><th>标的数</th><th>记录数</th><th>最早</th><th>最晚</th></tr>" + "".join(counts_rows) + "</table>"
    else:
        counts_html = f"<p>读取失败: {counts.get('error','unknown')}</p>"

    market_cards = []
    for m in MARKETS:
        info = market_account(m)
        if not info:
            market_cards.append(f'<div class="card"><h3>{m.upper()}</h3><p>无状态文件</p></div>')
            continue
        lots_rows = []
        for lot in info.get("lots", []):
            if not isinstance(lot, dict):
                continue
            lots_rows.append(
                f"<tr><td>{lot.get('code','')}</td><td>{lot.get('shares','')}</td>"
                f"<td>{fmt_money(lot.get('cost',''))}</td><td>{fmt_money(lot.get('price','-'))}</td>"
                f"<td>{lot.get('buy_date','')}</td><td>{lot.get('reason','')[:50]}</td></tr>"
            )
        lots_html = ("<table style='font-size:12px'><tr><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>买入日</th><th>理由</th></tr>" +
                     "".join(lots_rows) + "</table>") if lots_rows else "<p>当前无持仓</p>"
        market_cards.append(f"""
        <div class="card">
            <h3>{m.upper()} 模拟盘<span class="meta" style="font-size:11px;color:var(--muted)"> 状态文件更新 {info.get('state_mtime','-')}（累计口径）</span></h3>
            <p><strong>初始:</strong> {info['initial']} &nbsp;|&nbsp; <strong>总资产:</strong> {info['total']} &nbsp;|&nbsp;
               <strong>收益:</strong> {info['return']} &nbsp;|&nbsp; <strong>现金:</strong> {info['cash']} &nbsp;|&nbsp;
               <strong>成交(累计):</strong> {info['trade_count']}</p>
            {lots_html}
        </div>
        """)

    stock = dict(stock) if stock else {}
    try:
        stock["状态文件更新"] = time.strftime("%m-%d %H:%M", time.localtime(
            os.path.getmtime(os.path.join(STATE_DIR, "stock_account.json"))))
        stock["成交(累计)"] = stock.pop("trade_count", stock.get("成交(累计)", "-"))
    except OSError:
        pass
    stock_html = "<p>无状态文件</p>" if not stock else (
        "".join(f"<tr><td><strong>{k}</strong></td><td>{v}</td></tr>" for k, v in stock.items())
    )
    if stock:
        stock_html = "<table>" + stock_html + "</table>"

    backtest_html = ""
    for bt in backtests:
        escaped = bt["content"].replace("<", "&lt;").replace(">", "&gt;")
        backtest_html += f"<h4>{bt['file']}</h4><pre>{escaped}</pre>"

    log_html = ""
    for name, content in logs.items():
        escaped = content.replace("<", "&lt;").replace(">", "&gt;")
        log_html += f"<h4>{name}</h4><pre>{escaped}</pre>"

    cron_html = f"<pre>{crontab.replace('<','&lt;').replace('>','&gt;')}</pre>"
    ts_html = "".join(f"<tr><td><strong>{k}</strong></td><td><pre>{v}</pre></td></tr>" for k, v in ts.items())
    ts_html = "<table>" + ts_html + "</table>"

    machine_rows = "".join(f"<tr><td><strong>{k}</strong></td><td>{str(v).replace(chr(10),'<br/>')}</td></tr>" for k, v in machine.items())
    db_rows = "".join(f"<tr><td><strong>{k}</strong></td><td>{v}</td></tr>" for k, v in dbs.items())

    content = f"""
    {pi_status_html(pi_status_snapshot())}
    <h1>🖥️ Quant 状态</h1>
    <div class="meta">{HOST_LABEL} | 公网: <a href="https://quant.tail6a6cc8.ts.net/">https://quant.tail6a6cc8.ts.net/</a></div>

    <div class="card"><h2>服务状态</h2><p>{service_badge}</p></div>

    <div class="grid">
        <div class="card"><h2>系统状态</h2><table>{machine_rows}</table></div>
        <div class="card"><h2>数据库 / 存储</h2><table>{db_rows}</table></div>
    </div>

    <div class="card"><h2>市场数据范围</h2>{counts_html}</div>
    <div class="card"><h2>A股模拟盘</h2>{stock_html}</div>

    <h2>基金/港/美/加密/期货模拟盘</h2>
    {''.join(market_cards)}

    <div class="card"><h2>股票回测摘要</h2>{backtest_html or '<p>暂无回测摘要</p>'}</div>
    <div class="card"><h2>最近日志</h2>{log_html or '<p>暂无日志</p>'}</div>
    <div class="card"><h2>Cron 任务</h2>{cron_html}</div>
    <div class="card"><h2>Tailscale 穿透</h2>{ts_html}</div>
    """
    return base_page("状态", content, active="/quant")


def projects_page():
    projects = [
        {
            "name": "ai-quant",
            "desc": "主量化系统：A股/基金/港股/美股/加密/期货多市场 sm701 模拟盘、CLI 选股、事件驱动决策、回测框架。",
            "links": [
                ("/quant", "Quant 状态页"),
                ("/", "行情看板"),
            ],
        },
        {
            "name": "stock_predict / 桌面预测",
            "desc": "桌面端个股预测与分析模块。服务端已接入 OpenCode Go API (claude-haiku-5-5)，搜索fallback到Google News RSS，确保无DeepSeek官方Key也能跑。",
            "links": [
                ("/quant", "查看 API/搜索状态"),
            ],
        },
        {
            "name": "股票模拟盘 (sim.run)",
            "desc": "A股实盘风格模拟盘：CLI 缓存选股 → LLM 组合决策 → A股规则成交 → 复盘/RAG记忆。",
            "links": [
                ("#", "每日 9:31 / 14:45 自动运行"),
            ],
        },
        {
            "name": "全球市场模拟盘 (sim.multi_run)",
            "desc": "fund/hk/us/crypto/fut 五市场日频 sm701 模拟盘，每日早 8 点自动跑盘并邮件报告。",
            "links": [
                ("/quant", "查看账户状态"),
            ],
        },
        {
            "name": "回测结果 (backtest/results)",
            "desc": "股票策略历史回测报告，包含年度收益、夏普、最大回撤等指标。",
            "links": [
                ("/quant", "查看回测摘要"),
            ],
        },
        {
            "name": "下载中心",
            "desc": "为后续开发者打包提供：全球/A股数据库、数据清洗/回填工具、指标因子代码、配置文件，支持断点续传下载。",
            "links": [
                ("/downloads", "进入下载中心"),
            ],
        },
        {
            "name": "邮件状态报告",
            "desc": "每日 8:00 / 21:00 向 2180287399@qq.com 与 19526719996@163.com 发送机器与市场状态邮件。",
            "links": [],
        },
        {
            "name": "Tailscale 远程",
            "desc": "100.126.30.51 / quant.tail6a6cc8.ts.net；公网穿透 https://quant.tail6a6cc8.ts.net/。",
            "links": [
                ("https://login.tailscale.com", "Tailscale 控制台"),
            ],
        },
    ]

    projects.insert(0, {
        "name": "行情源 / 开发者接入",
        "desc": "行情 REST API（/api/markets、/api/quote、/api/kline、/api/indicators、/api/search）与本地数据库接入文档，支持 Python/JS/GSAP 等任意语言对接。",
        "links": [
            ("/docs", "查看接入文档"),
            ("/download?file=docs/API.md", "下载 API.md"),
        ],
    })

    cards = []
    for p in projects:
        links = " ".join(f'<a href="{href}">{label} →</a>' for href, label in p["links"])
        cards.append(f"""
        <div class="project-card">
            <h3>{p['name']}</h3>
            <p>{p['desc']}</p>
            {links}
        </div>
        """)

    content = f"""
    <h1>📁 项目入口</h1>
    <div class="meta">ai-quant 量化系统各模块入口</div>
    <div class="grid">
        {''.join(cards)}
    </div>
    """
    return base_page("项目入口", content, active="/projects")


# ============== 开发者接入文档 ==============

_BASE_URL = "https://quant.tail6a6cc8.ts.net"

_DOCS_INTRO = """
<h2>1. 总览</h2>
<p>本站行情源（Quant Market Data）覆盖 <strong>A股 / 港股 / 美股 / 加密货币 / 国内期货</strong> 五个市场的日频行情，
底层为本地 SQLite 联邦库（<code>data/global_daily.db</code> + A股 <code>stock_cache.db</code>），对外提供两种接入方式：</p>
<table>
  <tr><th>方式</th><th>适用场景</th><th>入口</th></tr>
  <tr><td>REST API（推荐）</td><td>任意语言 / 远程 / 脚本轮询</td><td>本站 <code>/api/*</code> 接口</td></tr>
  <tr><td>本地数据库直连</td><td>与 quant 机同机 / 挂载同一磁盘的 Python 程序</td><td><code>data/unified.py</code> 统一读取层</td></tr>
</table>
<p>Base URL：</p>
<pre>https://quant.tail6a6cc8.ts.net          # Tailscale 域名（账号内免鉴权直达）
http://100.126.30.51:8080                # 纯内网 IP 直连（同 tailnet 内更快）</pre>
<p>所有响应为 UTF-8 JSON；站点仅对 Tailscale 网络/账号可见，暂不开放公网注册。</p>
"""

_DOCS_QUICK = """
<h2>2. 快速开始（30 秒）</h2>
<h3>curl</h3>
<pre>curl "BASE_URL/api/quote?code=sh600519"
curl "BASE_URL/api/kline?code=us:AAPL&amp;period=week&amp;tail=52"
curl "BASE_URL/api/indicators?code=crypto:BTC/USDT&amp;period=day&amp;tail=300"</pre>
<h3>Python（requests）</h3>
<pre>import requests

BASE = "BASE_URL"
# 1) K 线 + 指标
d = requests.get(BASE + "/api/indicators",
                 params={"code": "sh600519", "period": "day", "tail": 300}, timeout=10).json()
bars = d["bars"]                       # [{date, open, high, low, close, vol, MA_MA5, ..., BOLL_UP, ...}]
ma5 = d["indicators"]["MA"]["MA5"]     # 与 bars 一一对齐的序列，None=窗口不足
macd = d["indicators"]["MACD"]         # {DIF, DEA, MACD}

# 2) 全市场概览（含看板缓存 60s）
m = requests.get(BASE + "/api/markets", timeout=10).json()
btc = next(x for x in m["markets"]["crypto"] if x["code"] == "crypto:BTC/USDT")
print(btc["close"], btc["change_pct"])</pre>
<h3>JavaScript / Node（>=18 自带 fetch）</h3>
<pre>const r = await fetch("BASE_URL/api/kline?code=sh600519&amp;period=day");
const d = await r.json();
d.bars.forEach(b =&gt; console.log(b.date, b.close));</pre>
"""

_DOCS_ENDPOINTS = """
<h2>3. REST 接口明细</h2>
<table>
  <tr><th>接口</th><th>参数</th><th>说明</th></tr>
  <tr><td><code>GET /api/quote?code=&lt;code&gt;</code></td>
      <td><code>code</code>（必）</td>
      <td>单标概览：最新价/涨跌/量 + 最近 30 个收盘点（<code>history</code>，迷你走势图用）。返回 <code>{code, data:{...}}</code>。</td></tr>
  <tr><td><code>GET /api/kline?code=&lt;code&gt;&amp;period=day&amp;tail=250</code></td>
      <td><code>code</code>；<code>period=day|week|month</code>；<code>tail=30~800</code> 根数</td>
      <td>纯 K 线 <code>bars[]:{date, open, high, low, close, vol}</code>。周/月K为日K重采样，<code>date</code> 取周期内最近成交日。</td></tr>
  <tr><td><code>GET /api/indicators?code=&lt;code&gt;&amp;period=day&amp;tail=300</code></td>
      <td>同上</td>
      <td>K线+指标：每根 bar 附 <code>MA_MA5..</code> / <code>MACD_*</code> / <code>RSI_*</code> / <code>KDJ_*</code> / <code>BOLL_*</code> 字段；
          另附 <code>indicators</code> 分组序列（与 bars 对齐，窗口不足为 <code>null</code>）。本站 K 线图同款数据。</td></tr>
  <tr><td><code>GET /api/markets</code></td>
      <td>无</td>
      <td>看板汇总：5 市场观察名单（各 8~10 只）+ headline 热门标的。服务端缓存 60 秒。</td></tr>
  <tr><td><code>GET /api/search?q=&lt;关键字&gt;</code></td>
      <td><code>q</code>（代码或中文名，模糊）</td>
      <td>全市场代码搜索 → <code>[{code, name, market}]</code>。</td></tr>
  <tr><td><code>GET /health</code></td>
      <td>无</td>
      <td>健康检查 <code>{"status":"ok"}</code>。</td></tr>
</table>
<h3>quote / markets 返回字段</h3>
<pre>code        标的编码（编码规则见第 4 节）
date        最近交易日 YYYY-MM-DD
open/high/low/close   OHLC（float）
prev        前一交易日收盘
change      涨跌额 = close - prev
change_pct  涨跌幅（小数，0.0123 = 1.23%）
vol         成交量（各市场原始口径；程序员可转换为亿/万）
history     [(date, close), ...] 最近 30 个交易日收盘</pre>
"""

_DOCS_CODES = """
<h2>4. 标的编码规则（code 参数）</h2>
<table>
  <tr><th>市场</th><th>格式</th><th>示例</th></tr>
  <tr><td>A股个股</td><td><code>sh|sz + 6位代码</code>（交易所前缀）</td><td><code>sh600519</code> 贵州茅台</td></tr>
  <tr><td>A股指数</td><td>同上，<code>000/399</code> 开头自动识别</td><td><code>sh000001</code> 上证指数、<code>sz399006</code> 创业板指</td></tr>
  <tr><td>港股</td><td><code>hk:5位补零</code></td><td><code>hk:00700</code> 腾讯控股</td></tr>
  <tr><td>美股</td><td><code>us:大写Ticker</code></td><td><code>us:AAPL</code>、<code>us:TSLA</code></td></tr>
  <tr><td>加密货币</td><td><code>crypto:BASE/QUOTE</code></td><td><code>crypto:BTC/USDT</code></td></tr>
  <tr><td>国内期货</td><td><code>fut:品种+0</code>（主力连续 82 品种，sm701 fut_daily 同源）</td><td><code>fut:IH0</code> 上证50、<code>fut:UR0</code> 尿素</td></tr>
</table>
<p>不确定编码？先查 <code>/api/search?q=茅台</code> → 返回 <code>sh600519</code>。</p>
"""

_DOCS_DB = """
<h2>5. 本地数据库直连（Python，同机/挂载盘，推荐大数据量离线回测）</h2>
<p>在 quant 机上（或挂载了 <code>/media/usb/ashare</code> 的环境）开发时，无需走 HTTP，直接用统一读取层
<code>data/unified.py</code>：五库联邦、市场即参数，与 REST 口径完全一致。</p>
<pre>import sys; sys.path.insert(0, "/home/lan/ai-quant")
from data import unified as u

u.paths()                                 # 查看命中的库文件
bars = u.get_bars("sh600519", tail=300)   # [Bar(code,date,open,high,low,close,vol)]
bars = u.get_bars("fut:RB0", tail=250)
for m in ["cn", "hk", "us", "crypto", "fut"]:
    print(len(u.universe(m)))             # [(code, name), ...]
print(u.counts())                         # 各市场标的数/记录数/时间范围</pre>
<h3>库结构</h3>
<pre>A股库   scripts/cli/stock_cache.db（约 2.2G，1272 万根日K）
  daily_bars   日K（code, date, open, high, low, close, ...）
  stocks       代码表 / 行业 / 市值分层
  f10_cache    基本面
  minute_cache 分钟线
全球库 data/global_daily.db（约 1.3G）
  g.stocks     code, name, market(HK/US/CRYPTO/FUT)
  g.daily_bars market, code, date, open, high, low, close, vol</pre>
<p>完整库文件可在 <a href="/downloads">下载中心</a> 打包带走；本文档源文件：<a href="/download?file=docs/API.md">docs/API.md</a>。</p>
"""

_DOCS_NOTES = """
<h2>6. 注意事项</h2>
<ul style="line-height:2">
  <li><b>频率</b>：本站读本地库，无强制限频；<code>/api/markets</code> 有 60 秒缓存，请勿高频刷新全市场。</li>
  <li><b>数据口径</b>：日频（收盘后落库，由 cron 每交易日更新）。加密 7×24 交易含周末，A股/港美股各交易日历对齐。</li>
  <li><b>新标的历史不足</b>（如缺少 MA60）：指标返回 <code>null</code> 属正常，按位置对齐 bars 消费即可。</li>
  <li><b>错误处理</b>：无数据返回 <code>{"code":"...","error":"no data"}</code>；HTTP 500 说明服务端异常，先看 <a href="/quant">状态页</a>。</li>
  <li><b>周期扩展</b>：如需 MA90/MA120 等额外窗口，请用 <code>/api/indicators</code> 拿原始 K 线后自算，指标实现同源 <code>factors/technical.py</code>。</li>
</ul>
"""

_DOCS_CONTACT = """
<h2>7. 联系 / 反馈</h2>
<p>本项目由 <strong>獨白</strong> 维护。接入过程中遇到接口缺失、编码兼容等问题，欢迎直接提需求：</p>
<ul>
  <li>Telegram：<a href="https://t.me/Monologue_101" target="_blank">@Monologue_101</a></li>
  <li>QQ：2180287399</li>
  <li>文档源：<a href="/download?file=docs/API.md">docs/API.md</a>（Markdown，可离线阅读）</li>
</ul>
"""


def docs_page():
    top = ('<h1>🔌 行情源接入文档（开发者）</h1>'
           '<div class="meta">面向程序/脚本/其它系统的只读行情接入 · '
           + datetime.now().strftime("%Y-%m-%d %H:%M")
           + ' 更新 · 本页地址 <code>/docs</code> · '
             '<a class="btn" href="/download?file=docs/API.md">⬇ 下载 API.md</a></div>'
           '<style>code{font-family:monospace;background:var(--surface-2);padding:1px 6px;'
           'border-radius:3px;font-size:12px}</style>')
    content = top + "".join(
        s.replace("BASE_URL", _BASE_URL) for s in (
            _DOCS_INTRO, _DOCS_QUICK, _DOCS_ENDPOINTS, _DOCS_CODES, _DOCS_DB, _DOCS_NOTES, _DOCS_CONTACT)
    )
    return base_page("行情源接入文档", content, active="/docs")



# ============== HTTP 服务 ==============

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html):
        data = html.encode("utf-8")
        encoding = None
        if len(data) > 8192 and "gzip" in self.headers.get("Accept-Encoding", ""):
            import gzip as _gzip
            data = _gzip.compress(data, 6)
            encoding = "gzip"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/" or path == "/index.html":
                self._html(index_page())
            elif path == "/quant":
                self._html(quant_page())
            elif path == "/projects":
                self._html(projects_page())
            elif path == "/api/markets":
                self._json(get_market_data())
            elif path == "/api/quote":
                qs = parse_qs(parsed.query)
                code = qs.get("code", [""])[0]
                u = _load_unified()
                q = _quote_for_code(u, code)
                self._json({"code": code, "data": q})
            elif path == "/api/search":
                qs = parse_qs(parsed.query)
                q = qs.get("q", [""])[0]
                self._json(search_universe(q))
            elif path == "/api/kline":
                qs = parse_qs(parsed.query)
                d = _kline_payload(qs.get("code", [""])[0], qs.get("period", ["day"])[0],
                                   qs.get("tail", ["250"])[0])
                self._json(d if d else {"code": qs.get("code", [""])[0], "error": "no data"})
            elif path == "/api/indicators":
                qs = parse_qs(parsed.query)
                d = _kline_payload(qs.get("code", [""])[0], qs.get("period", ["day"])[0],
                                   qs.get("tail", ["250"])[0], with_indicators=True)
                self._json(d if d else {"code": qs.get("code", [""])[0], "error": "no data"})
            elif path == "/kline":
                qs = parse_qs(parsed.query)
                code = qs.get("code", [""])[0]
                period = qs.get("period", ["day"])[0]
                if period not in ("day", "week", "month"):
                    period = "day"
                self._html(kline_page(code, period))
            elif path == "/quote":
                qs = parse_qs(parsed.query)
                code = qs.get("code", [""])[0]
                period = qs.get("period", ["day"])[0]
                if period not in ("day", "week", "month"):
                    period = "day"
                self._html(quote_page(code, period))
            elif path == "/docs":
                self._html(docs_page())
            elif path == "/downloads":
                self._html(download_page())
            elif path == "/download":
                qs = parse_qs(parsed.query)
                rel = qs.get("file", [""])[0]
                full = resolve_download_path(rel)
                if full:
                    send_file_with_range(self, full)
                else:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"forbidden or not found")
            elif path in ("/web_assets/logo.png", "/web_assets/favicon.png"):
                logo = os.path.join(BASE_DIR, "scripts", "web_assets",
                                    os.path.basename(path))
                if os.path.exists(logo):
                    send_file_with_range(self, logo)
                else:
                    self.send_response(404)
                    self.end_headers()
            elif path == "/health":
                self._json({"status": "ok"})
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Server error: {e}".encode("utf-8"))

    def log_message(self, fmt, *args):
        pass


def serve(addr="0.0.0.0", port=8080):
    httpd = HTTPServer((addr, port), Handler)
    print(f"Serving quant dashboard at http://{addr}:{port}")
    httpd.serve_forever()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve()
    else:
        print(index_page())


if __name__ == "__main__":
    main()
