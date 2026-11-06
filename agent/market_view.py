"""A股大盘简报：指数均线位置、近段涨跌与市场强弱。

借鉴 stock_gui.py 的 ai_market_brief，但不依赖 GUI 的后台闸门缓存：
- 直接用 CLI 缓存里的指数日K计算 MA20/MA60、20日/60日涨跌
- 指数数据过期时用 CLI 的增量拉取刷新（单指数硬超时，失败保留旧数据）
- 结果缓存到 sim/state/market_brief.json，默认 15 分钟

用途：
- 盘前资料 08:30 生成市场简报
- 决策工具 get_market（LLM 按需调用）
- 事件风险判定（指数在 MA20/MA60 上下方 → 风险姿态）
"""
import json
import os
import threading
import time
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(BASE_DIR, "sim", "state", "market_brief.json")

INDEXES = (
    ("上证指数", "sh000001"),
    ("深证成指", "sz399001"),
    ("创业板指", "sz399006"),
    ("科创50", "sh000688"),
)
CACHE_TTL = 900
REFRESH_TIMEOUT = 12


def _num(v, digits=2):
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def _ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _ret(closes, n):
    if len(closes) <= n or not closes[-n - 1]:
        return None
    return closes[-1] / closes[-n - 1] - 1


def _days_between(a, b):
    try:
        return (datetime.strptime(b, "%Y-%m-%d")
                - datetime.strptime(a, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return None


def _with_timeout(fn, timeout):
    box = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"刷新超时>{timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _rows(code, latest=None):
    """指数日K；过期时增量刷新，刷新失败保留缓存。"""
    from data.stock import cli_bridge as bridge

    rows = bridge.db_rows(code)
    stale = bool(rows and latest and rows[-1].get("date", "") < latest)
    if not rows or stale:
        try:
            fresh = _with_timeout(
                lambda: bridge.series(code, min_bars=100),
                REFRESH_TIMEOUT)
            if fresh:
                rows = fresh
        except Exception:
            pass
    return rows or []


def _index_stats(name, code, latest):
    rows = _rows(code, latest)
    if not rows:
        return None
    closes = [r.get("close") for r in rows if r.get("close") is not None]
    if len(closes) < 20:
        return None
    close = closes[-1]
    ma20, ma60 = _ma(closes, 20), _ma(closes, 60)
    last_date = str(rows[-1].get("date") or "")
    lag = _days_between(last_date, latest) if latest and last_date else None
    return {
        "name": name,
        "code": code,
        "date": last_date,
        "lag_days": lag,
        "close": _num(close),
        "ma20": _num(ma20),
        "ma60": _num(ma60),
        "above_ma20": bool(ma20 and close >= ma20),
        "above_ma60": bool(ma60 and close >= ma60),
        "ret20_pct": _num((_ret(closes, 20) or 0) * 100, 1),
        "ret60_pct": _num((_ret(closes, 60) or 0) * 100, 1),
    }


def _render(indexes, latest):
    lines = [f"市场环境（基准交易日 {latest or '未知'}）："]
    for it in indexes:
        if not it:
            continue
        pos20 = "上方" if it["above_ma20"] else "下方"
        pos60 = "上方" if it["above_ma60"] else "下方"
        lag = (f"；数据截至 {it['date']}，滞后 {it['lag_days']} 天"
               if it.get("lag_days") else "")
        lines.append(
            f"· {it['name']} {it['date']} 收{it['close']}；"
            f"MA20 {it['ma20']}（{pos20}）、MA60 {it['ma60']}（{pos60}）；"
            f"近20日{it['ret20_pct']:+.1f}%、近60日{it['ret60_pct']:+.1f}%"
            f"{lag}")
    valid = [x for x in indexes if x]
    above20 = sum(1 for x in valid if x["above_ma20"])
    above60 = sum(1 for x in valid if x["above_ma60"])
    if valid:
        if above20 >= max(3, len(valid) - 1) and above60 >= 2:
            trend = "strong"
            label = "偏强"
        elif above20 <= 1:
            trend = "weak"
            label = "偏弱"
        else:
            trend = "neutral"
            label = "震荡"
        lines.append(
            f"· 趋势：{len(valid)} 个指数中 {above20} 个在 MA20 上方、"
            f"{above60} 个在 MA60 上方 → {label}")
    return "\n".join(lines), trend if valid else "unknown"


def build(force=False, ttl=CACHE_TTL):
    """构建/读取市场简报。返回 {text, trend, indexes, generated_at, ...}。"""
    now = time.time()
    if not force:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                doc = json.load(f)
            if (isinstance(doc, dict)
                    and now - float(doc.get("ts") or 0) < ttl
                    and doc.get("text")):
                return doc
        except (OSError, ValueError):
            pass

    from data.stock import cli_bridge as bridge

    latest = bridge.latest_date() or date.today().isoformat()
    indexes = []
    for name, code in INDEXES:
        try:
            indexes.append(_index_stats(name, code, latest))
        except Exception:
            indexes.append(None)
    text, trend = _render(indexes, latest)
    doc = {
        "ts": now,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "latest": latest,
        "trend": trend,
        "text": text,
        "indexes": [x for x in indexes if x],
    }
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass
    return doc


def brief(force=False):
    """决策/盘前直接可用的市场简报文本；失败返回空串。"""
    try:
        return str(build(force=force).get("text") or "")
    except Exception:
        return ""


def trend(force=False):
    try:
        return str(build(force=force).get("trend") or "unknown")
    except Exception:
        return "unknown"
