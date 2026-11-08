#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pi 节点状态导出 HTTP（供 quant 网站聚合 Pi 精华数据）。

GET /health         健康检查 {"ok":1}
GET /pi_status.json 精华数据（机器/服务/五市场模拟盘/A股/回测/日志/库）
GET /               同 /pi_status.json

监听 tailscale IP（仅 tailnet 可达），30 秒内存缓存。
仅在 Pi 上运行：python3 pi_status_http.py 100.117.242.5 8188
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

HOST = sys.argv[1] if len(sys.argv) > 1 else "100.117.242.5"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8188
BASE = "/home/orangepi/ai-quant"
STATE_DIR = os.path.join(BASE, "sim", "state")
LOG_DIR = os.path.join(BASE, "logs")
BT_DIR = os.path.join(BASE, "backtest", "results")
MARKETS = ("fund", "hk", "us", "crypto", "fut")

_cache = {"ts": 0.0, "data": None}


def _sh(cmd, timeout=5):
    import subprocess
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout.strip() or "(无输出)"
    except Exception as e:
        return f"(失败: {e})"


def _df():
    try:
        st = os.statvfs("/media/usb" if os.path.exists("/media/usb") else "/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return f"{(total-free)/1e9:.1f}G 可用 / {total/1e9:.1f}G"
    except Exception as e:
        return str(e)


def _mem():
    try:
        with open("/proc/meminfo") as f:
            vals = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    vals[k.strip()] = int(v.split()[0]) * 1024
        return (f"已用 {(vals['MemTotal']-vals['MemAvailable'])/1e9:.2f}G"
                f" / {vals['MemTotal']/1e9:.2f}G")
    except Exception as e:
        return str(e)


def machine():
    return {
        "主机": _sh("hostname"),
        "Tailscale IP": _sh("tailscale ip -4 | head -1"),
        "公网地址": "https://orangepizero2w-1.tail6a6cc8.ts.net/",
        "运行时长": _sh("uptime -p 2>/dev/null || uptime"),
        "负载": _sh("cat /proc/loadavg | cut -d' ' -f1-3"),
        "磁盘USB": _df(),
        "内存": _mem(),
        "CPU": _sh("nproc") + "核",
        "时区": _sh("date +%Z (%z)"),
        "当前时间": _sh("date '+%Y-%m-%d %H:%M:%S'"),
    }


def services():
    svcs = ("ollama", "nginx", "ai-stock-web", "cron", "ssh", "tailscaled")
    return {s: _sh(f"systemctl is-active {s} 2>/dev/null || echo unknown",
                   timeout=6) for s in svcs}


def dbs():
    out = {}
    for name, path in (
            ("global_daily.db(usb)", "/media/usb/ashare/global_daily.db"),
            ("stock_cache.db(usb)", "/media/usb/ashare/stock_cache.db"),
            ("trading.db", os.path.join(BASE, "database", "trading.db"))):
        out[name] = (f"{os.path.getsize(path)/1e9:.2f}GB"
                     if os.path.exists(path) else "缺失")
    return out


def market_counts():
    try:
        con = sqlite3.connect("/media/usb/ashare/global_daily.db")
        rows = con.execute(
            "select market, count(*), count(distinct code), max(date) from daily_bars"
            " group by market").fetchall()
        con.close()
        return {"市场": "行数/代码/最新日",
                "detail": {m: {"rows": a, "codes": b, "max_date": c}
                           for m, a, b, c in rows}}
    except Exception as e:
        return {"error": str(e)[:80]}


def account(m):
    p = os.path.join(STATE_DIR, f"multi-{m}.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return None
    acct = data[-1].get("account") if isinstance(data, list) and data else (
        data if isinstance(data, dict) else {})
    if not isinstance(acct, dict):
        return None
    init = float(acct.get("initial_capital", 0) or 0)
    total = acct.get("total_asset") or acct.get("cash") or init
    try:
        ret = (float(total) - init) / init if init else 0
    except Exception:
        ret = 0
    lots = []
    pos = acct.get("positions") or acct.get("lots") or {}
    if isinstance(pos, dict):
        for code, arr in list(pos.items())[:6]:
            for single in (arr if isinstance(arr, list) else [arr]):
                if isinstance(single, dict):
                    lots.append({"code": code,
                                 "shares": single.get("shares"),
                                 "cost": single.get("cost"),
                                 "pnl_pct": single.get("pnl_pct")})
    mtime = ""
    try:
        mtime = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    except OSError:
        pass
    return {"总资产": total, "现金": acct.get("cash"),
            "收益": ret, "交易数(累计)": acct.get("trade_count", 0),
            "持仓": lots, "状态文件更新": mtime}


def stock_account():
    try:
        with open(os.path.join(STATE_DIR, "stock_account.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        return None
    mtime = ""
    try:
        mtime = time.strftime("%m-%d %H:%M", time.localtime(
            os.path.getmtime(os.path.join(STATE_DIR, "stock_account.json"))))
    except OSError:
        pass
    return {"现金": d.get("cash"), "总资产": d.get("total_asset"),
            "交易数(累计)": d.get("trade_count", 0), "日期": d.get("date"),
            "持仓数": len(d.get("positions") or {}), "状态文件更新": mtime}


def backtests():
    import glob
    files = sorted(glob.glob(os.path.join(BT_DIR, "*.md")),
                   key=os.path.getmtime, reverse=True)[:3]
    out = []
    for p in files:
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                out.append({"file": os.path.basename(p),
                            "head": f.read(700)})
        except Exception:
            pass
    return out


def logs():
    import glob
    out = {}
    for pat in ("daily-status-*.log", "daily-stock-*.log"):
        fs = sorted(glob.glob(os.path.join(LOG_DIR, pat)),
                    key=os.path.getmtime, reverse=True)
        if fs:
            try:
                with open(fs[0], "r", encoding="utf-8", errors="ignore") as f:
                    out[os.path.basename(fs[0])] = "\n".join(
                        f.read().splitlines()[-25:])
            except Exception as e:
                out[pat] = str(e)[:80]
    return out


def build():
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < 30:
        return _cache["data"]
    out = {
        "host": HOST,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "machine": machine(),
        "services": services(),
        "dbs": dbs(),
        "market_counts": market_counts(),
        "accounts": {m: account(m) for m in MARKETS},
        "stock_account": stock_account(),
        "backtest": backtests(),
        "logs": logs(),
    }
    _cache["data"], _cache["ts"] = out, now
    return out


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": 1, "ts": time.time()})
        elif self.path in ("/pi_status.json", "/"):
            try:
                self._json(200, build())
            except Exception as e:
                self._json(500, {"error": str(e)[:200]})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def _warm():
    import threading
    def w():
        time.sleep(2)
        try:
            build()
        except Exception:
            pass
    threading.Thread(target=w, daemon=True).start()


if __name__ == "__main__":
    _warm()
    HTTPServer((HOST, PORT), Handler).serve_forever()
