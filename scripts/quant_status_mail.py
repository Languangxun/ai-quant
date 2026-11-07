#!/usr/bin/env python3
"""quant-status-mailer: 在 quant 机本地运行，每日推送丰富状态邮件。"""
import os
import sys

# 让 cron/systemd 也能读到密钥/代理
_ENV_FILE = os.path.expanduser("~/.config/ai-quant/env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            if _k not in os.environ:
                os.environ[_k] = _v.strip('"\'')

import json
import glob
import subprocess
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ================= 配置 =================
SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465
SMTP_USER = os.environ.get("SMTP_USER", "languangxunlh@163.com")
# 授权码改为环境变量读取（~/.config/ai-quant/env 或 .env）；不再入库
SMTP_PASS = (os.environ.get("SMTP_PASS") or "").strip()
TO_ADDRS = [x.strip() for x in os.environ.get("SMTP_TO",
            "2180287399@qq.com,19526719996@163.com").split(",") if x.strip()]

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(BASE_DIR, "sim", "state")
BACKTEST_DIR = os.path.join(BASE_DIR, "backtest", "results")
HOST_LABEL = "quant(100.126.30.51 / 192.168.3.94)"
MARKETS = ["fund", "hk", "us", "crypto", "fut"]


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


# ============== 数据收集 ==============


def machine_status():
    return {
        "主机 / 内核": sh("hostname; uname -r"),
        "Tailscale IP": sh("tailscale ip -4 2>/dev/null || echo '未就绪'"),
        "公网地址": "https://quant.tail6a6cc8.ts.net/",
        "运行时间 / 负载": sh("uptime"),
        "磁盘用量": sh("df -h / | tail -1"),
        "内存用量": sh("free -h | head -2"),
        "Mihomo 代理": sh("ss -tlnp 2>/dev/null | grep -q ':7890' && echo '127.0.0.1:7890 监听' || echo '7890 未监听'"),
        "Venv 依赖": sh(f"cd {BASE_DIR} && .venv/bin/python -c \"import yaml,sqlalchemy,openai,numpy,pydantic; print('deps ok')\" 2>&1 | tail -1"),
    }


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


def market_state(market):
    path = os.path.join(STATE_DIR, f"multi-{market}.json")
    data = read_json(path)
    if not data:
        return {"状态": "无状态文件", "初始": "-", "总资产": "-", "收益": "-",
                "持仓": "-", "信号数": "-", "成交": "-"}
    if isinstance(data, list):
        last = data[-1] if data else {}
    elif isinstance(data, dict) and "account" in data:
        last = data
    elif isinstance(data, dict):
        last = {"account": data, "date": data.get("date", "-"),
                "signals": 0, "trade_count": data.get("trade_count", 0)}
    else:
        last = {}
    acct = last.get("account", {})
    initial = acct.get("initial_capital", 10000.0)
    total = acct.get("total_asset") or acct.get("cash", initial)
    try:
        ret = (float(total) - float(initial)) / float(initial)
    except Exception:
        ret = 0.0
    positions = acct.get("positions") or acct.get("lots") or {}
    pos_count = 0
    if isinstance(positions, dict):
        for v in positions.values():
            if isinstance(v, list):
                pos_count += len(v)
            else:
                pos_count += 1
    return {
        "状态": "已运行",
        "初始": fmt_money(initial),
        "总资产": fmt_money(total),
        "收益": fmt_pct(ret),
        "持仓": pos_count,
        "成交": last.get("trade_count", acct.get("trade_count", "-")),
    }


def stock_account():
    path = os.path.join(STATE_DIR, "stock_account.json")
    data = read_json(path)
    if not data:
        return {"状态": "无状态文件", "总资产": "-", "现金": "-", "持仓": "-", "成交": "-"}
    return {
        "状态": "已运行",
        "总资产": fmt_money(data.get("total_asset", 0)),
        "现金": fmt_money(data.get("cash", 0)),
        "持仓": len(data.get("positions", {})),
        "成交": data.get("trade_count", 0),
        "日期": data.get("date", "-"),
    }


def backtest_summary_text():
    mds = sorted(glob.glob(os.path.join(BACKTEST_DIR, "*.md")), key=os.path.getmtime, reverse=True)
    parts = []
    for p in mds[:3]:
        try:
            with open(p, "r", encoding="utf-8") as f:
                parts.append(f"== {os.path.basename(p)} ==\n{f.read(800)}")
        except Exception:
            pass
    return "\n\n".join(parts) or "暂无回测摘要"


# ============== 邮件正文 =================


def build_html():
    info = machine_status()
    dbs = db_status()
    counts = market_counts()
    stock = stock_account()
    bt = backtest_summary_text()

    machine_rows = "".join(
        f"<tr><td style='padding:6px;font-weight:bold;width:140px'>{k}</td>"
        f"<td style='padding:6px'><code>{str(v).replace(chr(10), '<br/>')}</code></td></tr>"
        for k, v in info.items()
    )
    db_rows = "".join(
        f"<tr><td style='padding:6px;font-weight:bold;width:140px'>{k}</td>"
        f"<td style='padding:6px'>{v}</td></tr>"
        for k, v in dbs.items()
    )

    market_rows = []
    for m in MARKETS:
        st = market_state(m)
        market_rows.append(
            f"<tr><td style='padding:6px;font-weight:bold;text-transform:uppercase'>{m}</td>"
            f"<td style='padding:6px'>{st['状态']}</td>"
            f"<td style='padding:6px'>{st['初始']}</td>"
            f"<td style='padding:6px'>{st['总资产']}</td>"
            f"<td style='padding:6px'>{st['收益']}</td>"
            f"<td style='padding:6px'>{st['持仓']}</td>"
            f"<td style='padding:6px'>{st['成交']}</td></tr>"
        )

    counts_rows = []
    if isinstance(counts, dict) and "error" not in counts:
        for m, v in counts.items():
            counts_rows.append(
                f"<tr><td style='padding:6px'>{m.upper()}</td>"
                f"<td style='padding:6px'>{v.get('codes','-')}</td>"
                f"<td style='padding:6px'>{v.get('rows','-')}</td>"
                f"<td style='padding:6px'>{v.get('min','-')}</td>"
                f"<td style='padding:6px'>{v.get('max','-')}</td></tr>"
            )

    stock_rows = "".join(
        f"<tr><td style='padding:6px;font-weight:bold;width:100px'>{k}</td>"
        f"<td style='padding:6px'>{v}</td></tr>"
        for k, v in stock.items()
    )

    bt_html = bt.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""
    <html>
    <body style="font-family:Arial,Helvetica,sans-serif;line-height:1.5">
        <h2 style="font-family:monospace">🖥️ {HOST_LABEL} 每日状态报告</h2>
        <p>生成时间: {now} | 公网看板: <a href="https://quant.tail6a6cc8.ts.net/">https://quant.tail6a6cc8.ts.net/</a></p>

        <h3>⚙️ 机器状态</h3>
        <table style="border-collapse:collapse;border:1px solid #ccc">
            {machine_rows}
        </table>

        <h3>💾 数据库 / 存储</h3>
        <table style="border-collapse:collapse;border:1px solid #ccc">
            {db_rows}
        </table>

        <h3>📊 市场数据范围</h3>
        <table style="border-collapse:collapse;border:1px solid #ccc;width:100%">
            <tr style="background:#f0f0f0"><th>市场</th><th>标的数</th><th>记录数</th><th>最早</th><th>最晚</th></tr>
            {''.join(counts_rows)}
        </table>

        <h3>🇨🇳 A股模拟盘</h3>
        <table style="border-collapse:collapse;border:1px solid #ccc">
            {stock_rows}
        </table>

        <h3>📈 基金/港/美/加密/期货模拟盘</h3>
        <table style="border-collapse:collapse;border:1px solid #ccc;width:100%">
            <tr style="background:#f0f0f0"><th>市场</th><th>状态</th><th>初始</th><th>总资产</th><th>收益</th><th>持仓</th><th>成交</th></tr>
            {''.join(market_rows)}
        </table>

        <h3>📉 股票回测摘要</h3>
        <div style="background:#f7f8fa;padding:10px;border:1px solid #ccc;font-size:13px">
            {bt_html}
        </div>

        <p style="color:#888;font-size:12px">Generated by quant-status-mailer</p>
    </body>
    </html>
    """


def send(subject, html, to_addrs=TO_ADDRS):
    if not SMTP_PASS:
        print("[mail] SMTP_PASS 未配置（~/.config/ai-quant/env），跳过发送", flush=True)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = SMTP_USER
    msg["To"] = ",".join(to_addrs)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, to_addrs, msg.as_string())
    print("mail sent to", to_addrs)


def main():
    dry = "--dry-run" in sys.argv
    html = build_html()
    if dry:
        print(html[:3000])
        return
    subject = f"🤖 quant 每日状态 [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
    send(subject, html)


if __name__ == "__main__":
    main()
