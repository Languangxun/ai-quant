"""板块指数实时波动监控（事件驱动激活）。

替代固定时间表：交易日盘中每分钟拉一次东财全行业板块快照
（m:90+t:2，一次请求约 90 个板块指数），用量比/振幅预筛，
对最可疑的几个板块再拉日K复核 ATR / 布林带宽 / 量能：

- ATR：最新真实波幅 / ATR14，突破 1.8 倍算剧震；
- 布林带宽 (20,2)：当前带宽相对近 60 日带宽分布的 z 分数；
- 量能：今日成交量 / MA5。

触发条件（min_signals=2，默认三者占二）即"波动大"，由调用方
激活盘中研究 + 决策。若长时间（默认 60 分钟）没有任何激活，
静默超时也强制激活一次，防止行情平淡时系统完全失联。

只判断和报告，不下单、不改配置；状态快照存
sim/state/volatility_watch.json（已在 .gitignore）。
"""
import json
import math
import os
import time
import urllib.request
from datetime import datetime, time as dtime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UT = "fa5fd1943c7b386f172d6893dbfba10b"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# A股连续竞价窗口（按本机时间；节假日由调用方用 TradingCalendar 排除）
SESSION_WINDOWS = ((dtime(9, 25), dtime(11, 31)),
                   (dtime(12, 59), dtime(15, 1)))

DEFAULT_CONFIG = {
    "enabled": True,
    "interval_sec": 20,          # 快照间隔（秒）。行业板块快照一次请求全量~90个，
                                 # 20s 轮询 = 3 req/min，异动即可分钟级触发
    "quiet_minutes": 60,         # 静默超过 N 分钟强制激活一次
    "min_cooldown_min": 15,      # 两次激活最小间隔（分钟）
    "max_activations": 8,        # 每日最多激活次数
    "atr_ratio": 1.8,            # 当日 TR / ATR14
    "bw_z": 2.0,                 # 布林带宽 z 分数（近60日分布）
    "vol_ratio": 2.0,            # 今日量 / MA5 量
    "amp_pct": 2.2,              # 日振幅%（快照预筛参考）
    "pct_abs": 1.8,              # 涨跌幅绝对值%（预筛参考）
    "amount_ratio": 2.0,         # 快照成交额 / 自身近60个快照均值（预筛）
    "min_signals": 2,            # ATR/带宽/量能 至少满足几个才激活
    "max_verify": 6,             # 每个周期最多复核几个板块
    "verify_ttl_sec": 300,       # 同一板块复核结果缓存
    "kline_limit": 60,           # 复核拉多少根日K
}

STATE_PATH = os.path.join(BASE_DIR, "sim", "state",
                          "volatility_watch.json")

# 日K复核缓存（code -> (ts, 指标, 命中数)），跨周期持久，cap 上限防爆
_VERIFY_CACHE = {}
_VERIFY_CACHE_MAX = 256


# ---------------------------------------------------------------- 数据抓取
def _em_openers():
    """东财请求 opener 链。环境变量 EM_PROXY 配置专用出口（如
    http://100.117.242.5:8899，经 tailnet 内 Orange Pi 隧道），
    因为部分出口 IP 会被东财行情接口 WAF 拒绝；未配置则直连。"""
    proxy = (os.environ.get("EM_PROXY") or "").strip()
    opener = None
    if proxy:
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        except Exception:
            opener = None
    return ([opener] if opener else []) + [urllib.request.build_opener()]


def _get(url, timeout=6.0):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    last = None
    for opener in _em_openers():
        try:
            with opener.open(req, timeout=timeout) as r:
                raw = r.read(2_000_000)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:
            last = e
    raise last


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def fetch_sector_snapshot(timeout=6.0):
    """东财行业板块实时快照。返回 [{code,name,price,pct,high,low,open,
    prev_close,volume,amount,amp}]；失败抛异常。"""
    url = ("https://push2delay.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=100&po=1&np=1&fltt=2&invariant=0"
           "&fields=f12,f14,f2,f3,f5,f6,f7,f15,f16,f17,f18"
           f"&fs=m:90+t:2&ut={UT}")
    data = _get(url, timeout=timeout).get("data") or {}
    diff = data.get("diff") or {}
    items = list(diff.values()) if isinstance(diff, dict) else diff
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        prev_close = _f(it.get("f18"))
        high, low = _f(it.get("f15")), _f(it.get("f16"))
        amp = _f(it.get("f7"))
        if amp is None and prev_close and high is not None \
                and low is not None:
            amp = (high - low) / prev_close * 100.0
        row = {
            "code": str(it.get("f12") or ""),
            "name": str(it.get("f14") or ""),
            "price": _f(it.get("f2")),
            "pct": _f(it.get("f3")),
            "high": high,
            "low": low,
            "open": _f(it.get("f17")),
            "prev_close": prev_close,
            "volume": _f(it.get("f5")),
            "amount": _f(it.get("f6")),
            "amp": amp,
        }
        if row["code"].startswith("BK") and row["amount"]:
            row["name"] = row["name"].replace("Ⅱ", "")[:12]
            out.append(row)
    return out


def fetch_daily_kline(bk_code, limit=60, timeout=8.0):
    """板块指数日K [(date, open, close, high, low, volume)]，失败抛异常。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid=90.{bk_code}&fields1=f1,f2,f3"
           "&fields2=f51,f52,f53,f54,f55,f56"
           f"&klt=101&fqt=0&lmt={max(25, limit)}&end=20500101&ut={UT}")
    klines = (_get(url, timeout=timeout).get("data") or {}).get(
        "klines") or []
    out = []
    for s in klines:
        p = s.split(",")
        if len(p) < 6:
            continue
        try:
            out.append((p[0], float(p[1]), float(p[2]), float(p[3]),
                        float(p[4]), float(p[5])))
        except ValueError:
            continue
    return out[-limit:]


# ---------------------------------------------------------------- 指标
def indicators(bars):
    """从日K算 ATR14 比值、布林带宽(20,2) z 分数、量比。

    bars: [(date, open, close, high, low, volume)]，升序。
    返回 dict；数据不足返回 {}。"""
    if len(bars) < 25:
        return {}
    closes = [b[2] for b in bars]
    highs = [b[3] for b in bars]
    lows = [b[4] for b in bars]
    vols = [b[5] for b in bars]

    # Wilder ATR14
    trs = []
    for i in range(1, len(bars)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < 15 or sum(trs) <= 0:
        return {}
    atr = trs[0]
    for tr in trs[1:]:
        atr = (atr * 13 + tr) / 14
    if atr <= 0:
        return {}
    atr_ratio = trs[-1] / atr

    # 布林带宽 (20,2) 及其近60日分布 z 分数
    bw_hist = []
    for i in range(20, len(closes) + 1):
        win = closes[i - 20:i]
        mid = sum(win) / 20.0
        sd = math.sqrt(sum((x - mid) ** 2 for x in win) / 20.0)
        if mid > 0:
            bw_hist.append((4.0 * sd) / mid * 100.0)
    if len(bw_hist) < 10:
        return {}
    bw = bw_hist[-1]
    base = bw_hist[:-1][-60:]
    mean = sum(base) / len(base)
    sd = math.sqrt(sum((x - mean) ** 2 for x in base) / len(base))
    bw_z = (bw - mean) / sd if sd > 0 else 0.0

    # 量比：今日成交额之外的量字段是成交量；今日量 / 前5日均量
    base_v = vols[-6:-1]
    ma5 = sum(base_v) / len(base_v) if base_v else 0.0
    vol_ratio = (vols[-1] / ma5) if ma5 > 0 else None

    return {"atr_ratio": round(atr_ratio, 3), "bw": round(bw, 3),
            "bw_z": round(bw_z, 2), "vol_ratio": (round(vol_ratio, 2)
                                                  if vol_ratio else None)}


# ---------------------------------------------------------------- 状态
def load_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, path=STATE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def update_history(state, snapshot, ts=None, keep=240):
    """把本周期快照滚动并入 per-板块历史（成交额序列用于量能突增预筛）。
    默认保留 240 个快照（interval=20s 时约 80 分钟窗口）。"""
    ts = ts or time.strftime("%H:%M:%S")
    hist = state.setdefault("hist", {})
    for row in snapshot:
        code = row["code"]
        h = hist.setdefault(code, {"name": row["name"], "amount": []})
        h["name"] = row["name"]
        h.setdefault("amount", []).append(float(row["amount"] or 0))
        if len(h["amount"]) > keep:
            h["amount"] = h["amount"][-keep:]
    return state


def _amp_ratio_hist(state, code):
    h = (state.get("hist") or {}).get(code) or {}
    return [a for a in (h.get("amount") or []) if a > 0]


# ------------------------------------------------------- 预筛 / 复核
def prefilter(state, snapshot, cfg):
    """快照级粗筛：振幅 / 涨跌 / 自身量能突增。返回按可疑度排序的板块。"""
    amp_p = cfg.get("amp_pct", 2.2)
    pct_a = cfg.get("pct_abs", 1.8)
    amt_r = cfg.get("amount_ratio", 2.0)
    cands = []
    for row in snapshot:
        code, name = row["code"], row["name"]
        amount_hist = _amp_ratio_hist(state, code)[:-1]
        ratio = None
        if len(amount_hist) >= 30:
            mean = sum(amount_hist) / len(amount_hist)
            if mean > 0:
                ratio = float(row["amount"] or 0) / mean
        score, hits = 0.0, []
        amp = row.get("amp") or 0.0
        pct = abs(row.get("pct") or 0.0)
        if ratio and ratio >= amt_r:
            score += min(3.0, ratio)
            hits.append(f"额量比{ratio:.1f}")
        if amp >= amp_p:
            score += amp / amp_p
            hits.append(f"振幅{amp:.1f}%")
        if pct >= pct_a:
            score += pct / pct_a
            hits.append(f"涨跌{row.get('pct'):+.1f}%")
        if score > 0:
            cands.append((score, code, name, hits))
    cands.sort(reverse=True)
    return [(c, n, h) for _, c, n, h in cands[:int(cfg.get("max_verify", 6))]]


def verify_daily(code, cfg, cache=None):
    """拉日K复核 ATR / 布林带宽 / 量能，返回 (指标dict, 命中数)。
    cache 为空时使用模块级持久缓存（跨周期复用，按 verify_ttl_sec 过期）。"""
    now = time.time()
    if not isinstance(cache, dict):
        cache = _VERIFY_CACHE
    if len(cache) > _VERIFY_CACHE_MAX:
        cutoff = now - float(cfg.get("verify_ttl_sec", 300))
        stale = [k for k, v in cache.items() if v[0] < cutoff]
        for k in stale:
            del cache[k]
    cached = cache.get(code)
    if cached and now - cached[0] < cfg.get("verify_ttl_sec", 300):
        return cached[1], cached[2]
    try:
        bars = fetch_daily_kline(code, limit=int(cfg.get("kline_limit", 60)))
    except Exception:
        bars = []
    ind = indicators(bars) if bars else {}
    hits = 0
    if ind:
        if ind["atr_ratio"] >= cfg.get("atr_ratio", 1.8):
            hits += 1
        if ind["bw_z"] >= cfg.get("bw_z", 2.0):
            hits += 1
        vr = ind.get("vol_ratio")
        if vr is not None and vr >= cfg.get("vol_ratio", 2.0):
            hits += 1
    cache[code] = (now, ind, hits)
    return ind, hits


# ---------------------------------------------------------------- 会话
def in_session(now=None):
    t = (now or datetime.now().time())
    return any(a <= t <= b for a, b in SESSION_WINDOWS)


# ---------------------------------------------------------------- 主检查
def check(state, cfg=None, do_fetch=True, snapshot=None, verify_cache=None):
    """执行一个监控周期，返回
    {action: activate|wait|skip, reason, sector, ind, hits, quick_hits,
     force, snapshot_size}

    action=activate 时调用方应激活（研究+决策）。静默超时 /
    首次进入会话 / 冷却结束后的强制逻辑也在这里给出。
    verify_cache 缺省用模块级持久缓存（跨周期复用复核结果，
    避免 20s 快照间隔下每轮重复拉日K）。"""
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    today = time.strftime("%Y-%m-%d")
    if state.get("day") != today:
        # 新交易日：跨日清理（会话起点一并重置，修复开盘立刻
        # "静默超时强制激活" 的误触发）
        state.update(day=today, activated=0, last_ts="", hist={},
                     session_started_at=0)
    if not cfg.get("enabled", True):
        return {"action": "skip", "reason": "disabled"}

    if do_fetch:
        try:
            snapshot = fetch_sector_snapshot()
            state["snap_fail"] = 0
        except Exception as e:
            state["snap_fail"] = int(state.get("snap_fail") or 0) + 1
            snapshot = None
            last_err = f"{type(e).__name__}: {e}"[:120]
            state["snap_err"] = last_err
    if not snapshot:
        return {"action": "wait", "reason": f"无快照（连续失败"
                                        f"{state.get('snap_fail')}次）"}

    update_history(state, snapshot)
    state["last_snap_at"] = datetime.now().isoformat(timespec="seconds")
    state["last_snap_size"] = len(snapshot)

    activated = int(state.get("activated") or 0)
    last_ts = state.get("last_ts") or ""
    cooldown = float(cfg.get("min_cooldown_min", 15)) * 60
    try:
        elapsed = (time.time() - datetime.fromisoformat(last_ts)
                   .timestamp()) if last_ts else 1e9
    except ValueError:
        elapsed = 1e9

    # 预筛 -> 日K复核
    cands = prefilter(state, snapshot, cfg)
    best = None
    for code, name, hits in cands:
        ind, vh = verify_daily(code, cfg, cache=verify_cache)
        if vh >= int(cfg.get("min_signals", 2)):
            best = {"code": code, "name": name, "ind": ind,
                    "hits": vh, "quick_hits": hits}
            break

    if best and elapsed >= cooldown and activated < int(
            cfg.get("max_activations", 8)):
        return {"action": "activate",
                "reason": "波动触发：" + " ".join(best["quick_hits"]),
                "sector": best, "force": False,
                "snapshot_size": len(snapshot)}

    now_min = time.time()
    started = state.get("session_started_at") or 0
    if not started and in_session():
        state["session_started_at"] = now_min
        return {"action": "wait", "reason": "会话开始，静默计时起点",
                "snapshot_size": len(snapshot)}
    quiet = float(cfg.get("quiet_minutes", 60)) * 60
    anchor = last_ts or (datetime.fromtimestamp(started).isoformat()
                         if started else "")
    try:
        quiet_elapsed = ((time.time()
                          - datetime.fromisoformat(anchor).timestamp())
                         if anchor else 1e9)
    except ValueError:
        quiet_elapsed = 1e9
    if (in_session() and quiet_elapsed >= quiet
            and elapsed >= cooldown
            and activated < int(cfg.get("max_activations", 8))):
        return {"action": "activate",
                "reason": (f"静默 {quiet_elapsed / 60:.0f} 分钟超时，强制激活"),
                "sector": None, "force": True,
                "snapshot_size": len(snapshot)}
    return {"action": "wait", "reason": "无波动信号", "sector": None,
            "ind": (best or {}).get("ind"), "hits": (best or {}).get("hits"),
            "quick_hits": (best or {}).get("quick_hits"),
            "snapshot_size": len(snapshot)}
